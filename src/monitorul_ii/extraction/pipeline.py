"""Extract dispatcher: MD → sidecar JSON.

Responsibilities, in order:

  1. Read MD, split into frontmatter + body, build `EnvelopeMeta`.
  2. Classify (or honour `override_type`) to decide which extractor runs.
  3. Skip-with-reason for types whose extractor hasn't shipped — does NOT
     write a stub `body=other` sidecar (Q1 from the design grilling).
  4. Build the `ExtractContext`, run the per-type extractor, merge the
     shared boilerplate claims, compute coverage.
  5. Build the envelope, attach body + coverage, validate against the JSON
     Schema. Validation failure dumps the rejected dict to
     `<basename>.rejected.json` for inspection (Q3 atomicity contract).
  6. Write the sidecar atomically (`.part` rename).

Idempotency is version-aware (Q2): an existing sidecar's
`schema_version` + per-component `extractor_versions` are compared to the
current code; matches skip, mismatches re-extract. `force=True` overrides.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from monitorul_ii.classifier import (
    HEADER_WINDOW_BYTES,
    DocumentType,
    classify,
    parse_issue_suffix,
)
from monitorul_ii.extraction.boilerplate import (
    BOILERPLATE_VERSION,
    claim_shared_boilerplate,
)
from monitorul_ii.extraction.coverage import (
    COVERAGE_VERSION,
    Claim,
    compute_coverage,
    line_offsets,
    lines_for_range,
)
from monitorul_ii.extraction.envelope import (
    EnvelopeMeta,
    document_id,
    envelope_meta_from_frontmatter,
    split_md,
)
from monitorul_ii.extraction.extractors import EXTRACTOR_VERSIONS, EXTRACTORS
from monitorul_ii.extraction.references import REFERENCES_VERSION
from monitorul_ii.extraction.schema import SchemaError, validate
from monitorul_ii.extraction.speakers import SPEAKERS_VERSION
from monitorul_ii.extraction.topics import TOPICS_VERSION

SCHEMA_VERSION = "1.7.0"
EXTRACTOR_LABEL = "regex@1"


# Canonical map of shared-helper versions. The dispatcher copies this into
# each sidecar's `extractor_versions`; bumping any value here invalidates
# every sidecar (since every body uses these helpers transitively).
def _shared_helper_versions() -> dict[str, str]:
    return {
        "boilerplate": BOILERPLATE_VERSION,
        "coverage": COVERAGE_VERSION,
        "references": REFERENCES_VERSION,
        "speakers": SPEAKERS_VERSION,
        "topics": TOPICS_VERSION,
    }


ExtractStatus = Literal["extract", "skip", "error"]


@dataclass(frozen=True)
class ExtractResult:
    md_path: Path
    sidecar_path: Path
    status: ExtractStatus
    doc_type: DocumentType | None = None
    reason: str | None = None
    coverage_pct: float | None = None
    rejected_path: Path | None = None
    sidecar: dict[str, Any] | None = None  # populated for status=="extract"


@dataclass(frozen=True)
class ExtractContext:
    """Read-only context passed to per-type extractors.

    `body_text` is post-frontmatter; all char offsets are 0-indexed into it.
    `content_sha` is sha256 of the body bytes truncated to 12 hex chars
    (SourceSpan.content_sha for every emitted record).
    """

    body_text: str
    line_offsets: list[int]
    content_sha: str
    meta: EnvelopeMeta
    frontmatter: dict[str, Any] = field(default_factory=dict)

    def lines_for(self, chars: tuple[int, int]) -> tuple[int, int]:
        return lines_for_range(chars, self.line_offsets)

    def make_source_span(self, chars: tuple[int, int]) -> dict[str, Any]:
        lr = self.lines_for(chars)
        return {
            "chars": [chars[0], chars[1]],
            "lines": [lr[0], lr[1]],
            "content_sha": self.content_sha,
        }


def _sidecar_path_for(md_path: Path) -> Path:
    """`<basename>.extraction.json` next to the MD.

    `Path.with_suffix` rejects multi-dot suffixes, hence the manual stem
    composition.
    """
    return md_path.parent / f"{md_path.stem}.extraction.json"


def _rejected_path_for(md_path: Path) -> Path:
    return md_path.parent / f"{md_path.stem}.rejected.json"


def _versions_current(cached: dict[str, Any], doc_type: DocumentType) -> bool:
    """Compare a cached sidecar's versions to the current code.

    Returns True iff every key in the cache's `extractor_versions` matches
    the runtime, AND the runtime carries no extra keys the cache lacks.
    Schema_version mismatch is also a re-run trigger.
    """
    if cached.get("schema_version") != SCHEMA_VERSION:
        return False
    cached_versions = (cached.get("extraction") or {}).get("extractor_versions") or {}
    expected = _shared_helper_versions()
    expected[doc_type] = EXTRACTOR_VERSIONS.get(doc_type, "0.0.0")
    return cached_versions == expected


def _read_cached_sidecar(p: Path) -> dict[str, Any] | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _build_envelope(
    *,
    doc_type: DocumentType,
    meta: EnvelopeMeta,
    md_path: Path,
    pdf_path: Path,
    content_sha: str,
    confidence: float,
) -> dict[str, Any]:
    versions = _shared_helper_versions()
    versions[doc_type] = EXTRACTOR_VERSIONS.get(doc_type, "0.0.0")
    return {
        "schema_version": SCHEMA_VERSION,
        "document_id": document_id(meta),
        "content_sha": content_sha,
        "document_type": doc_type,
        "metadata": meta.to_metadata_dict(),
        "raw_markdown_path": str(md_path),
        "raw_pdf_path": str(pdf_path),
        "extraction": {
            "extractor": EXTRACTOR_LABEL,
            "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "extractor_versions": versions,
            "confidence": round(confidence, 4),
        },
    }


def _pdf_path_for(md_path: Path) -> Path:
    return md_path.parent / f"{md_path.stem}.pdf"


def _aggregate_confidence(body_dict: dict[str, Any]) -> float:
    """Mean of per-section confidences, or 1.0 when nothing extracted.

    Scans every body shape's record arrays: qr's `questions`, plenary's
    `agenda_items` (+ nested `activities[]`) + `interpellations`,
    committee_synthesis's `committees[].meetings[].agenda[]`.
    """
    scores: list[float] = []

    def _maybe_score(d: dict[str, Any]) -> None:
        ext = d.get("extraction") or {}
        c = ext.get("confidence")
        if isinstance(c, (int, float)):
            scores.append(float(c))

    for q in body_dict.get("questions", []) or []:
        _maybe_score(q)
    for item in body_dict.get("agenda_items", []) or []:
        _maybe_score(item)
        for act in item.get("activities", []) or []:
            _maybe_score(act)
    for interp in body_dict.get("interpellations", []) or []:
        _maybe_score(interp)
    for committee in body_dict.get("committees", []) or []:
        _maybe_score(committee)
        for meeting in committee.get("meetings", []) or []:
            for ag in meeting.get("agenda", []) or []:
                _maybe_score(ag)

    if not scores:
        return 1.0
    return sum(scores) / len(scores)


def _write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def extract(
    md_path: Path,
    *,
    force: bool = False,
    override_type: DocumentType | None = None,
    write: bool = True,
) -> ExtractResult:
    """Extract `md_path` → write sidecar + return ExtractResult.

    `force=True` re-runs even when versions match. `override_type` bypasses
    the classifier (use sparingly — only when classify is wrong on a
    specific doc). `write=False` runs the full pipeline but doesn't touch
    disk — useful in tests.
    """
    sidecar_path = _sidecar_path_for(md_path)

    if not md_path.exists():
        return ExtractResult(
            md_path=md_path,
            sidecar_path=sidecar_path,
            status="error",
            reason=f"missing MD: {md_path}",
        )

    text = md_path.read_text(encoding="utf-8")
    fm_dict, body = split_md(text)
    try:
        meta = envelope_meta_from_frontmatter(fm_dict)
    except ValueError as exc:
        return ExtractResult(
            md_path=md_path,
            sidecar_path=sidecar_path,
            status="error",
            reason=str(exc),
        )

    # Classify
    if override_type is not None:
        doc_type: DocumentType = override_type
    else:
        suffix = parse_issue_suffix(md_path.name)
        cr = classify(body[:HEADER_WINDOW_BYTES], suffix)
        doc_type = cr.top_type

    if doc_type not in EXTRACTORS:
        return ExtractResult(
            md_path=md_path,
            sidecar_path=sidecar_path,
            status="skip",
            doc_type=doc_type,
            reason=f"no extractor for {doc_type}",
        )

    # Version-aware idempotency
    if not force and write and sidecar_path.exists():
        cached = _read_cached_sidecar(sidecar_path)
        if cached is not None and _versions_current(cached, doc_type):
            cov = (cached.get("coverage") or {}).get("claimed_pct")
            return ExtractResult(
                md_path=md_path,
                sidecar_path=sidecar_path,
                status="skip",
                doc_type=doc_type,
                reason="versions match",
                coverage_pct=float(cov) if isinstance(cov, (int, float)) else None,
            )

    # Build context + run extractor
    content_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    ctx = ExtractContext(
        body_text=body,
        line_offsets=line_offsets(body),
        content_sha=content_sha,
        meta=meta,
        frontmatter=fm_dict,
    )
    extract_fn = EXTRACTORS[doc_type]
    try:
        body_dict, claims = extract_fn(ctx)
    except Exception as exc:
        return ExtractResult(
            md_path=md_path,
            sidecar_path=sidecar_path,
            status="error",
            doc_type=doc_type,
            reason=f"extractor raised: {exc!r}",
        )

    # Augment with shared boilerplate
    boilerplate_claims: list[Claim] = claim_shared_boilerplate(body)
    all_claims: list[Claim] = list(claims) + boilerplate_claims

    coverage = compute_coverage(body, all_claims)
    envelope = _build_envelope(
        doc_type=doc_type,
        meta=meta,
        md_path=md_path,
        pdf_path=_pdf_path_for(md_path),
        content_sha=content_sha,
        confidence=_aggregate_confidence(body_dict),
    )
    sidecar = {**envelope, "coverage": coverage, "body": body_dict}

    try:
        validate(sidecar)
    except SchemaError as exc:
        rejected_path = _rejected_path_for(md_path)
        if write:
            _write_atomic(
                rejected_path,
                json.dumps(sidecar, indent=2, ensure_ascii=False, default=str),
            )
        return ExtractResult(
            md_path=md_path,
            sidecar_path=sidecar_path,
            status="error",
            doc_type=doc_type,
            reason=f"schema validation failed: {exc.errors[0]}",
            rejected_path=rejected_path if write else None,
        )

    if write:
        _write_atomic(
            sidecar_path,
            json.dumps(sidecar, indent=2, ensure_ascii=False, default=str),
        )

    return ExtractResult(
        md_path=md_path,
        sidecar_path=sidecar_path,
        status="extract",
        doc_type=doc_type,
        coverage_pct=coverage["claimed_pct"],
        sidecar=sidecar,
    )
