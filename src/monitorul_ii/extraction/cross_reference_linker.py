"""Cross-reference linker — within-document anchor join for `art. N` unknowns.

Sister to `linker.py` (which joins across documents). Same write contract:
in-memory pass over each sidecar, atomic write via `.part` rename,
pre-write schema validation, idempotent skip when already populated.
The difference is the join key: this pass is intra-document, anchor-to-
unknown over a body span.

# Why this exists

`references.py` v0.4.0+ emits `UnknownReference` entries with
`hint="law-ish"` for cite-shaped spans that don't classify into a strict
variant. The dominant unclassified bucket on the 5,551-doc production
corpus is bare `art. N` cross-references — phrases like
"art. 25 alin. (3)" or "articolul 14" — that lack an issuer (no `Legea`,
no `Codul`, no `OUG` next to them). On their own these are useless; in
context they refer to articles of a law/code/bill cited elsewhere in the
same parent (same agenda title, same speech body).

This linker walks every plenary stenogram + plenary joint session
sidecar, finds every reference list (`primary_references` /
`references_mentioned`) and resolves each art-N unknown WITHIN ITS LIST
to the nearest preceding non-unknown reference. Schema 1.12.0+ permits
the additive `resolved_to` field (carrying the anchor's char_offsets).

# Design calls (locked v0.1.0)

## (1) Output shape — additive, not graduating

The linker keeps the unknown's existing `type=unknown` shape and adds a
`resolved_to` pointer at its anchor. Reasons against graduating into a
fully-typed reference (e.g. promoting `art. N` near `Legea 95/2006` into
a typed law-with-articles ref): the article number isn't on the original
strict variant's schema, so graduation would require its own schema
extension (article list on every variant); and a stale or wrong link
shouldn't corrupt the strict-variant pool — keeping unknowns as unknowns
preserves the "uncommitted" semantics that downstream consumers can read.

The cost is one extra hop: consumers join `unknown.resolved_to` → anchor
ref's char_offsets → matching strict-variant ref in the same list. The
hop is cheap (a single linear scan over the parent list).

## (2) Scoping rule — same-list only

Each reference list (`agenda_items[].primary_references` /
`agenda_items[].activities[].references_mentioned`) is its own
coordinate system. Offsets stored on each ref are LOCAL to the parent
string the ref was extracted from (agenda title or speech text), NOT
global body offsets. The references parsers in `references.py`
(`parse_primary_references`, `parse_mentioned_references`) are called by
extractors WITHOUT a `base_offset` argument — that's the contract — so
their output offsets index into the local parent string.

Cross-list resolution (e.g., agenda's bill-cite anchoring an article in
a speech inside that agenda) would require body-global coordinates, an
agenda-aggregation index, OR re-parsing the parent text. Out of scope
for v0.1; same-list is the precision floor.

A v0.2 future-work candidate: walk each agenda_item, build a per-agenda
anchor pool from its `primary_references[]` PLUS any speech bill cites,
and resolve speech-level art-N unknowns into that pool. That's a
structural join, not a regex change — see "Known caveats" below.

# Production results (v0.1.0, 5551-doc corpus)

- Touched (linked + cleared): 3,629 / 5,551 sidecars (65.4%)
- art-N unknowns total:        110,453
- Resolved with anchor:         20,433 (18.5%)
- Unresolved (no same-list anchor): 90,020 (81.5%)
- Errors: 0
- Spot-check precision (random 20-sample, manual review): ~85% (17/20)

The 81.5% unresolved fraction is dominated by cross-list cases — speech-
level art-N unknowns whose owning bill cite lives in the parent agenda's
`primary_references[]`, not in the same speech's `references_mentioned`.
Resolving those needs the agenda-aggregation join sketched above (v0.2
future work). The remaining unresolved bucket is true no-anchor cases —
SUMAR-area citations whose law cite never appeared in the same parent.

Initial design iterations tried body-text-based scoping (sentence /
paragraph / activity scope, computed against the MD body). Two problems
killed precision: (1) pymupdf4llm's markdown uses `<br>` and `|` for
SUMAR table cells, so `\\n\\n`-only paragraph detection treated the
whole SUMAR as one paragraph and cross-anchored unrelated rows; (2) the
references parsers emit LOCAL offsets, but the linker was treating them
as body-global — every comparison was nonsensical. Same-list scoping
sidesteps both: refs in a single list share a coordinate system by
construction, and table boundaries are irrelevant since each list lives
inside one cell. Idempotency was verified on the corpus (second run
yields 0 new resolutions).

## (3) Anchor priority — most-recent-preceding, with code-over-law tie-break

Within a list, when multiple eligible anchors precede the unknown, the
**most-recent-preceding** wins (closest to the unknown's start by char
offset, scanning leftward). Rationale: parliamentary discourse moves
topic-by-topic; the closest cite is overwhelmingly the one being
discussed.

At a strict tie (anchor A and anchor B both end at the same offset),
prefer `code` over `law` over `bill` over everything else. Codes (Codul
muncii, Codul fiscal, Codul penal) are conventionally cited once and
then referenced repeatedly via bare `art. N`; named laws follow the
same pattern but are more often re-cited inline. The tie is rare — most
references end at distinct offsets.

## (4) Document-type scoping — plenary only

Plenary stenograms and plenary joint sessions only. The qr / committee /
report_facsimile body shapes don't carry `primary_references` or
`references_mentioned` slots in the schema, so the art-N unknown bucket
is entirely concentrated in plenary stenograms (agenda_items[].
primary_references and activities[].references_mentioned on
SpeechActivity).

# Idempotency contract

Sidecars are skipped when every unknown art-N reference already carries
a `resolved_to` value (or has been considered and got null because no
anchor was findable). `force=True` re-resolves all unknowns regardless,
including clearing stale `resolved_to` values when the new run finds no
anchor under the current rules. This makes `--force` the canonical
"re-resolve under current scope" command.

# Versioning

`XREF_LINKER_VERSION = "0.1.0"`. NOT included in `extractor_versions` —
same Q11 contract as the vote-pair linker. Re-extracting a sidecar
clobbers `resolved_to` (extractor doesn't know about it); re-running
`monitorul-ii link --xref-only` recovers (linking is fast — a single
walk per sidecar, no MD body access).

The linker DOES bump the sidecar's schema_version to the runtime
version (1.12.0) on every successful write. The new schema is fully
backwards-compatible with 1.11.0 — the only delta is the additive
`resolved_to` slot — so the bump is honest "this sidecar is now schema
1.12.0".

# Known caveats

1. Does not resolve cross-list references. If an unknown art-N is in
   speech S1 and its anchor is in speech S2 (a different speech in the
   same agenda), or if the anchor is in the agenda's primary_references
   but the unknown is in a child speech's references_mentioned, the
   linker doesn't fire. Out of scope for v0.1; a future "agenda anchor"
   rule would fan an agenda's bill cite out to speeches under it.
2. Does not detect anchor relevance — within a list, if the most-
   recent-preceding anchor is `Legea 47/1992` but the speaker is
   actually citing `Constituția art. 14`, the linker links to the
   wrong anchor. Spot-check precision is the headline acceptance gate.
3. Does not handle forward references (`art. 14 al legii care va fi
   adoptată` — common in committee debates). Forward refs stay null.
4. Per-list ref offsets ARE local; `resolved_to.char_offsets` is also
   the LOCAL offsets of the anchor (within the same parent string),
   NOT body-global. Downstream consumers should look up the anchor by
   char_offsets within the SAME `primary_references` /
   `references_mentioned` list as the unknown.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from monitorul_ii.extraction.pipeline import SCHEMA_VERSION
from monitorul_ii.extraction.schema import SchemaError, validate

XREF_LINKER_VERSION = "0.1.0"


XrefLinkStatus = Literal["linked", "skip", "error"]


@dataclass(frozen=True)
class XrefLinkResult:
    """One row per sidecar processed by the cross-reference linker pass."""

    sidecar_path: Path
    status: XrefLinkStatus
    resolved: int = 0  # unknowns whose resolved_to got populated this run
    unresolved: int = 0  # unknowns inspected but with no anchor in scope
    skipped_already: int = 0  # unknowns skipped because resolved_to was already set
    cleared_stale: int = 0  # force-mode: prior resolved_to dropped when no anchor
    reason: str | None = None


# Document types this pass walks. The qr / committee / report_facsimile
# body shapes don't carry `primary_references` or `references_mentioned`
# slots in the schema, so the art-N unknown bucket is entirely concentrated
# in plenary stenograms (agenda_items[].primary_references and
# activities[].references_mentioned on SpeechActivity).
_LINKABLE_DOC_TYPES = (
    "plenary_stenogram",
    "plenary_joint_session",
)


# `art. N` / `Art. N` / `articolul N` — the cite shape we resolve. Mirrors
# the regex that `references.py` `_UNKNOWN_EMIT_PATTERNS` uses to emit the
# unknown in the first place.
_ART_N_RE = re.compile(r"\bart(?:icol)?\.?\s*\d+", re.IGNORECASE)


# Tie-break ranking when multiple anchors end at the same offset. Lower
# rank wins. Codes are most-cited-once-then-bare-art-N; laws follow but
# are often re-cited inline; bills are usually self-contained. Anything
# else falls to the bottom.
_ANCHOR_TIE_BREAK = {
    "code": 0,
    "law": 1,
    "bill": 2,
    "oug": 3,
    "og": 4,
    "regulation": 5,
    "constitution": 6,
    "chamber_resolution": 7,
    "parliamentary_resolution": 8,
    "treaty": 9,
    "court_decision": 10,
    "eu_doc": 11,
    "motion": 12,
}


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


def _walk_ref_lists(node: Any) -> Iterator[list[dict[str, Any]]]:
    """Yield every list found under `primary_references` / `references_mentioned`.

    Each yielded list is the actual list mutable in-place — the linker
    relies on that for resolved_to writes.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("primary_references", "references_mentioned") and isinstance(
                value, list
            ):
                yield value
            else:
                yield from _walk_ref_lists(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_ref_lists(item)


def _is_art_n_unknown(ref: dict[str, Any]) -> bool:
    """Match the linker's narrow target shape: `type=unknown` AND raw matches
    art-N regex. Skips other unknowns (HG / Decret / Decizia ICCJ — the
    cite-shape detector emits these too)."""
    if ref.get("type") != "unknown":
        return False
    raw = ref.get("raw") or ""
    return bool(_ART_N_RE.search(raw))


@dataclass(frozen=True)
class _Anchor:
    start: int
    end: int
    type: str


def _collect_anchors_in_list(ref_list: list[dict[str, Any]]) -> list[_Anchor]:
    """Return every non-unknown reference in a list with valid char_offsets,
    sorted by start. The collected list is the candidate pool for anchor
    lookup of unknowns in the SAME list.
    """
    anchors: list[_Anchor] = []
    for ref in ref_list:
        if not isinstance(ref, dict):
            continue
        if ref.get("type") == "unknown":
            continue
        offsets = ref.get("char_offsets")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or not all(isinstance(x, int) for x in offsets)
        ):
            continue
        anchors.append(
            _Anchor(
                start=offsets[0],
                end=offsets[1],
                type=ref.get("type") or "",
            )
        )
    anchors.sort(key=lambda a: a.start)
    return anchors


def _find_anchor(unknown_start: int, anchors: list[_Anchor]) -> _Anchor | None:
    """Pick the best anchor for an art-N unknown at `unknown_start`.

    Strategy (same-list scoping, see module docstring):
      1. Consider only anchors with end <= unknown_start (preceding).
      2. The **closest** preceding anchor wins (largest end offset).
      3. Tie-break (equal end offset): _ANCHOR_TIE_BREAK rank lower wins.

    Returns None when no preceding anchor exists in the list — the
    unknown stays unresolved.
    """
    candidates = [a for a in anchors if a.end <= unknown_start]
    if not candidates:
        return None
    # Closest preceding wins (max end). Tie-break on type rank (lower
    # better). Use stable sort so equal-end-equal-rank ties favor first-
    # encountered (left-to-right document order).
    candidates_sorted = sorted(
        candidates,
        key=lambda a: (
            -a.end,
            _ANCHOR_TIE_BREAK.get(a.type, 99),
        ),
    )
    return candidates_sorted[0]


def link_xrefs(
    sidecar_path: Path,
    *,
    force: bool = False,
    write: bool = True,
) -> XrefLinkResult:
    """Resolve every art-N unknown in one sidecar to its same-list anchor.

    Walks every reference list under `body` (any `primary_references` or
    `references_mentioned` array, recursively); for each art-N unknown
    in a list, finds the most-recent preceding non-unknown anchor in
    THE SAME LIST and writes the anchor's char_offsets into the
    unknown's `resolved_to`. Pre-write schema validation guards shape
    regressions; atomic write via `.part` rename. Idempotent: already-
    resolved unknowns skip unless force=True.

    Returns an XrefLinkResult counting how many resolutions were written
    this run, how many were skipped (already populated), how many were
    inspected but had no anchor in scope, and (under force) how many
    stale resolved_to values were cleared.
    """
    sc = _read_sidecar(sidecar_path)
    if sc is None:
        return XrefLinkResult(
            sidecar_path=sidecar_path,
            status="error",
            reason="failed to read sidecar JSON",
        )
    if sc.get("document_type") not in _LINKABLE_DOC_TYPES:
        return XrefLinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="not a linkable doc type",
        )
    body = sc.get("body")
    if not isinstance(body, dict):
        return XrefLinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="missing body",
        )
    ref_lists = list(_walk_ref_lists(body))
    if not ref_lists:
        return XrefLinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="no reference lists in body",
        )

    resolved = 0
    unresolved = 0
    skipped_already = 0
    cleared_stale = 0

    for ref_list in ref_lists:
        anchors = _collect_anchors_in_list(ref_list)
        for ref in ref_list:
            if not isinstance(ref, dict):
                continue
            if not _is_art_n_unknown(ref):
                continue
            existing = ref.get("resolved_to")
            if existing is not None and not force:
                skipped_already += 1
                continue
            offsets = ref.get("char_offsets")
            if (
                not isinstance(offsets, list)
                or len(offsets) != 2
                or not isinstance(offsets[0], int)
            ):
                continue
            unknown_start = offsets[0]
            anchor = _find_anchor(unknown_start, anchors)
            if anchor is None:
                # Honest null: leave resolved_to unset (or null) so a
                # future linker bump can retry.
                #
                # Exception: when force=True AND a stale resolved_to
                # already sits on the ref, clear it. This makes
                # force=True the canonical "re-resolve under current
                # rules" command — without it, a tightening of the
                # linker rules leaves stale links alone and the only
                # way to clean them up is a manual sidecar rewrite.
                if existing is not None and force:
                    ref["resolved_to"] = None
                    cleared_stale += 1
                unresolved += 1
                continue
            ref["resolved_to"] = {
                "char_offsets": [anchor.start, anchor.end],
            }
            resolved += 1

    if resolved == 0 and cleared_stale == 0:
        # No writes — skip schema validate + disk write to keep the
        # corpus pass fast.
        return XrefLinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="no resolutions",
            unresolved=unresolved,
            skipped_already=skipped_already,
        )
    # Bump the sidecar's schema_version to the runtime version. The xref
    # linker writes the additive `resolved_to` field that the pre-1.12.0
    # schema doesn't permit; bumping forward is safe because the new
    # schema is fully backwards-compatible (only one optional field was
    # added). Older sidecars on disk that haven't been re-extracted yet
    # gain the new fields via the linker pass — without this bump,
    # pre-write validation would reject them.
    sc["schema_version"] = SCHEMA_VERSION
    try:
        validate(sc)
    except SchemaError as exc:
        return XrefLinkResult(
            sidecar_path=sidecar_path,
            status="error",
            reason=f"schema validation failed after link: {exc.errors[0]}",
        )
    if write:
        _write_atomic(
            sidecar_path,
            json.dumps(sc, indent=2, ensure_ascii=False),
        )
    return XrefLinkResult(
        sidecar_path=sidecar_path,
        status="linked",
        resolved=resolved,
        unresolved=unresolved,
        skipped_already=skipped_already,
        cleared_stale=cleared_stale,
    )


def link_all_xrefs(
    sidecars: Iterable[Path],
    *,
    force: bool = False,
    write: bool = True,
) -> Iterator[XrefLinkResult]:
    """Walk a list of sidecars, run the xref pass on each linkable type."""
    for path in sidecars:
        sc = _read_sidecar(path)
        if sc is None:
            continue
        if sc.get("document_type") not in _LINKABLE_DOC_TYPES:
            continue
        yield link_xrefs(path, force=force, write=write)


__all__ = [
    "XREF_LINKER_VERSION",
    "XrefLinkResult",
    "link_all_xrefs",
    "link_xrefs",
]
