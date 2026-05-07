"""Playback verifier — checks that every per-doc child record (speech,
vote, agenda item, interpellation, question, committee meeting) projected
to Elasticsearch corresponds 1:1 to a real piece of content in the source
markdown body, in source order, attributed to the right speaker.

Three layers of checks:

  A. Hard correctness (positions in range, monotonic, dup-free, count parity,
     ES↔sidecar child-set parity).
  B. Per-record content correctness (speech header at speech position,
     agenda title in SUMAR, interpellation questioner near position, etc.).
  C. MD↔sidecar provenance (every body speaker-header is claimed by an
     activity; every sidecar speech text appears at its declared span).

Encoded as expected-not-bugs (the three "false alarms" from the prompt):
  - speech continuations after narrator/vote/procedural events;
  - agenda titles living in the SUMAR table at top of doc, not at the
    agenda's body position;
  - the literal `<chair narration>` speaker name (no header search).

Module-level entry points:
  - `verify_doc(md_path, *, sidecar=None, es=None, check_es=True) -> DocResult`
  - `iter_md_paths(roots) -> Iterator[Path]`
  - `main()` — CLI runner with ProcessPoolExecutor parallelism.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Lazy-loaded ES client per worker; never share across processes.
_ES_CLIENT: Any | None = None


# ---- data types ----------------------------------------------------------


@dataclass
class Issue:
    """One verification finding."""

    kind: str
    rid: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.rid is not None:
            out["rid"] = self.rid
        out.update(self.detail)
        return out


@dataclass
class DocResult:
    doc_id: str
    md_path: str
    issues: list[Issue] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_jsonl(self) -> str:
        out: dict[str, Any] = {
            "doc_id": self.doc_id,
            "md_path": self.md_path,
            "issues": [i.to_dict() for i in self.issues],
            "stats": self.stats,
        }
        if self.error:
            out["error"] = self.error
        return json.dumps(out, ensure_ascii=False)


# ---- patterns ------------------------------------------------------------

# Speaker headers — the union of every shape the plenary corpus uses.
# Each pattern carries a `name` (the inner human-name span). Source-order
# scan: we try patterns in priority order at every line start, picking
# the first that matches. Anchors are MULTILINE start-of-line.
#
# Variant 1: `## **NAME[:]**` — modern canonical, with optional trailing
# colon inside or outside the bold.
_HDR_HASH_PLAIN = re.compile(
    r"^##\s+\*\*\s*(?P<name>[^*\n]+?)\s*\*\*\s*:?\s*$",
    re.MULTILINE,
)
# Variant 2: `## **NAME** – _role_ **:**` — modern canonical with role.
_HDR_HASH_ROLE = re.compile(
    r"^##\s+\*\*\s*(?P<name>[^*\n]+?)\s*\*\*\s*[–\-]\s*_[^_\n]+_\s*\*\*:\*\*\s*$",
    re.MULTILINE,
)
# Variant 3: `**NAME** – _role_ **:**` — NO `## ` prefix. Bug-source for
# round 1; ministers / PM / presidents / foreign dignitaries appear here.
_HDR_NOHASH_ROLE = re.compile(
    r"^\*\*\s*(?P<name>[^*\n]+?)\s*\*\*\s*[–\-]\s*_[^_\n]+_\s*\*\*:\*\*\s*$",
    re.MULTILINE,
)
# Variant 4: `**NAME** – _role_:` — same as 3 but trailing plain colon.
_HDR_NOHASH_ROLE_PLAIN_COLON = re.compile(
    r"^\*\*\s*(?P<name>[^*\n]+?)\s*\*\*\s*[–\-]\s*_[^_\n]+_\s*:\s*$",
    re.MULTILINE,
)

# Combined — try in priority order. The hash variants are tried first
# because they are the canonical shape and the no-hash patterns would
# otherwise also match (a line starting with `## **X**` also starts with
# `**X**` after a `## ` prefix that the no-hash pattern doesn't anchor
# against). We anchor each at line start to avoid that.
_SPEAKER_HEADER_PATTERNS: tuple[re.Pattern[str], ...] = (
    _HDR_HASH_ROLE,
    _HDR_HASH_PLAIN,
    _HDR_NOHASH_ROLE,
    _HDR_NOHASH_ROLE_PLAIN_COLON,
)


# Vote-opening phrases — mirrors `extractors/plenary/votes.py`'s
# `_VOTE_OPEN_PATTERNS` plus the chair-narrative `Cine este pentru…?`
# triplet pattern that the actual vote detector picks up via state
# machine but appears at vote position.
_VOTE_OPEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bSupun(?:em)?\s+votului\b", re.IGNORECASE),
    re.compile(
        r"\bV[ăa]\s+rog\s+s[ăa]\s+v[ăa]\s+preg[ăa]ti[țt]i\s+(?:de|pentru)\s+vot\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bS[ăa]\s+înceap[ăa]\s+votul\b", re.IGNORECASE),
    re.compile(r"\bTrecem\s+la\s+vot\b", re.IGNORECASE),
    re.compile(r"\bV[ăa]\s+rog\s+s[ăa]\s+vota[țt]i\b", re.IGNORECASE),
    re.compile(r"\bCine\s+este\s+pentru\b", re.IGNORECASE),
    re.compile(r"\bCine\s+(?:e|este)\s+împotriv[ăa]\b", re.IGNORECASE),
    re.compile(r"\bCine\s+se\s+ab[țt]ine\b", re.IGNORECASE),
)


# SUMAR table region — used for agenda title verification. Matches the
# canonical `## SUMAR` heading, the bare `SUMAR` token, and the pipe-
# prefixed `|SUMAR<br>` form (markdown-converted single-cell table).
_SUMAR_OPENING_RE = re.compile(r"^(?:##\s+)?(?:\|\s*)?SUMAR\b", re.MULTILINE)
# End of SUMAR — the agenda body begins at the first `## **`-prefixed
# line, the genre marker `(STENOGRAMA)` / `## **DEZBATERI`, or a horizontal
# rule following the SUMAR table. We look for the first speaker header
# or known boilerplate line as the SUMAR closer.
_SUMAR_END_RE = re.compile(
    r"(?:^##\s+\*\*[^|]|^\*\*\s*Ședin[țta]a[^*]+\*\*|^##\s+\*\*Ședin[țta]a)",
    re.MULTILINE,
)


# Honorific + role peels for name normalization. Keep loose — we're
# comparing for token overlap, not exact equality.
_HONORIFIC_RE = re.compile(
    r"\b(?:domnul|doamna|domnișoara|domnisoara|dl\.?|dna\.?)\s+",
    re.IGNORECASE,
)
_ROLE_PREFIX_RE = re.compile(
    r"\b(?:deputat(?:ul)?|senator(?:ul|oare)?|"
    r"ministru(?:l|lui)?|secretar(?:ul|i|ilor)?(?:\s+de\s+stat)?|"
    r"pre[șs]edinte(?:le|lui)?|vicepre[șs]edinte(?:le|lui)?|"
    r"viceprim-?ministru(?:l)?|prim-?ministru(?:l)?)\s+",
    re.IGNORECASE,
)
# Trailing comma-clause (role appended) — `Domnul X, președintele Senatului`.
_TRAILING_ROLE_RE = re.compile(r"\s*,.*$")


# Plenary-specific boilerplate that visually resembles a speaker header
# (`## **TEXT**`) but is structural section markup. The boilerplate
# claimer in extractors/plenary/boilerplate.py + session.py covers these
# at extraction time; the verifier must skip them too so dropped_turn
# false-positives don't pile up.
# `[ȘŞSª™]` covers Ș (modern), Ş (cedilla), S (stripped), ª (PostScript
# substitute), ™ (PostScript substitute). `[țţtþ]` covers ț (modern),
# ţ (cedilla), t (stripped), þ (PostScript substitute). `[ȚŢTÞ]` is
# the uppercase equivalent. These mojibake folds appear in 2000-2008
# era PDFs where the converter substituted Postscript glyphs for
# Romanian characters.
# `[ȘŞSª™�]` covers Ș (modern), Ş (cedilla), S (stripped), ª (PostScript
# substitute), ™ (PostScript substitute), � (Unicode replacement when the
# glyph couldn't be decoded). `[țţtþ]` covers ț (modern), ţ (cedilla),
# t (stripped), þ (PostScript substitute). `[ȚŢTÞ]` is the uppercase
# equivalent. `fi` is the ligature glyph that 2002-2008 PostScript-
# converted PDFs use in place of `ț` / `Ț`. These mojibake folds appear
# in old PDFs where the converter substituted Postscript glyphs for
# Romanian characters.
# `[ȘŞSª™�]` covers Ș (modern), Ş (cedilla), S (stripped), ª (PostScript
# substitute), ™ (PostScript substitute), � (Unicode replacement when
# the glyph couldn't be decoded). `[țţtþ]` covers ț (modern), ţ
# (cedilla), t (stripped), þ (PostScript substitute). `[ȚŢTÞ]` is the
# uppercase equivalent. `fi` is the ligature glyph that 2002-2008
# PostScript-converted PDFs use in place of `ț` / `Ț`.
#
# We deliberately omit leading `\b` from the alternation: `\b` requires
# a word-character on one side, but the mojibake substitute chars
# (`™`, `�`) are non-word chars, so `\b` fails when the match starts
# at the beginning of an all-non-word run. `_is_boilerplate_header`
# uses `re.search` so the alternation can land anywhere in the string.
_BOILERPLATE_HEADER_RE = re.compile(
    r"(?:"
    r"PARTEA|PA\s*R\s*T\s*E\s*A|"
    r"DEZBATERI\s+PARLAMENTARE|"
    r"(?:[ȘŞSª™�]|fi)EDIN[ȚŢTÞfi]+E\s+COMUNE|"
    r"[ȘŞSª™�]edin[țţtþfi]+a\s+(?:din\s+ziua|comun[ăaã])|"
    r"[ȘŞSª™�]edin[țţtþfi]+a\s+(?:a\s+început|începe)|"
    r"SESIUNEA|SUMAR|"
    r"STENOGRAMA|"
    r"CAMERA\s+DEPUTA[ȚŢTÞfi]+ILOR|SENATUL|"
    r"EDITOR|A\s*B\s*O\s*N\s*A\s*M\s*E\s*N\s*T\s*E|"
    r"MONITORUL\s+OFICIAL|ISSN|"
    r"RAPOARTE\s+DE\s+ACTIVITATE|"
    r"LISTA\s+ÎNTREB[ĂA]RILOR|LISTA\s+ÎNTREB[ÃA]RILOR|"
    r"LISTA\s+INTREBARILOR|"
    r"SINTEZA\s+LUCR[ĂÃA]RILOR"
    r")",
    re.IGNORECASE,
)


def _is_boilerplate_header(name: str) -> bool:
    """Return True when a captured header `name` is a plenary section
    marker, not a real speaker header. Filter applied to MD-coverage
    enumeration so the boilerplate isn't double-counted as a dropped turn.
    """
    if _BOILERPLATE_HEADER_RE.search(name):
        return True
    # Detect old-PDF spaced-letter section markers: `A B O N A M E N T E`,
    # `C U P R I N S`, `R A P O R T U L`, `L I S T A`, `P R E Þ U R I L E`,
    # `M O Þ I U N E`, `P R O G R A M U L`, etc. (the 2000-2002 PostScript-
    # converter padded section banners with one space between every
    # letter). Heuristic: ≥ 3 single-letter tokens separated by single
    # spaces, and overall mostly uppercase or PostScript-mojibake glyphs.
    tokens = name.split()
    if len(tokens) >= 3:
        single_letter = sum(1 for t in tokens if len(t) == 1)
        if single_letter >= max(3, len(tokens) // 2):
            return True
    return False


# ---- helpers -------------------------------------------------------------


def _strip_diacritics(s: str) -> str:
    """NFKD strip, lowercase. Mirrors the persons matcher's strict tier."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _peel_name(raw: str) -> str:
    """Strip honorifics, role prefixes, and trailing role clauses. Return
    the bare name string for token-overlap matching.
    """
    s = raw.strip()
    # Strip leading honorific + optional rank, repeating because a header
    # may stack `Domnul deputat NAME` (rare, but observed).
    for _ in range(3):
        m = _HONORIFIC_RE.match(s)
        if m:
            s = s[m.end() :].strip()
            continue
        m = _ROLE_PREFIX_RE.match(s)
        if m:
            s = s[m.end() :].strip()
            continue
        break
    # Strip trailing role clause.
    s = _TRAILING_ROLE_RE.sub("", s).strip()
    # Strip trailing punctuation that the regex capture sometimes pulls
    # through when the colon sits inside the bold delimiters
    # (`## **NAME:**` → captured as `NAME:`); without this, a single-
    # token name like "Speaker:" fails the token-overlap check against
    # the canonical "Speaker" form.
    s = s.rstrip(":;,.()")
    return s


def _name_tokens(name: str) -> set[str]:
    """Tokenize a name for overlap matching, accent-insensitive."""
    folded = _strip_diacritics(_peel_name(name))
    # Whitespace + hyphen split — `Sorin-Mihai` ↔ `Sorin Mihai`.
    parts = re.split(r"[\s\-]+", folded)
    return {p for p in parts if p and len(p) >= 2}


def _name_overlap_ratio(a: str, b: str) -> float:
    """Token-set Jaccard-like overlap; >= 0.5 considered a match.
    Returns 0.0 when either side has no tokens.
    """
    ta = _name_tokens(a)
    tb = _name_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    smaller = min(len(ta), len(tb))
    if smaller == 0:
        return 0.0
    return len(inter) / smaller


def split_md(text: str) -> tuple[dict[str, Any], str]:
    """Local copy of envelope.split_md so the verifier doesn't need the
    full project import path (lighter ProcessPoolExecutor startup).
    """
    m = re.match(r"\A---\s*\n(?P<body>.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    return {"_raw_frontmatter": m.group("body")}, text[m.end() :]


def find_sumar_span(body: str) -> tuple[int, int] | None:
    """Locate the SUMAR span (table region at top of doc).

    Returns (start, end) char offsets into body, or None when no SUMAR
    is detected. The end is the position of the first speaker header or
    `## **Ședința...**` line after SUMAR — i.e., where the body content
    actually begins.
    """
    m_open = _SUMAR_OPENING_RE.search(body)
    if not m_open:
        return None
    start = m_open.start()
    # Find end: first speaker header or session-opener after SUMAR.
    m_end = _SUMAR_END_RE.search(body, m_open.end())
    if not m_end:
        # No clear end; bound to 8 KB after open (typical SUMAR is < 4 KB).
        return start, min(len(body), start + 8000)
    return start, m_end.start()


def iter_speaker_headers(body: str) -> Iterator[tuple[int, int, str]]:
    """Yield (start, end, name) for every recognized speaker header in body.

    Patterns are tried in priority order; overlapping matches at the same
    line start are deduplicated via a set of seen `(start, line_end)`
    pairs (the longer-match wins via priority order).
    """
    seen: dict[int, tuple[int, str]] = {}
    for pat in _SPEAKER_HEADER_PATTERNS:
        for m in pat.finditer(body):
            start = m.start()
            if start in seen:
                continue
            end = m.end()
            seen[start] = (end, m.group("name").strip())
    for start in sorted(seen):
        end, name = seen[start]
        yield (start, end, name)


def _has_header_at(
    body: str, position: int, lookahead: int = 200
) -> tuple[str, int] | None:
    """Look for a speaker header starting at or near `position`.

    Allows leading whitespace + newlines (the activity span often starts
    with a few blank lines that lead into the header). Returns
    (matched_name, header_end_position) or None.
    """
    chunk_end = min(len(body), position + lookahead)
    chunk = body[position:chunk_end]
    # Check from the START of the chunk only (the activity should begin
    # at the header line). We allow at most 30 chars of leading whitespace
    # / paragraph break before the header.
    for pat in _SPEAKER_HEADER_PATTERNS:
        m = pat.match(chunk)
        if m:
            return m.group("name").strip(), position + m.end()
        # Also try skipping leading whitespace/newlines (up to 30 chars).
        m2 = re.match(r"^[\s\n]{0,30}", chunk)
        offset = m2.end() if m2 else 0
        if offset > 0 and offset < len(chunk):
            m3 = pat.match(chunk, offset)
            if m3 and m3.start() == offset:
                return m3.group("name").strip(), position + m3.end()
    return None


def _has_vote_open_phrase(body: str, position: int, window: int = 200) -> bool:
    """Whether body[position:position+window] contains any vote-open phrase."""
    end = min(len(body), position + window)
    chunk = body[position:end]
    return any(p.search(chunk) for p in _VOTE_OPEN_PATTERNS)


# ---- check functions -----------------------------------------------------


def check_es_correctness(
    es_hits: list[dict[str, Any]],
    document_doc: dict[str, Any] | None,
    sidecar: dict[str, Any],
    body_len: int,
) -> list[Issue]:
    """Layer A — hard correctness checks against the ES projection."""
    issues: list[Issue] = []
    if not es_hits:
        # Allowed: report_facsimile and tiny docs may have zero per-doc
        # children. We surface this only when sidecar expects > 0.
        sc_child_ids = _sidecar_child_record_ids(sidecar)
        if any(sc_child_ids.values()):
            issues.append(
                Issue(
                    kind="es_empty",
                    detail={
                        "expected_total": sum(len(v) for v in sc_child_ids.values()),
                    },
                )
            )
        return issues

    seen_ids: set[str] = set()
    last_pos: int | None = None
    out_of_range_count = 0
    non_monotonic_count = 0
    for h in es_hits:
        rid = h["_id"]
        if rid in seen_ids:
            issues.append(Issue(kind="dup_record_id", rid=rid))
        seen_ids.add(rid)
        src = h.get("_source") or {}
        pos = src.get("position_in_document")
        if pos is None:
            issues.append(Issue(kind="missing_position", rid=rid))
            continue
        try:
            pos_i = int(pos)
        except (TypeError, ValueError):
            issues.append(
                Issue(
                    kind="missing_position",
                    rid=rid,
                    detail={"raw": str(pos)},
                )
            )
            continue
        if pos_i < 0 or pos_i >= body_len:
            out_of_range_count += 1
            issues.append(
                Issue(
                    kind="position_oor",
                    rid=rid,
                    detail={"position": pos_i, "body_len": body_len},
                )
            )
            continue
        if last_pos is not None and pos_i < last_pos:
            non_monotonic_count += 1
            issues.append(
                Issue(
                    kind="non_monotonic",
                    rid=rid,
                    detail={"position": pos_i, "prev_position": last_pos},
                )
            )
        last_pos = pos_i

    # Count parity: ES per-grain count vs document_doc's announced count.
    if document_doc:
        per_grain: dict[str, int] = {}
        for h in es_hits:
            idx = h.get("_index") or "?"
            per_grain[idx] = per_grain.get(idx, 0) + 1
        expected = {
            "mo-agenda-items": document_doc.get("agenda_count") or 0,
            "mo-speeches": document_doc.get("speech_count") or 0,
            "mo-votes": document_doc.get("vote_count") or 0,
            "mo-interpellations": document_doc.get("interpellation_count") or 0,
            "mo-questions": document_doc.get("question_count") or 0,
        }
        for grain, want in expected.items():
            got = per_grain.get(grain, 0)
            if got != want:
                issues.append(
                    Issue(
                        kind="count_mismatch",
                        detail={"grain": grain, "expected": want, "got": got},
                    )
                )

    # Child-set parity: ES record_ids vs sidecar record_ids.
    sc_child_ids = _sidecar_child_record_ids(sidecar)
    sc_all = {rid for ids in sc_child_ids.values() for rid in ids}
    es_all = {h["_id"] for h in es_hits}
    only_in_sidecar = sc_all - es_all
    only_in_es = es_all - sc_all
    for rid in sorted(only_in_sidecar):
        issues.append(Issue(kind="missing_in_es", rid=rid))
    for rid in sorted(only_in_es):
        issues.append(Issue(kind="orphan_in_es", rid=rid))

    return issues


def _sidecar_child_record_ids(sidecar: dict[str, Any]) -> dict[str, list[str]]:
    """Compute the record_ids the indexer would produce for this sidecar,
    grouped by grain. Mirrors `denormalize.denormalize_sidecar` minus the
    full denormalization (we just need the IDs).
    """
    out: dict[str, list[str]] = {
        "mo-agenda-items": [],
        "mo-speeches": [],
        "mo-votes": [],
        "mo-interpellations": [],
        "mo-questions": [],
        "mo-committee-meetings": [],
    }
    body = sidecar.get("body") or {}
    if not isinstance(body, dict):
        return out
    doc_type = sidecar.get("document_type")
    if doc_type in ("plenary_stenogram", "plenary_joint_session"):
        for ai in body.get("agenda_items") or []:
            if not isinstance(ai, dict):
                continue
            ai_id = ai.get("id")
            if ai_id:
                out["mo-agenda-items"].append(ai_id)
            for act in ai.get("activities") or []:
                if not isinstance(act, dict):
                    continue
                rid = act.get("id")
                if not rid:
                    continue
                if act.get("type") == "speech":
                    out["mo-speeches"].append(rid)
                elif act.get("type") == "vote":
                    out["mo-votes"].append(rid)
        for ix in body.get("interpellations") or []:
            if isinstance(ix, dict) and ix.get("id"):
                out["mo-interpellations"].append(ix["id"])
    elif doc_type == "question_register":
        for q in body.get("questions") or []:
            if isinstance(q, dict) and q.get("id"):
                out["mo-questions"].append(q["id"])
    elif doc_type == "committee_synthesis":
        for committee in body.get("committees") or []:
            if not isinstance(committee, dict):
                continue
            for meeting in committee.get("meetings") or []:
                if isinstance(meeting, dict) and meeting.get("id"):
                    out["mo-committee-meetings"].append(meeting["id"])
    return out


def check_speeches(sidecar: dict[str, Any], body: str) -> list[Issue]:
    """Layer B + D — speech header presence and span text correctness."""
    issues: list[Issue] = []
    body_obj = sidecar.get("body") or {}
    items = body_obj.get("agenda_items") or []
    if not isinstance(items, list):
        return issues
    body_len = len(body)
    for ai in items:
        if not isinstance(ai, dict):
            continue
        activities = ai.get("activities") or []
        if not isinstance(activities, list):
            continue
        # Walk activities in source order so we can detect continuations.
        sorted_acts = sorted(
            (a for a in activities if isinstance(a, dict)),
            key=lambda a: ((a.get("source_span") or {}).get("chars", [0]) + [0])[0],
        )
        prev_act_type: str | None = None
        for act in sorted_acts:
            act_type = act.get("type")
            speaker = (
                act.get("speaker") if isinstance(act.get("speaker"), dict) else None
            )
            speaker_raw = (
                (speaker or {}).get("raw") or (speaker or {}).get("name") or ""
            )
            speaker_name = (speaker or {}).get("name") or speaker_raw
            chars = (act.get("source_span") or {}).get("chars") or []
            position = chars[0] if chars else None
            rid = act.get("id")

            if act_type == "speech":
                if position is None or not isinstance(position, int):
                    issues.append(Issue(kind="span_missing", rid=rid))
                    prev_act_type = act_type
                    continue
                if position < 0 or position >= body_len:
                    issues.append(
                        Issue(
                            kind="position_oor_sidecar",
                            rid=rid,
                            detail={"position": position, "body_len": body_len},
                        )
                    )
                    prev_act_type = act_type
                    continue
                # Exemption: chair narration literal — not searched.
                if speaker_raw.strip() == "<chair narration>":
                    prev_act_type = act_type
                    continue
                # Exemption: continuation after non-speech event.
                is_continuation = prev_act_type in (
                    "narrator",
                    "procedural",
                    "vote",
                    "deferral",
                )
                hdr = _has_header_at(body, position)
                if hdr is not None:
                    matched_name, _hdr_end = hdr
                    # Skip when the captured "name" is a structural marker
                    # (`A B O N A M E N T E`, `R A P O R T U L`, …); these
                    # arise from old-PDF banner conversion and the extractor
                    # also picks them up as speakers — both sides agree on
                    # the boilerplate, but neither side is a real speaker.
                    if _is_boilerplate_header(matched_name):
                        prev_act_type = act_type
                        continue
                    overlap = _name_overlap_ratio(matched_name, speaker_name)
                    if overlap < 0.5:
                        issues.append(
                            Issue(
                                kind="speaker_mismatch",
                                rid=rid,
                                detail={
                                    "attributed": speaker_name,
                                    "in_body": matched_name,
                                    "position": position,
                                    "head": body[position : position + 140].replace(
                                        "\n", " "
                                    ),
                                },
                            )
                        )
                else:
                    if not is_continuation:
                        # No header at speech position and not a continuation.
                        issues.append(
                            Issue(
                                kind="invented_or_misplaced_speech",
                                rid=rid,
                                detail={
                                    "attributed": speaker_name,
                                    "position": position,
                                    "head": body[position : position + 140].replace(
                                        "\n", " "
                                    ),
                                },
                            )
                        )
                # Span-text provenance: first ~80 chars of `text` should
                # appear under `body[chars[0]:chars[1]]` modulo
                # whitespace. We're forgiving — the activity may include
                # a leading speaker-header line that's not in `text`.
                text = (act.get("text") or "").strip()
                if text and len(chars) == 2:
                    s, e = chars[0], chars[1]
                    if 0 <= s < e <= body_len:
                        slab = body[s:e]
                        head = text[:80]
                        if head and head not in slab:
                            # Try whitespace-collapsed comparison.
                            ws_slab = re.sub(r"\s+", " ", slab)
                            ws_head = re.sub(r"\s+", " ", head)
                            if ws_head not in ws_slab:
                                issues.append(
                                    Issue(
                                        kind="span_text_mismatch",
                                        rid=rid,
                                        detail={
                                            "head": head[:60],
                                            "span_chars": [s, e],
                                        },
                                    )
                                )
            prev_act_type = act_type
    return issues


def check_md_coverage(sidecar: dict[str, Any], body: str) -> list[Issue]:
    """Layer C — every speaker-header in the MD must be claimed by an
    activity within ±100 chars.
    """
    issues: list[Issue] = []
    doc_type = sidecar.get("document_type")
    if doc_type not in ("plenary_stenogram", "plenary_joint_session"):
        return issues

    # Collect record START positions — agenda activities + interpellations.
    # A speaker header at position H is "claimed by a record" iff some
    # record START sits within HALO chars of H. We do NOT treat record
    # END positions as claim points: that would let a wrapping activity
    # span (the Boc bug pattern: chair's activity extends past Boc's
    # header) silently absorb the missed turn.
    starts: list[int] = []
    body_obj = sidecar.get("body") or {}
    items = body_obj.get("agenda_items") or []
    if not isinstance(items, list):
        items = []
    for ai in items:
        if not isinstance(ai, dict):
            continue
        for act in ai.get("activities") or []:
            if not isinstance(act, dict):
                continue
            chars = (act.get("source_span") or {}).get("chars") or []
            if chars and act.get("type") in (
                "speech",
                "vote",
                "narrator",
                "procedural",
                "deferral",
            ):
                starts.append(int(chars[0]))
    interps = body_obj.get("interpellations") or []
    if isinstance(interps, list):
        for ix in interps:
            if not isinstance(ix, dict):
                continue
            chars = (ix.get("source_span") or {}).get("chars") or []
            if chars:
                starts.append(int(chars[0]))
    starts.sort()

    # Boilerplate-claimed intervals (`coverage.claimed_by_policy[]`).
    # The plenary extractor claims chair connectives, pure political
    # declarations, SUMAR table, footers, and so on as boilerplate —
    # the content is intentionally NOT emitted as records. A header
    # whose position falls inside any such interval is "claimed by
    # boilerplate" and must not flag as dropped_turn.
    bp_intervals: list[tuple[int, int]] = []
    coverage = sidecar.get("coverage") or {}
    for c in coverage.get("claimed_by_policy") or []:
        chars = c.get("chars") if isinstance(c, dict) else None
        if isinstance(chars, list) and len(chars) == 2:
            bp_intervals.append((int(chars[0]), int(chars[1])))
    bp_intervals.sort()

    HALO = 100
    sumar = find_sumar_span(body)

    for hstart, _hend, name in iter_speaker_headers(body):
        # Headers inside SUMAR are titles, not turns.
        if sumar and sumar[0] <= hstart < sumar[1]:
            continue
        # Plenary section-marker boilerplate (PARTEA / DEZBATERI / …).
        if _is_boilerplate_header(name):
            continue
        # Inside a boilerplate-claimed interval (chair connective,
        # pure political declaration, footer, …) — extractor knows
        # about it, just doesn't emit a record.
        in_bp = False
        for s, e in bp_intervals:
            if s <= hstart < e:
                in_bp = True
                break
            if s > hstart:
                break
        if in_bp:
            continue
        ok = False
        for ap in starts:
            if abs(ap - hstart) <= HALO:
                ok = True
                break
            if ap > hstart + HALO:
                break
        if not ok:
            issues.append(
                Issue(
                    kind="dropped_turn",
                    detail={
                        "name": name[:80],
                        "position": hstart,
                        "head": body[hstart : hstart + 100].replace("\n", " "),
                    },
                )
            )
    return issues


def check_agenda_titles(sidecar: dict[str, Any], body: str) -> list[Issue]:
    """Layer B (#7) — agenda title appears in SUMAR span or body.

    The exemption: titles do NOT need to appear at the agenda's
    `position_in_document` (that's where the discussion begins).
    """
    issues: list[Issue] = []
    doc_type = sidecar.get("document_type")
    if doc_type not in ("plenary_stenogram", "plenary_joint_session"):
        return issues
    body_obj = sidecar.get("body") or {}
    items = body_obj.get("agenda_items") or []
    if not isinstance(items, list):
        return issues
    # Cheap full-body presence check — catches titles even when the
    # SUMAR isn't recognized. Both sides are dash-normalized so the
    # SUMAR's `4–9` (en-dash) and the title's `4-9` (hyphen) collide.
    body_norm = _normalize_for_title_match(body)
    sumar = find_sumar_span(body)
    sumar_norm = (
        _normalize_for_title_match(body[sumar[0] : sumar[1]]) if sumar else None
    )
    for ai in items:
        if not isinstance(ai, dict):
            continue
        title = (ai.get("title") or "").strip()
        if not title:
            continue
        # Build a list of candidate substrings. The agenda title from
        # SUMAR sometimes carries a trailing page-number ("Aprobarea
        # ordinii de zi 14") that the body doesn't repeat — strip it
        # for the head candidate. For batch-vote agenda titles
        # (`Supunerea la votul final a: – Proiectului…`) the bill name
        # only shows up later in the body — we also try a tail and a
        # middle slice. The check passes when ANY candidate lands.
        norm_title = _normalize_for_title_match(title)
        # Strip trailing standalone digits / page-ranges (`14`, `1-12`,
        # `48` — a SUMAR-row column-bleed pattern). Only operates on
        # the trailing token; in-title numbers (`Legea nr. 52/2003`)
        # are unaffected.
        norm_title_no_pg = re.sub(
            r"\s+\d+(?:\s*[-–]\s*\d+)?\s*$", "", norm_title
        ).rstrip()
        candidates: list[str] = []
        head = norm_title[:40]
        if head:
            candidates.append(head)
        if norm_title_no_pg and norm_title_no_pg != norm_title:
            head_clean = norm_title_no_pg[:40]
            if head_clean and head_clean not in candidates:
                candidates.append(head_clean)
        if len(norm_title) >= 60:
            tail = norm_title[-40:]
            if tail and tail not in candidates:
                candidates.append(tail)
        if len(norm_title) >= 90:
            mid = norm_title[len(norm_title) // 2 - 15 : len(norm_title) // 2 + 15]
            if mid and mid not in candidates:
                candidates.append(mid)
        if not candidates:
            continue
        ok = False
        for cand in candidates:
            if sumar_norm and cand in sumar_norm:
                ok = True
                break
            if cand in body_norm:
                ok = True
                break
        if ok:
            continue
        issues.append(
            Issue(
                kind="agenda_title_not_in_body",
                rid=ai.get("id"),
                detail={"title": title[:80]},
            )
        )
    return issues


def _normalize_for_title_match(s: str) -> str:
    """Normalize a string for lenient title-membership matching.

    - Lowercase
    - HTML line breaks (`<br>`, `<br/>`, `<br />`) collapse to a space
      (the converter retains them inside SUMAR table cells while the
      agenda extractor strips them from the title — without this fold,
      ~370 corpus titles surface as false `agenda_title_not_in_body`).
    - All dashes / hyphens / unusual minus-likes collapse to ASCII `-`
      (en-dash, em-dash, figure dash, minus sign, soft hyphen all fold).
    - Strip diacritics (en-dash mismatches and `Ț` vs `T` are the
      dominant sources of false-positive flags).
    - Collapse runs of whitespace.
    """
    s = s.lower()
    s = re.sub(r"<\s*br\s*/?\s*>", " ", s)
    s = re.sub(r"[‐-―−­]", "-", s)
    s = _strip_diacritics(s)
    s = re.sub(r"\s+", " ", s)
    return s


def check_interpellations(sidecar: dict[str, Any], body: str) -> list[Issue]:
    """Layer B (#8) — interpellation questioner name in body[pos:pos+400]."""
    issues: list[Issue] = []
    doc_type = sidecar.get("document_type")
    if doc_type not in ("plenary_stenogram", "plenary_joint_session"):
        return issues
    body_obj = sidecar.get("body") or {}
    interps = body_obj.get("interpellations") or []
    if not isinstance(interps, list):
        return issues
    body_len = len(body)
    for ix in interps:
        if not isinstance(ix, dict):
            continue
        rid = ix.get("id")
        chars = (ix.get("source_span") or {}).get("chars") or []
        if not chars:
            continue
        position = chars[0]
        if not (0 <= position < body_len):
            issues.append(
                Issue(
                    kind="position_oor_sidecar",
                    rid=rid,
                    detail={"position": position},
                )
            )
            continue
        questioner = ix.get("questioner") or {}
        qname = (questioner or {}).get("name") or (questioner or {}).get("raw") or ""
        if not qname:
            continue
        chunk = body[position : min(body_len, position + 600)]
        chunk_folded = _strip_diacritics(chunk)
        # Try the bare name first, then the peeled name. We accept any
        # token of the name appearing — false negatives are worse than
        # false positives at this layer (the verifier surfaces a flag,
        # not a bug).
        peeled = _peel_name(qname)
        peeled_folded = _strip_diacritics(peeled)
        if peeled_folded and peeled_folded in chunk_folded:
            continue
        # Token fallback — at least 2 tokens of length >=3 must appear.
        toks = [t for t in re.split(r"[\s\-]+", peeled_folded) if len(t) >= 3]
        if toks and sum(1 for t in toks if t in chunk_folded) >= max(1, len(toks) // 2):
            continue
        issues.append(
            Issue(
                kind="interpellation_questioner_missing",
                rid=rid,
                detail={
                    "questioner": qname[:80],
                    "position": position,
                    "head": chunk[:120].replace("\n", " "),
                },
            )
        )
    return issues


def check_questions(sidecar: dict[str, Any], body: str) -> list[Issue]:
    """Layer B (#9) — question regnum + questioner appear near position."""
    issues: list[Issue] = []
    doc_type = sidecar.get("document_type")
    if doc_type != "question_register":
        return issues
    body_obj = sidecar.get("body") or {}
    questions = body_obj.get("questions") or []
    if not isinstance(questions, list):
        return issues
    body_len = len(body)
    for q in questions:
        if not isinstance(q, dict):
            continue
        rid = q.get("id")
        chars = (q.get("source_span") or {}).get("chars") or []
        if not chars or len(chars) < 2:
            continue
        position = chars[0]
        span_end = chars[1]
        if not (0 <= position < body_len):
            issues.append(
                Issue(
                    kind="position_oor_sidecar",
                    rid=rid,
                    detail={"position": position},
                )
            )
            continue
        # Check regnum near position. The registration line `Nr. <N>/<DD.MM.YYYY>`
        # typically lands at the END of the question span (after the question
        # body), so we search the FULL span — not a fixed-size window. Long
        # questions (5000+ chars) made the old 2000-char window false-flag
        # ~676 cases corpus-wide; spans correctly bound the answer space.
        regnum = q.get("registration_number")
        if regnum:
            window = body[position : min(body_len, span_end)]
            if regnum not in window:
                issues.append(
                    Issue(
                        kind="question_regnum_missing",
                        rid=rid,
                        detail={
                            "regnum": regnum,
                            "position": position,
                        },
                    )
                )
    return issues


def check_votes(sidecar: dict[str, Any], body: str) -> list[Issue]:
    """Layer B (#10) — vote span starts with a vote-open phrase."""
    issues: list[Issue] = []
    doc_type = sidecar.get("document_type")
    if doc_type not in ("plenary_stenogram", "plenary_joint_session"):
        return issues
    body_obj = sidecar.get("body") or {}
    items = body_obj.get("agenda_items") or []
    if not isinstance(items, list):
        return issues
    body_len = len(body)
    for ai in items:
        if not isinstance(ai, dict):
            continue
        for act in ai.get("activities") or []:
            if not isinstance(act, dict):
                continue
            if act.get("type") != "vote":
                continue
            chars = (act.get("source_span") or {}).get("chars") or []
            if not chars:
                continue
            position = chars[0]
            if not (0 <= position < body_len):
                continue
            if not _has_vote_open_phrase(body, position, window=300):
                issues.append(
                    Issue(
                        kind="vote_open_missing",
                        rid=act.get("id"),
                        detail={
                            "position": position,
                            "head": body[position : position + 120].replace("\n", " "),
                        },
                    )
                )
    return issues


# ---- ES helpers ----------------------------------------------------------


def _get_es_client() -> Any | None:
    """Lazily build an ES client, one per worker process. Returns None
    when the cluster isn't configured or unreachable.

    Loads `.env` from the project root before reading `ESConfig.from_env()`
    so that the verifier picks up `ES_URL` / `ES_API_KEY` / `ES_VERIFY_CERTS`
    in operator workflows that don't pre-export them — same pattern as
    `monitorul-ii` CLI subcommands.
    """
    global _ES_CLIENT
    if _ES_CLIENT is not None:
        return _ES_CLIENT
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass
    try:
        from monitorul_ii.elasticsearch.client import build_client
        from monitorul_ii.elasticsearch.config import ESConfig
    except Exception:
        return None
    cfg = ESConfig.from_env()
    if cfg is None:
        return None
    try:
        _ES_CLIENT = build_client(cfg)
    except Exception:
        return None
    return _ES_CLIENT


def fetch_es_projection(
    es: Any, document_id: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Fetch the parent document doc + every per-doc child via the same
    queries the public site uses. Returns (parent_source, child_hits).

    The child hits are dicts with `_id`, `_index` (already normalized to
    the read-alias name), and `_source`. Pages past the queries layer's
    `PLAYBACK_PAGE_SIZE` cap until `total` is exhausted — `mo://2018/II/11`
    and friends carry 700+ child records, well over the 500-cap default.
    """
    from monitorul_ii.elasticsearch.queries import (
        PLAYBACK_PAGE_SIZE,
        get_document,
        list_document_children,
    )

    parent = None
    try:
        parent = get_document(es, document_id)
    except Exception:
        parent = None
    children: list[dict[str, Any]] = []
    try:
        page = 1
        while True:
            result = list_document_children(es, document_id, page=page)
            for h in result.hits:
                children.append(
                    {
                        "_id": h.id,
                        "_index": h.index,
                        "_source": h.source,
                    }
                )
            if len(children) >= result.total or len(result.hits) < PLAYBACK_PAGE_SIZE:
                break
            page += 1
            if page > 50:  # 25K records hard cap — guards against runaway loops
                break
    except Exception:
        children = []
    return parent, children


# ---- per-doc orchestrator ------------------------------------------------


def verify_doc(
    md_path: Path,
    *,
    sidecar: dict[str, Any] | None = None,
    body: str | None = None,
    es: Any | None = None,
    check_es: bool = True,
) -> DocResult:
    """Run every applicable check against one MD/sidecar/ES triple.

    `sidecar` and `body` are loaded from disk when not provided. `es` is
    looked up via the lazy worker client when not provided. `check_es=False`
    skips the ES correctness layer entirely (faster; suitable for offline
    runs against the sidecar-vs-MD layer only).
    """
    md_path = Path(md_path)
    sidecar_path = md_path.with_suffix(".extraction.json")
    try:
        if body is None:
            md_text = md_path.read_text(encoding="utf-8")
            _, body = split_md(md_text)
        if sidecar is None:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return DocResult(
            doc_id="<unknown>",
            md_path=str(md_path),
            error=f"load failed: {exc}",
        )

    document_id = sidecar.get("document_id") or "<unknown>"
    body_len = len(body)

    issues: list[Issue] = []

    # Layer C — MD → sidecar coverage (dropped turns)
    issues.extend(check_md_coverage(sidecar, body))

    # Layer B — speech speaker / span text
    issues.extend(check_speeches(sidecar, body))

    # Layer B — agenda titles
    issues.extend(check_agenda_titles(sidecar, body))

    # Layer B — interpellation questioner
    issues.extend(check_interpellations(sidecar, body))

    # Layer B — question regnum near position
    issues.extend(check_questions(sidecar, body))

    # Layer B — vote open phrase
    issues.extend(check_votes(sidecar, body))

    es_hits: list[dict[str, Any]] = []
    parent_doc: dict[str, Any] | None = None
    if check_es:
        if es is None:
            es = _get_es_client()
        if es is not None:
            parent_doc, es_hits = fetch_es_projection(es, document_id)
            issues.extend(check_es_correctness(es_hits, parent_doc, sidecar, body_len))

    # Stats
    by_grain: dict[str, int] = {}
    for h in es_hits:
        idx = h.get("_index") or "?"
        by_grain[idx] = by_grain.get(idx, 0) + 1

    sc_child_ids = _sidecar_child_record_ids(sidecar)
    sidecar_total = sum(len(v) for v in sc_child_ids.values())

    by_kind: dict[str, int] = {}
    for i in issues:
        by_kind[i.kind] = by_kind.get(i.kind, 0) + 1

    return DocResult(
        doc_id=document_id,
        md_path=str(md_path),
        issues=issues,
        stats={
            "body_len": body_len,
            "doc_type": sidecar.get("document_type"),
            "es_total": len(es_hits),
            "sidecar_total": sidecar_total,
            "by_grain": by_grain,
            "by_kind": by_kind,
        },
    )


# ---- CLI -----------------------------------------------------------------


def iter_md_paths(roots: Iterable[Path]) -> Iterator[Path]:
    """Yield MD paths under each root (file or directory)."""
    for r in roots:
        r = Path(r)
        if r.is_dir():
            for p in sorted(r.glob("*.md")):
                yield p
        elif r.is_file():
            if r.suffix == ".md":
                yield r
            elif r.suffix == ".json" and r.name.endswith(".extraction.json"):
                # Caller passed a sidecar; map back to the MD.
                yield r.with_suffix("").with_suffix(".md")
            else:
                yield r


def _verify_one_worker(args: tuple[str, bool]) -> dict[str, Any]:
    md_path, check_es = args
    res = verify_doc(Path(md_path), check_es=check_es)
    return json.loads(res.to_jsonl())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify ES playback fidelity against source MDs.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="MD file(s) or directory containing *.md / *.extraction.json",
    )
    parser.add_argument(
        "-j",
        "--workers",
        type=int,
        default=1,
        help="Process pool size for parallel verification (default: 1)",
    )
    parser.add_argument(
        "--no-es",
        action="store_true",
        help="Skip ES checks; verify sidecar-vs-MD only",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSONL results to this file (one row per doc) in addition to stdout",
    )
    parser.add_argument(
        "--filter-kind",
        action="append",
        default=[],
        help="Only emit docs whose issues include this kind. Repeatable.",
    )
    parser.add_argument(
        "--affected-docs-for-kind",
        help="Print only doc_ids of docs with at least one issue of this kind, one per line.",
    )
    parser.add_argument(
        "--affected-md-paths-for-kind",
        help="Print only md_paths of docs with at least one issue of this kind, one per line.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Stop after N docs (for smoke tests)",
    )
    args = parser.parse_args(argv)

    paths = list(iter_md_paths(Path(p) for p in args.paths))
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print("no MD paths to verify", file=sys.stderr)
        return 2

    check_es = not args.no_es

    output_fh = None
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        output_fh = open(args.output, "w", encoding="utf-8")

    started = time.time()
    total = len(paths)
    verified = 0
    passed = 0
    with_issues = 0
    total_issues = 0
    by_kind: dict[str, int] = {}
    affected_docs: list[str] = []
    affected_paths: list[str] = []

    work = [(str(p), check_es) for p in paths]

    def emit(rec: dict[str, Any]) -> None:
        nonlocal with_issues, total_issues
        nonlocal affected_docs, affected_paths
        kinds = {i["kind"] for i in rec.get("issues", [])}
        if args.filter_kind and not (kinds & set(args.filter_kind)):
            return
        if args.affected_docs_for_kind:
            if args.affected_docs_for_kind in kinds:
                affected_docs.append(rec["doc_id"])
            return
        if args.affected_md_paths_for_kind:
            if args.affected_md_paths_for_kind in kinds:
                affected_paths.append(rec["md_path"])
            return
        line = json.dumps(rec, ensure_ascii=False)
        print(line)
        if output_fh:
            output_fh.write(line + "\n")

    if args.workers > 1:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            for fut in as_completed(ex.submit(_verify_one_worker, w) for w in work):
                rec = fut.result()
                verified += 1
                issues = rec.get("issues") or []
                if issues:
                    with_issues += 1
                else:
                    passed += 1
                total_issues += len(issues)
                for i in issues:
                    by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
                emit(rec)
    else:
        for w in work:
            rec = _verify_one_worker(w)
            verified += 1
            issues = rec.get("issues") or []
            if issues:
                with_issues += 1
            else:
                passed += 1
            total_issues += len(issues)
            for i in issues:
                by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
            emit(rec)

    if output_fh:
        output_fh.close()

    if args.affected_docs_for_kind:
        for d in sorted(set(affected_docs)):
            print(d)
        return 0
    if args.affected_md_paths_for_kind:
        for p in sorted(set(affected_paths)):
            print(p)
        return 0

    elapsed = time.time() - started
    by_kind_sorted = dict(sorted(by_kind.items(), key=lambda kv: -kv[1]))
    summary = {
        "verified": verified,
        "passed": passed,
        "with_issues": with_issues,
        "total_issues": total_issues,
        "by_kind": by_kind_sorted,
        "elapsed_sec": round(elapsed, 2),
        "total": total,
    }
    print(
        f"verified={verified}  passed={passed}  with_issues={with_issues}"
        f"  total_issues={total_issues}  elapsed={elapsed:.1f}s",
        file=sys.stderr,
    )
    print(f"by_kind={json.dumps(by_kind_sorted, ensure_ascii=False)}", file=sys.stderr)
    # Also drop the summary at the end of the output file (if any).
    if args.output:
        with open(args.output, "a", encoding="utf-8") as fh:
            fh.write("# summary " + json.dumps(summary, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
