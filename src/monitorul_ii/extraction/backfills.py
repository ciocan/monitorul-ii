"""Registry-driven backfill passes (Tier 4).

Sister to `linker.py`. Each pass walks `*.extraction.json` sidecars,
joins specific raw fields against a curated registry in
`monitorul_ii.registries`, and writes the canonical id back into the
sidecar's `*_normalized` slot.

Same architectural shape as the cross-doc linker:

  - Pre-write JSON Schema validation so a normaliser hiccup never lands
    a broken sidecar on disk.
  - Atomic write via `.part` rename.
  - Idempotent: skip when the target field already holds the correct
    canonical id; `force=True` overwrites mismatches.

Backfill versions are intentionally NOT part of `extractor_versions`
(Q11 conservative-by-design contract — exact-match semantics would
force extractor re-runs whenever a registry bumped). Backfill output
lives entirely inside body content; if a future extract re-run clobbers
it (extractor version bumped → re-extract), the user re-runs `backfill`
to recover. Re-running is fast (in-memory dict lookup per record).

# Passes

  - `backfill_issuing_body` (4.1) — populates
    `body.report.issuing_body_normalized` on `report_facsimile`
    sidecars from the institutional-bodies registry.
  - `backfill_ministries` (4.2) — populates
    `qr.questions[].addressee.ministry_normalized` and
    `plenary_*.interpellations[].addressed_to_normalized` from the
    ministries registry, with institutional-body fallback for
    addressees outside the ministry namespace.
  - `backfill_proposed_by` (4.4) — populates
    `plenary_*.agenda_items[].activities[].proposed_by` (Speaker
    shape, `role="Guvern"`) on votes whose parent agenda's
    `primary_references[]` carries an `oug` or `og` ref OR whose
    title matches an OUG/OG cite pattern. Precision-first: votes
    without a Government signal stay null. Recovering the long tail
    (PL-x / L bills proposed by parliamentary groups / individual
    MPs / committees) requires per-bill parlament.ro metadata —
    deferred.

Future passes follow the same shape; each lives as its own function
in this module to keep the registry-to-field mapping explicit and one
easy `grep` away.

  - 4.3 — Speaker.person_id from an MP-list registry. Blocked on
    data acquisition (cdep.ro / senat.ro MP lists or curated CSV).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from monitorul_ii.extraction.schema import SchemaError, validate
from monitorul_ii.registries import (
    INSTITUTIONAL_BODIES_REGISTRY_VERSION,
    MINISTRIES_REGISTRY_VERSION,
    PERSONS_REGISTRY_VERSION,
    MatchedVia,
    normalize_addressee,
    normalize_institutional_body,
    normalize_speaker,
)

BackfillStatus = Literal["filled", "skip", "error"]

# Per-pass version. Bumped when the field-extraction logic itself
# changes; the registry version is recorded separately on each result.
ISSUING_BODY_BACKFILL_VERSION = "0.1.0"
MINISTRY_BACKFILL_VERSION = "0.1.0"
PROPOSED_BY_BACKFILL_VERSION = "0.1.0"
PERSONS_BACKFILL_VERSION = "0.1.0"


@dataclass(frozen=True)
class BackfillResult:
    sidecar_path: Path
    status: BackfillStatus
    field: str  # JSONPointer-ish path of the field we wrote (or skipped)
    canonical_id: str | None = None
    matched_via: MatchedVia | None = None
    raw_value: str | None = None
    reason: str | None = None


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_atomic(path: Path, payload: str) -> None:
    """Atomic write via `.part` rename — same contract as linker."""
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


# -- issuing body (4.1) -----------------------------------------------------


def backfill_issuing_body(
    sidecar_path: Path,
    *,
    force: bool = False,
    write: bool = True,
) -> BackfillResult:
    """Fill `report.issuing_body_normalized` on one report_facsimile sidecar.

    Reads the sidecar, looks up `body.report.issuing_body` in the
    institutional-bodies registry, writes the canonical id (or `None`
    on no-match — null is the schema default and signals "registry
    gap"). Pre-write schema validation; atomic rename.

    Idempotency:
      - target slot already holds the same id → skip (no write).
      - target slot holds a different id → skip unless `force=True`.
      - target slot is null and registry returns no match → skip.

    `write=False` runs the full logic without touching disk (test +
    --dry-run path).
    """
    field = "body.report.issuing_body_normalized"

    sc = _read_sidecar(sidecar_path)
    if sc is None:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason="failed to read sidecar JSON",
        )
    if sc.get("document_type") != "report_facsimile":
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="skip",
            field=field,
            reason="not a report_facsimile sidecar",
        )
    body = sc.get("body")
    if not isinstance(body, dict):
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason="missing body",
        )
    report = body.get("report")
    if not isinstance(report, dict):
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason="missing body.report",
        )

    raw = report.get("issuing_body")
    if not isinstance(raw, str) or not raw:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="skip",
            field=field,
            reason="no issuing_body raw value",
            raw_value=None,
        )

    canonical, matched_via = normalize_institutional_body(raw)
    existing = report.get("issuing_body_normalized")

    if canonical is None:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="skip",
            field=field,
            reason="no registry match",
            raw_value=raw,
        )
    if existing == canonical:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="skip",
            field=field,
            reason="already filled with same canonical id",
            canonical_id=canonical,
            matched_via=matched_via,
            raw_value=raw,
        )
    if existing not in (None, "") and not force:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="skip",
            field=field,
            reason=f"existing id {existing!r} differs from registry id "
            f"{canonical!r} (use --force to overwrite)",
            canonical_id=canonical,
            matched_via=matched_via,
            raw_value=raw,
        )

    report["issuing_body_normalized"] = canonical
    try:
        validate(sc)
    except SchemaError as exc:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason=f"schema validation failed after backfill: {exc.errors[0]}",
            canonical_id=canonical,
            matched_via=matched_via,
            raw_value=raw,
        )
    if write:
        _write_atomic(
            sidecar_path,
            json.dumps(sc, indent=2, ensure_ascii=False),
        )
    return BackfillResult(
        sidecar_path=sidecar_path,
        status="filled",
        field=field,
        canonical_id=canonical,
        matched_via=matched_via,
        raw_value=raw,
    )


def backfill_all_issuing_bodies(
    sidecars: Iterable[Path],
    *,
    force: bool = False,
    write: bool = True,
) -> Iterator[BackfillResult]:
    """Walk a list of sidecars, run the issuing-body pass on each report.

    Non-report sidecars are filtered out cheaply (one read + a single
    `document_type` check) and produce no result. Errors and skips
    produce one result each so the CLI can summarise.
    """
    for path in sidecars:
        sc = _read_sidecar(path)
        if sc is None:
            continue
        if sc.get("document_type") != "report_facsimile":
            continue
        yield backfill_issuing_body(path, force=force, write=write)


# -- ministry / addressee (4.2) ---------------------------------------------


@dataclass(frozen=True)
class MinistryBackfillCounts:
    """Per-sidecar counts for the ministry pass."""

    qr_filled: int = 0
    qr_skipped: int = 0
    plenary_filled: int = 0
    plenary_skipped: int = 0


def _maybe_apply_normalized(
    target: dict[str, Any],
    field: str,
    raw: str | None,
    canonical: str | None,
    *,
    force: bool,
) -> tuple[bool, str | None]:
    """Conditionally write `target[field] = canonical`.

    Returns `(written, skip_reason)` — `written` is True iff we actually
    mutated the dict. `skip_reason` is set when we declined to write
    (already-filled-same / mismatch / no-match / no-raw). Used as the
    inner loop body for both qr.questions[*] and plenary.interpellations[*].
    """
    existing = target.get(field)
    if not isinstance(raw, str) or not raw:
        return (False, "no raw value")
    if canonical is None:
        return (False, "no registry match")
    if existing == canonical:
        return (False, "already filled")
    if existing not in (None, "") and not force:
        return (False, "mismatch (force off)")
    target[field] = canonical
    return (True, None)


def backfill_ministries(
    sidecar_path: Path,
    *,
    force: bool = False,
    write: bool = True,
) -> BackfillResult:
    """Fill ministry/addressee normalized slots on one sidecar.

    Walks every record-level slot that holds a ministry- or
    institutional-body raw and joins it against the composite registry
    (ministry first, institutional fallback). Touched slots:

      - `question_register.body.questions[*].addressee.ministry_normalized`
      - `plenary_*.body.interpellations[*].addressed_to_normalized`

    Other document types are skipped cheaply. Pre-write schema
    validation runs over the mutated dict; on failure the file is NOT
    touched.

    The result's `canonical_id` and `matched_via` summarise the *first*
    fill in the document for telemetry; per-record counts live in
    `result.reason` as a structured summary string when at least one
    fill happened. Skip results carry the dominant skip reason.
    """
    field = "ministry/addressee normalized"

    sc = _read_sidecar(sidecar_path)
    if sc is None:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason="failed to read sidecar JSON",
        )
    doc_type = sc.get("document_type")
    if doc_type not in (
        "question_register",
        "plenary_stenogram",
        "plenary_joint_session",
    ):
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="skip",
            field=field,
            reason="not a ministry-bearing sidecar",
        )

    body = sc.get("body")
    if not isinstance(body, dict):
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason="missing body",
        )

    fills: list[tuple[str, str, MatchedVia]] = []
    skip_counts: dict[str, int] = {}
    raw_examples: list[tuple[str, str | None]] = []  # (raw, canonical-or-none)

    if doc_type == "question_register":
        for q in body.get("questions") or []:
            addr = q.get("addressee")
            if not isinstance(addr, dict):
                continue
            raw = addr.get("ministry")
            canonical, via = normalize_addressee(raw)
            written, reason = _maybe_apply_normalized(
                addr,
                "ministry_normalized",
                raw,
                canonical,
                force=force,
            )
            if written:
                fills.append((raw or "", canonical or "", via or "exact"))
            elif reason:
                skip_counts[reason] = skip_counts.get(reason, 0) + 1
            if raw and len(raw_examples) < 3:
                raw_examples.append((raw, canonical))
    else:
        for it in body.get("interpellations") or []:
            raw = it.get("addressed_to")
            canonical, via = normalize_addressee(raw)
            written, reason = _maybe_apply_normalized(
                it,
                "addressed_to_normalized",
                raw,
                canonical,
                force=force,
            )
            if written:
                fills.append((raw or "", canonical or "", via or "exact"))
            elif reason:
                skip_counts[reason] = skip_counts.get(reason, 0) + 1
            if raw and len(raw_examples) < 3:
                raw_examples.append((raw, canonical))

    if not fills:
        # No mutation; figure out the dominant skip reason for logs.
        if skip_counts:
            dom = max(skip_counts, key=lambda k: skip_counts[k])
            reason = f"{dom} ({sum(skip_counts.values())} records)"
        else:
            reason = "no records"
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="skip",
            field=field,
            reason=reason,
            raw_value=raw_examples[0][0] if raw_examples else None,
        )

    try:
        validate(sc)
    except SchemaError as exc:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason=f"schema validation failed after backfill: {exc.errors[0]}",
        )
    if write:
        _write_atomic(
            sidecar_path,
            json.dumps(sc, indent=2, ensure_ascii=False),
        )
    # Headline: first fill (mostly diagnostic; full per-record telemetry
    # comes through the matched_via histogram the CLI builds across runs).
    raw, canonical, via = fills[0]
    summary_reason = f"{len(fills)} records filled"
    if skip_counts:
        skipped_total = sum(skip_counts.values())
        summary_reason += f", {skipped_total} skipped"
    return BackfillResult(
        sidecar_path=sidecar_path,
        status="filled",
        field=field,
        canonical_id=canonical,
        matched_via=via,
        raw_value=raw,
        reason=summary_reason,
    )


def backfill_all_ministries(
    sidecars: Iterable[Path],
    *,
    force: bool = False,
    write: bool = True,
) -> Iterator[BackfillResult]:
    """Walk a list of sidecars, run the ministry pass on each."""
    for path in sidecars:
        sc = _read_sidecar(path)
        if sc is None:
            continue
        if sc.get("document_type") not in (
            "question_register",
            "plenary_stenogram",
            "plenary_joint_session",
        ):
            continue
        yield backfill_ministries(path, force=force, write=write)


# -- proposed_by (4.4) ------------------------------------------------------


# Government-proposer detector. Two signals (logical OR):
#   1. Parent agenda's `primary_references[]` contains a ref of type `oug`
#      or `og` — Government Ordinances / Emergency Ordinances are by
#      definition government-issued, and any vote on a bill that approves
#      one inherits that proposer.
#   2. Agenda title contains an OUG/OG cite — covers cases where the
#      ref-extractor missed the cite but the title still mentions
#      "Ordonanței Guvernului" / "OUG" / etc.
#
# The detector is precision-first. The ~85% of votes that don't fit
# either signal stay with `proposed_by=null`; those are mostly PL-x /
# L bills whose proposer (parliamentary group / individual MP /
# committee) is recorded on parlament.ro per-bill metadata. Recovering
# the long tail requires a parlament.ro scrape (deferred — see
# `docs/architecture.md` § "Future graduation candidates"). The 60%
# coverage target the original prompt cited isn't reachable from
# title/ref pattern alone on this corpus; bill-by-bill metadata is
# stripped from the agenda text by the time it lands in the stenogram.
_GOVERNMENT_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"Ordonan[țţt]ei?\s+(?:Guvernului|de\s+urgen[țţt][ăa]?\s+a\s+Guvernului)",
        re.IGNORECASE,
    ),
    re.compile(r"\bO\.?U\.?G\.?\b", re.IGNORECASE),
    # Bare `OG` is too noisy (collides with words like `OGURI`). Keep
    # the dotted form only.
    re.compile(r"\bO\.G\.\b"),
)


def _build_government_speaker() -> dict[str, Any]:
    """Construct the canonical `Guvern` Speaker dict used for OUG/OG votes.

    `Vote.proposed_by` is a Speaker (not a string) per the schema; for
    Government-proposed bills the proposer is the government itself, so
    we synthesise a stable Speaker dict with `role="Guvern"` and
    `raw="Guvernul României"`. `name` / `title` / `party_group` /
    `person_id` stay null — the government isn't a single named person
    and a future person-registry pass would (correctly) fail to match
    this entry.
    """
    return {
        "raw": "Guvernul României",
        "name": "Guvernul",
        "title": None,
        "role": "Guvern",
        "party_group": None,
        "person_id": None,
    }


def _agenda_is_government_proposed(agenda_item: dict[str, Any]) -> bool:
    refs = agenda_item.get("primary_references") or []
    for r in refs:
        if isinstance(r, dict) and r.get("type") in ("oug", "og"):
            return True
    title = agenda_item.get("title") or ""
    if not isinstance(title, str):
        return False
    return any(p.search(title) for p in _GOVERNMENT_TITLE_PATTERNS)


def backfill_proposed_by(
    sidecar_path: Path,
    *,
    force: bool = False,
    write: bool = True,
) -> BackfillResult:
    """Fill `Vote.proposed_by` on plenary sidecars from agenda signals.

    For each `vote` activity under each agenda item: if the parent
    agenda's `primary_references[]` carries an `oug` or `og` cite, OR
    the agenda title matches one of the OUG/OG title patterns, the
    proposer is the Government — we populate `proposed_by` with the
    canonical `Guvern` Speaker dict.

    The pass is intentionally precision-first; votes whose agenda
    carries no OUG/OG signal stay with `proposed_by=null`. Recovering
    those (~85% of votes) requires per-bill parlament.ro metadata; that
    path is deferred.

    Same atomic-write + pre-write schema validation contract as the
    other passes. Idempotent: skip when every vote in scope already
    holds the matching proposer; mismatches skip unless `force=True`.
    """
    field = "vote.proposed_by"

    sc = _read_sidecar(sidecar_path)
    if sc is None:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason="failed to read sidecar JSON",
        )
    if sc.get("document_type") not in ("plenary_stenogram", "plenary_joint_session"):
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="skip",
            field=field,
            reason="not a plenary sidecar",
        )

    body = sc.get("body")
    if not isinstance(body, dict):
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason="missing body",
        )

    fills = 0
    skipped_eligible = 0
    skip_counts: dict[str, int] = {}
    items = body.get("agenda_items") or []
    gov_speaker = _build_government_speaker()

    for ag in items:
        if not _agenda_is_government_proposed(ag):
            continue
        for act in ag.get("activities") or []:
            if act.get("type") != "vote":
                continue
            existing = act.get("proposed_by")
            if existing == gov_speaker:
                skip_counts["already filled"] = skip_counts.get("already filled", 0) + 1
                skipped_eligible += 1
                continue
            if existing is not None and not force:
                skip_counts["mismatch (force off)"] = (
                    skip_counts.get("mismatch (force off)", 0) + 1
                )
                skipped_eligible += 1
                continue
            act["proposed_by"] = dict(gov_speaker)
            fills += 1

    if fills == 0:
        if skipped_eligible:
            dom = max(skip_counts, key=lambda k: skip_counts[k])
            reason = f"{dom} ({skipped_eligible} eligible votes)"
        else:
            reason = "no government-proposed agendas"
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="skip",
            field=field,
            reason=reason,
        )

    try:
        validate(sc)
    except SchemaError as exc:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason=f"schema validation failed after backfill: {exc.errors[0]}",
        )
    if write:
        _write_atomic(
            sidecar_path,
            json.dumps(sc, indent=2, ensure_ascii=False),
        )
    summary = f"{fills} votes filled (Guvern proposer)"
    if skipped_eligible:
        summary += f", {skipped_eligible} skipped"
    return BackfillResult(
        sidecar_path=sidecar_path,
        status="filled",
        field=field,
        canonical_id="guvern",
        matched_via="exact",
        reason=summary,
    )


def backfill_all_proposed_by(
    sidecars: Iterable[Path],
    *,
    force: bool = False,
    write: bool = True,
) -> Iterator[BackfillResult]:
    """Walk plenary sidecars; populate `Vote.proposed_by` from OUG/OG signals."""
    for path in sidecars:
        sc = _read_sidecar(path)
        if sc is None:
            continue
        if sc.get("document_type") not in (
            "plenary_stenogram",
            "plenary_joint_session",
        ):
            continue
        yield backfill_proposed_by(path, force=force, write=write)


# -- persons (4.3) ----------------------------------------------------------


# Speaker dicts are spread across many sidecar paths. Rather than hand-
# rolling a per-doc-type walker for each variant we duck-type by shape:
# a Speaker is any dict whose key set is exactly the canonical six.
_SPEAKER_KEYS = frozenset({"raw", "name", "title", "role", "party_group", "person_id"})


def _walk_speakers_in_place(o: Any) -> Iterator[dict[str, Any]]:
    """Yield every Speaker dict under `o`, recursing into containers.

    We do NOT recurse into a Speaker's own fields once detected — speakers
    are leaves and their `raw`/`name` strings can technically contain
    nested punctuation that this function would otherwise mistake for a
    container. Identification is structural (presence of all six required
    Speaker keys); committee roster `RosterEntry`, signatures, vote
    `proposed_by`, interpellation questioner / response.speaker, plenary
    chair / chair_segments / secretaries are all caught by the same walk.
    """
    if isinstance(o, dict):
        if _SPEAKER_KEYS.issubset(o.keys()):
            yield o
            return
        for v in o.values():
            yield from _walk_speakers_in_place(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_speakers_in_place(v)


def _person_match_input(speaker: dict[str, Any]) -> str | None:
    """Pick the cleanest string to feed normalize_speaker.

    Prefers `name` (the extractor's parsed person-name span) when it's a
    non-empty string; falls back to `raw`. Both can be present on a well-
    formed sidecar; `name` typically has the honorific peeled and is the
    most direct registry-match candidate.
    """
    name = speaker.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    raw = speaker.get("raw")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def backfill_persons(
    sidecar_path: Path,
    *,
    force: bool = False,
    write: bool = True,
) -> BackfillResult:
    """Fill `Speaker.person_id` across every Speaker dict in one sidecar.

    Walks the sidecar body for Speaker shapes (chair, secretaries,
    chair_segments, agenda activities' speaker / proposed_by, interpellation
    questioner / response.speaker, qr.questions[].questioner, committee
    roster, committee signatures, etc.). For each, calls
    `normalize_speaker(name_or_raw, context_year=metadata.year)` and writes
    the resolved id to `person_id`. Pre-write schema validation; atomic
    rename. The same Q11 contract as the other backfills: re-extract
    clobbers `person_id`, re-running the backfill recovers.

    Idempotency:
      - target slot already holds the same canonical id → skip-not-write.
      - target slot holds a different id → skip unless `force=True`.
      - target slot is null and registry returns no match → skip-not-write.

    `force=True` overwrites mismatches.
    `write=False` runs the full logic (including matching) without
    touching disk — the test + --dry-run path.

    The result's `canonical_id` and `matched_via` summarise the *first*
    fill in the document for headline telemetry; the per-record matched-
    via histogram lives on the CLI side.
    """
    field = "speaker.person_id"

    sc = _read_sidecar(sidecar_path)
    if sc is None:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason="failed to read sidecar JSON",
        )
    body = sc.get("body")
    if not isinstance(body, dict):
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason="missing body",
        )

    metadata = sc.get("metadata") or {}
    year_val = metadata.get("year")
    context_year: int | None = year_val if isinstance(year_val, int) else None

    fills: list[tuple[str, str, MatchedVia]] = []
    skip_counts: dict[str, int] = {}
    seen_speakers = 0

    for speaker in _walk_speakers_in_place(body):
        seen_speakers += 1
        target_input = _person_match_input(speaker)
        if not target_input:
            skip_counts["no raw value"] = skip_counts.get("no raw value", 0) + 1
            continue
        canonical, via = normalize_speaker(target_input, context_year=context_year)
        existing = speaker.get("person_id")
        if canonical is None:
            skip_counts["no registry match"] = (
                skip_counts.get("no registry match", 0) + 1
            )
            continue
        if existing == canonical:
            skip_counts["already filled"] = skip_counts.get("already filled", 0) + 1
            continue
        if existing is not None and not force:
            skip_counts["mismatch (force off)"] = (
                skip_counts.get("mismatch (force off)", 0) + 1
            )
            continue
        speaker["person_id"] = canonical
        fills.append((target_input, canonical, via or "exact"))

    if not fills:
        if seen_speakers == 0:
            reason = "no speakers in body"
        elif skip_counts:
            dom = max(skip_counts, key=lambda k: skip_counts[k])
            reason = f"{dom} ({sum(skip_counts.values())} speakers)"
        else:
            reason = "no speakers needing fill"
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="skip",
            field=field,
            reason=reason,
        )

    try:
        validate(sc)
    except SchemaError as exc:
        return BackfillResult(
            sidecar_path=sidecar_path,
            status="error",
            field=field,
            reason=f"schema validation failed after backfill: {exc.errors[0]}",
        )
    if write:
        _write_atomic(
            sidecar_path,
            json.dumps(sc, indent=2, ensure_ascii=False),
        )
    raw_first, canonical_first, via_first = fills[0]
    summary = f"{len(fills)} speakers filled"
    if skip_counts:
        skipped_total = sum(skip_counts.values())
        summary += f", {skipped_total} skipped"
    return BackfillResult(
        sidecar_path=sidecar_path,
        status="filled",
        field=field,
        canonical_id=canonical_first,
        matched_via=via_first,
        raw_value=raw_first,
        reason=summary,
    )


def backfill_all_persons(
    sidecars: Iterable[Path],
    *,
    force: bool = False,
    write: bool = True,
) -> Iterator[BackfillResult]:
    """Walk a list of sidecars, run the persons pass on each.

    Every document type is in scope (Speakers appear in plenary, qr, and
    committee_synthesis). Cheap pre-filter just on JSON parse + body
    presence; backfill_persons handles the speaker-walk internally.
    """
    for path in sidecars:
        sc = _read_sidecar(path)
        if sc is None:
            continue
        if not isinstance(sc.get("body"), dict):
            continue
        yield backfill_persons(path, force=force, write=write)


__all__ = [
    "INSTITUTIONAL_BODIES_REGISTRY_VERSION",
    "ISSUING_BODY_BACKFILL_VERSION",
    "MINISTRIES_REGISTRY_VERSION",
    "MINISTRY_BACKFILL_VERSION",
    "PERSONS_BACKFILL_VERSION",
    "PERSONS_REGISTRY_VERSION",
    "PROPOSED_BY_BACKFILL_VERSION",
    "BackfillResult",
    "BackfillStatus",
    "MinistryBackfillCounts",
    "backfill_all_issuing_bodies",
    "backfill_all_ministries",
    "backfill_all_persons",
    "backfill_all_proposed_by",
    "backfill_issuing_body",
    "backfill_ministries",
    "backfill_persons",
    "backfill_proposed_by",
]
