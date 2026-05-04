"""Cross-document linker pass.

Populates back-link fields that need awareness of OTHER sidecars, beyond
what the per-type extractors can fill at single-doc extract time. v0.1
fills exactly one slot:

  `report_facsimile.body.report.received_at.received_in_document` — the
  `mo://YYYY/PART/ISSUE` document_id of the joint-session (or single-
  chamber) stenogram that received the report.

Workflow: extract first (writes `received_in_document: null` for
report_facsimile sidecars), then run the linker over the same directory.
The linker walks `*.extraction.json`, indexes the joint-session +
plenary stenograms by `metadata.session_date` (with `metadata.published`
fallback), and fills in the back-links by date match.

**Idempotency.** Linker runs are idempotent: by default each report sidecar
is linked at most once (subsequent runs see the populated field and skip).
`force=True` re-links populated entries — use after a stenogram cohort
re-extract that may have rewritten document_ids.

**Versioning.** The linker version is **not** part of `extractor_versions`,
because that field's exact-match semantics (Q11 conservative-by-design)
would force extractor re-runs whenever the linker bumps. Linker output
lives entirely inside body content; if a future extract re-run clobbers
it (extractor version bumped → re-extract), the user re-runs `link` to
recover. Linking is fast (a single dict lookup per doc).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from monitorul_ii.extraction.schema import SchemaError, validate

LINKER_VERSION = "0.1.0"


LinkStatus = Literal["linked", "skip", "error"]


@dataclass(frozen=True)
class LinkResult:
    sidecar_path: Path
    status: LinkStatus
    target_document_id: str | None = None
    reason: str | None = None
    sidecar: dict[str, Any] | None = None  # populated for status=="linked"


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_atomic(path: Path, payload: str) -> None:
    """Atomic write via `.part` rename — same contract as the dispatcher."""
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


# Document types that act as "receiving sessions" for report_facsimile.
# Joint sessions are the dominant case (every R-suffix doc observed in the
# corpus is received in joint session); single-chamber stenograms are
# included for completeness because the schema's `received_at.session_kind`
# allows `camera` / `senat` for future-proofing.
_RECEIVING_SESSION_TYPES = ("plenary_joint_session", "plenary_stenogram")


def build_session_index(sidecars: Iterable[Path]) -> dict[str, str]:
    """Index of `session_date` → `document_id` for receiving-session sidecars.

    Walks the iterable of sidecar paths once, reads each, and indexes any
    `plenary_joint_session` / `plenary_stenogram` sidecar by its
    `metadata.session_date` (falling back to `metadata.published` when
    session_date is null). When multiple sidecars share a date,
    `plenary_joint_session` wins over `plenary_stenogram` (joint sessions
    receive reports more often); within the same type, first-seen wins.
    """
    by_date: dict[str, tuple[int, str]] = {}
    # priority: lower number wins. joint=0 beats stenogram=1.
    type_priority = {"plenary_joint_session": 0, "plenary_stenogram": 1}
    for path in sidecars:
        sc = _read_sidecar(path)
        if sc is None:
            continue
        doc_type = sc.get("document_type")
        if doc_type not in _RECEIVING_SESSION_TYPES:
            continue
        meta = sc.get("metadata") or {}
        date = meta.get("session_date") or meta.get("published")
        if not isinstance(date, str) or not date:
            continue
        doc_id = sc.get("document_id")
        if not isinstance(doc_id, str) or not doc_id:
            continue
        prio = type_priority[doc_type]
        existing = by_date.get(date)
        if existing is None or prio < existing[0]:
            by_date[date] = (prio, doc_id)
    return {date: doc_id for date, (_, doc_id) in by_date.items()}


def _get_received_at(sidecar: dict[str, Any]) -> dict[str, Any] | None:
    """Return the report_facsimile `received_at` block, or None if absent."""
    body = sidecar.get("body")
    if not isinstance(body, dict):
        return None
    report = body.get("report")
    if not isinstance(report, dict):
        return None
    received_at = report.get("received_at")
    if not isinstance(received_at, dict):
        return None
    return received_at


def link_report(
    sidecar_path: Path,
    *,
    session_index: dict[str, str],
    force: bool = False,
    write: bool = True,
) -> LinkResult:
    """Link a single report_facsimile sidecar to its receiving stenogram.

    Reads the sidecar, looks up `received_at.session_date` in the index,
    writes `received_in_document` back. Pre-write schema validation
    guards against shape regressions; an invalid post-link sidecar is
    rejected and the file is NOT touched.

    Returns a LinkResult with the new document_id (status=linked) or a
    skip reason (status=skip / error). `write=False` runs the full link
    logic but doesn't touch disk — useful in tests.
    """
    sc = _read_sidecar(sidecar_path)
    if sc is None:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="error",
            reason="failed to read sidecar JSON",
        )
    if sc.get("document_type") != "report_facsimile":
        return LinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="not a report_facsimile sidecar",
        )
    received_at = _get_received_at(sc)
    if received_at is None:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="error",
            reason="missing body.report.received_at block",
        )
    existing = received_at.get("received_in_document")
    if existing and not force:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="already linked",
            target_document_id=existing,
        )
    session_date = received_at.get("session_date")
    if not isinstance(session_date, str) or not session_date:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="no session_date in report",
        )
    target = session_index.get(session_date)
    if target is None:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason=f"no receiving session indexed for {session_date}",
        )
    # Don't link a sidecar to itself (defensive: shouldn't happen because
    # report_facsimile is excluded from the index, but guard anyway).
    if target == sc.get("document_id"):
        return LinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="self-link prevented",
        )
    received_at["received_in_document"] = target
    try:
        validate(sc)
    except SchemaError as exc:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="error",
            reason=f"schema validation failed after link: {exc.errors[0]}",
        )
    if write:
        _write_atomic(
            sidecar_path,
            json.dumps(sc, indent=2, ensure_ascii=False),
        )
    return LinkResult(
        sidecar_path=sidecar_path,
        status="linked",
        target_document_id=target,
        sidecar=sc,
    )


def link_all(
    sidecars: Iterable[Path],
    *,
    force: bool = False,
    write: bool = True,
) -> Iterator[LinkResult]:
    """Walk a list of sidecars, build the session index, link each report.

    Two passes over the list: pass 1 builds the index from joint /
    single-chamber stenograms; pass 2 yields a LinkResult per
    report_facsimile sidecar. Non-report sidecars are silently skipped
    (no result yielded — the caller filters on its own input).
    """
    sidecar_list = list(sidecars)
    session_index = build_session_index(sidecar_list)
    for path in sidecar_list:
        sc = _read_sidecar(path)
        if sc is None:
            continue
        if sc.get("document_type") != "report_facsimile":
            continue
        yield link_report(
            path,
            session_index=session_index,
            force=force,
            write=write,
        )


__all__ = [
    "LINKER_VERSION",
    "LinkResult",
    "build_session_index",
    "link_report",
    "link_all",
]
