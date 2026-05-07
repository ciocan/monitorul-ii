"""Stable identity layer for extraction sidecars.

Mints `record_id`, `content_fingerprint`, and `slug` for every grain that
becomes an Elasticsearch doc downstream — agenda items, activities
(speeches / votes / procedural / narrator / deferral), interpellations,
questions, committee meetings, committee agenda items, and reports.

The keystone safety design: identities are persisted in the sidecar so
re-extractions, downstream consumers (LLM enrichments, ES indexer, the
public website's URL slugs) all key off the same surface. Without this
layer every URL is brittle and every enrichment file's join key drifts.

Slug-once contract: `assign_identity` accepts a `prior_sidecar` argument.
When a record's freshly-minted `record_id` matches an entry in the prior
sidecar, the prior `slug` wins — the slug is locked on first mint and
never re-derived even if the title evolves under regex tweaks. This is
what makes URLs stable across months of extractor iteration.

`record_id` patterns follow Q2 of the ES design doc:
  - Document             → mo://YYYY/PART/ISSUE
  - Agenda item          → <doc_id>#agenda-<ordinal>
  - Activity (non-vote)  → <doc_id>#agenda-<ord>#act-<seq>
  - Vote                 → <doc_id>#agenda-<ord>#vote-<seq>
  - Interpellation       → <doc_id>#interp-<num>            (or #interp-seq-<n>)
  - Question             → <doc_id>#q-<regnum>              (or #q-seq-<n>)
  - Committee meeting    → <doc_id>#cmt-<committee_id>
  - Committee agenda     → <doc_id>#cmt-<committee_id>#item-<ordinal>
  - Report               → <doc_id>                          (one per R-MO)
"""

from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from typing import Any

IDENTITY_VERSION = "0.1.1"

GrainName = str  # "agenda_item" | "activity_speech" | "activity_vote" | ...


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")
_NATURAL_KEY_SAFE_RE = re.compile(r"[^A-Za-z0-9._\-/]+")


def _slug_safe_natural_key(raw: str) -> str:
    """Normalize a natural-key fragment (regnum, interpellation_number,
    committee_id) for safe inclusion in a record_id suffix.

    Keeps alphanumerics, dots, dashes, slashes, and underscores; replaces
    everything else with `-`. Strips leading/trailing punctuation.
    """
    cleaned = _NATURAL_KEY_SAFE_RE.sub("-", raw).strip("-")
    return cleaned or "unknown"


def mint_record_id(
    doc_id: str,
    grain: GrainName,
    *,
    ordinal: int | None = None,
    seq: int | None = None,
    natural_key: str | None = None,
) -> str:
    """Pure function returning the canonical record_id for a grain.

    `doc_id` is `mo://YYYY/PART/ISSUE` (the envelope's document_id).
    `grain` is one of: document, agenda_item, activity, vote, interpellation,
    interpellation_seq, question, question_seq, committee_meeting,
    committee_agenda_item, report.

    Raises ValueError when required positional arguments for the grain are
    missing — the call sites (in `assign_identity`) are exhaustive, so any
    miss here surfaces as a programmer error fast.
    """
    if grain in ("document", "report"):
        return doc_id

    if grain == "agenda_item":
        if ordinal is None:
            raise ValueError("agenda_item requires ordinal")
        return f"{doc_id}#agenda-{ordinal}"

    if grain == "activity":
        if ordinal is None or seq is None:
            raise ValueError("activity requires ordinal + seq")
        return f"{doc_id}#agenda-{ordinal}#act-{seq}"

    if grain == "vote":
        if ordinal is None or seq is None:
            raise ValueError("vote requires ordinal + seq")
        return f"{doc_id}#agenda-{ordinal}#vote-{seq}"

    if grain == "interpellation":
        if natural_key is None:
            raise ValueError("interpellation requires natural_key")
        return f"{doc_id}#interp-{_slug_safe_natural_key(natural_key)}"

    if grain == "interpellation_seq":
        if seq is None:
            raise ValueError("interpellation_seq requires seq")
        return f"{doc_id}#interp-seq-{seq}"

    if grain == "question":
        if natural_key is None:
            raise ValueError("question requires natural_key")
        return f"{doc_id}#q-{_slug_safe_natural_key(natural_key)}"

    if grain == "question_seq":
        if seq is None:
            raise ValueError("question_seq requires seq")
        return f"{doc_id}#q-seq-{seq}"

    if grain == "committee_meeting":
        if natural_key is None:
            raise ValueError("committee_meeting requires natural_key")
        return f"{doc_id}#cmt-{_slug_safe_natural_key(natural_key)}"

    if grain == "committee_agenda_item":
        if natural_key is None or ordinal is None:
            raise ValueError("committee_agenda_item requires natural_key + ordinal")
        return f"{doc_id}#cmt-{_slug_safe_natural_key(natural_key)}#item-{ordinal}"

    raise ValueError(f"unknown grain: {grain!r}")


def compute_content_fingerprint(text: str) -> str:
    """sha256 of NFC-normalized + whitespace-collapsed input, hex-truncated
    to 12 chars.

    The fingerprint is forensic: when extractor v0.3 changes activity
    boundaries, fingerprints let migration scripts map old IDs to new ones
    for in-flight enrichments. Identical text → identical fingerprint;
    extra whitespace / unicode normal-form variations don't perturb it.
    """
    normalized = unicodedata.normalize("NFC", text or "")
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:12]


def compute_short_id(
    year: int,
    issue: str,
    ordinal: int | None = None,
    seq: int | None = None,
) -> str:
    """Deterministic ~10-char base32 short-id for URL tails.

    The composite tuple (year, issue, ordinal, seq) is sha256'd and the
    first 6 bytes are base32-encoded (10 chars after stripping `=`
    padding). Lowercased for URL ergonomics. Stability: the same tuple
    always produces the same short_id — server routes match on this only,
    so the human-readable slug prefix can evolve without breaking the URL.
    """
    parts: list[str] = [str(year), issue]
    if ordinal is not None:
        parts.append(f"o{ordinal}")
    if seq is not None:
        parts.append(f"s{seq}")
    composite = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(composite).digest()[:6]
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return encoded


def mint_slug(title: str, short_id: str, *, max_keyword_chars: int = 60) -> str:
    """ASCII-folded `<title-keywords>-<short_id>` slug, capped to ~8 tokens.

    - NFKD-decompose to strip Romanian diacritics (asciifold).
    - Lowercase.
    - Replace non-alphanumeric runs with `-`.
    - Truncate to first 8 tokens or `max_keyword_chars` chars (whichever
      first), then strip trailing `-`.
    - Empty / all-punctuation titles fall back to the short_id alone.
    """
    if not title:
        return short_id
    folded = unicodedata.normalize("NFKD", title)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.lower()
    folded = _NON_ALNUM_RE.sub("-", folded).strip("-")
    if not folded:
        return short_id
    tokens = folded.split("-")
    keyword = "-".join(tokens[:8])
    if len(keyword) > max_keyword_chars:
        keyword = keyword[:max_keyword_chars].rstrip("-")
        # If we cut mid-token, drop that partial token.
        if keyword and len(keyword) < max_keyword_chars:
            pass
    keyword = keyword.rstrip("-")
    if not keyword:
        return short_id
    return f"{keyword}-{short_id}"


def _index_prior_records(prior_body: dict[str, Any]) -> dict[str, str]:
    """Walk a prior body dict and collect {record_id: slug} for slug
    preservation. Returns empty dict when no prior exists.
    """
    out: dict[str, str] = {}
    if not prior_body:
        return out

    def _record(rec: dict[str, Any] | None) -> None:
        if not isinstance(rec, dict):
            return
        rid = rec.get("id")
        slug = rec.get("slug")
        if isinstance(rid, str) and isinstance(slug, str):
            out[rid] = slug

    # Plenary / joint
    for item in prior_body.get("agenda_items", []) or []:
        _record(item)
        for act in item.get("activities", []) or []:
            _record(act)
    for interp in prior_body.get("interpellations", []) or []:
        _record(interp)

    # Question register
    for q in prior_body.get("questions", []) or []:
        _record(q)

    # Committee synthesis
    for committee in prior_body.get("committees", []) or []:
        for meeting in committee.get("meetings", []) or []:
            _record(meeting)
            for ag in meeting.get("agenda", []) or []:
                _record(ag)

    # Report facsimile
    report = prior_body.get("report")
    _record(report)

    return out


def _activity_text(act: dict[str, Any]) -> str:
    """Extract the canonical text for an activity's content fingerprint."""
    t = act.get("type")
    if t == "speech":
        return str(act.get("text") or "")
    if t == "vote":
        return str(act.get("motion_text") or "")
    if t in ("procedural", "narrator", "deferral"):
        return str(act.get("text") or "")
    return ""


def _activity_grain_kind(act: dict[str, Any]) -> str:
    """Vote activities use a separate seq counter; everything else shares
    the act-seq counter. Returns either "vote" or "activity"."""
    return "vote" if act.get("type") == "vote" else "activity"


def _activity_title_for_slug(act: dict[str, Any]) -> str:
    """Activities don't carry titles per se. Use the first ~80 chars of
    their text as a slug seed. Votes use motion_text; speeches/etc. use
    `text`. This is a best-effort decoration — the short_id at the slug
    tail is what makes the URL canonical."""
    text = _activity_text(act)
    return text[:80] if text else ""


def assign_identity(
    body: dict[str, Any],
    doc_type: str,
    doc_id: str,
    year: int,
    issue: str,
    *,
    prior_sidecar: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mint id/content_fingerprint/slug into every grain in `body` in place.

    Returns the envelope-level identity block: {record_id: doc_id}. The
    caller stores this at `sidecar["extraction"]["identity"]`.

    When `prior_sidecar` is provided, slugs are preserved for any record
    whose freshly-minted `id` matches a prior record's `id` — the slug-
    once contract that keeps published URLs stable across re-extractions.
    """
    prior_body = (prior_sidecar or {}).get("body") or {}
    prior_slugs = _index_prior_records(prior_body)

    def _stamp(
        rec: dict[str, Any],
        *,
        rid: str,
        text: str,
        title: str,
        ordinal: int | None = None,
        seq: int | None = None,
    ) -> None:
        rec["id"] = rid
        rec["content_fingerprint"] = compute_content_fingerprint(text)
        if rid in prior_slugs:
            rec["slug"] = prior_slugs[rid]
        else:
            short_id = compute_short_id(year, issue, ordinal=ordinal, seq=seq)
            rec["slug"] = mint_slug(title, short_id)

    if doc_type in ("plenary_stenogram", "plenary_joint_session"):
        for item in body.get("agenda_items", []) or []:
            ordinal = item.get("ordinal")
            if not isinstance(ordinal, int):
                continue
            rid = mint_record_id(doc_id, "agenda_item", ordinal=ordinal)
            _stamp(
                item,
                rid=rid,
                text=str(item.get("title") or ""),
                title=str(item.get("title") or ""),
                ordinal=ordinal,
            )
            act_seq = 0
            vote_seq = 0
            for act in item.get("activities", []) or []:
                kind = _activity_grain_kind(act)
                if kind == "vote":
                    vote_seq += 1
                    a_rid = mint_record_id(
                        doc_id, "vote", ordinal=ordinal, seq=vote_seq
                    )
                    _stamp(
                        act,
                        rid=a_rid,
                        text=_activity_text(act),
                        title=_activity_title_for_slug(act),
                        ordinal=ordinal,
                        seq=vote_seq,
                    )
                else:
                    act_seq += 1
                    a_rid = mint_record_id(
                        doc_id, "activity", ordinal=ordinal, seq=act_seq
                    )
                    _stamp(
                        act,
                        rid=a_rid,
                        text=_activity_text(act),
                        title=_activity_title_for_slug(act),
                        ordinal=ordinal,
                        seq=act_seq,
                    )

        interp_seq = 0
        seen_interp_ids: set[str] = set()
        for interp in body.get("interpellations", []) or []:
            interp_seq += 1
            num = interp.get("interpellation_number")
            if isinstance(num, str) and num:
                i_rid = mint_record_id(doc_id, "interpellation", natural_key=num)
                # Two interpellations occasionally share the same
                # `interpellation_number` — corpus-real bug surfaced via
                # the playback verifier (count_mismatch on
                # `mo-interpellations`, 105 docs / 205 collisions
                # corpus-wide). Append a positional suffix so the
                # indexer's bulk-upsert doesn't merge them under the
                # same `_id`. The suffix is the 1-based ordinal among
                # entries that share the same `num` (so the first keeps
                # `interp-N`; the second becomes `interp-N-2`, etc.).
                if i_rid in seen_interp_ids:
                    base = i_rid
                    dup_idx = 2
                    while i_rid in seen_interp_ids:
                        i_rid = f"{base}-{dup_idx}"
                        dup_idx += 1
            else:
                i_rid = mint_record_id(doc_id, "interpellation_seq", seq=interp_seq)
            seen_interp_ids.add(i_rid)
            text = str(interp.get("question_text") or interp.get("topic") or "")
            title = str(interp.get("topic") or interp.get("question_text") or "")
            _stamp(
                interp,
                rid=i_rid,
                text=text,
                title=title,
                ordinal=interp_seq,
                seq=interp_seq,
            )

    elif doc_type == "question_register":
        q_seq = 0
        seen_q_ids: set[str] = set()
        for q in body.get("questions", []) or []:
            q_seq += 1
            regnum = q.get("registration_number")
            if isinstance(regnum, str) and regnum:
                q_rid = mint_record_id(doc_id, "question", natural_key=regnum)
                # Same dedup guard as the interpellation path above:
                # extractor occasionally emits two questions sharing the
                # same regnum; without this, the indexer's bulk-upsert
                # would merge them under one `_id`.
                if q_rid in seen_q_ids:
                    base = q_rid
                    dup_idx = 2
                    while q_rid in seen_q_ids:
                        q_rid = f"{base}-{dup_idx}"
                        dup_idx += 1
            else:
                q_rid = mint_record_id(doc_id, "question_seq", seq=q_seq)
            seen_q_ids.add(q_rid)
            text = str(q.get("question_text") or q.get("topic") or "")
            title = str(q.get("topic") or q.get("question_text") or "")
            _stamp(
                q,
                rid=q_rid,
                text=text,
                title=title,
                ordinal=q_seq,
                seq=q_seq,
            )

    elif doc_type == "committee_synthesis":
        for committee in body.get("committees", []) or []:
            cname = committee.get("name") or ""
            cid = _slug_safe_natural_key(cname[:80]) if cname else "unknown"
            for m_idx, meeting in enumerate(
                committee.get("meetings", []) or [], start=1
            ):
                m_rid = mint_record_id(
                    doc_id, "committee_meeting", natural_key=f"{cid}-{m_idx}"
                )
                purpose = meeting.get("purpose") or ""
                dates = meeting.get("dates") or []
                dates_repr = ",".join(str(d) for d in dates)
                m_text = f"{cname}|{purpose}|{dates_repr}"
                _stamp(
                    meeting,
                    rid=m_rid,
                    text=m_text,
                    title=cname,
                    ordinal=m_idx,
                    seq=m_idx,
                )
                for ag in meeting.get("agenda", []) or []:
                    ord_ = ag.get("ordinal")
                    if not isinstance(ord_, int):
                        continue
                    a_rid = mint_record_id(
                        doc_id,
                        "committee_agenda_item",
                        natural_key=f"{cid}-{m_idx}",
                        ordinal=ord_,
                    )
                    title = str(ag.get("title") or "")
                    _stamp(
                        ag,
                        rid=a_rid,
                        text=title,
                        title=title,
                        ordinal=ord_,
                        seq=ord_,
                    )

    elif doc_type == "report_facsimile":
        report = body.get("report")
        if isinstance(report, dict):
            r_rid = mint_record_id(doc_id, "report")
            title = str(report.get("title") or "")
            issuing = str(report.get("issuing_body") or "")
            _stamp(
                report,
                rid=r_rid,
                text=f"{title}|{issuing}",
                title=title,
            )

    return {"record_id": doc_id}


__all__ = [
    "IDENTITY_VERSION",
    "assign_identity",
    "compute_content_fingerprint",
    "compute_short_id",
    "mint_record_id",
    "mint_slug",
]
