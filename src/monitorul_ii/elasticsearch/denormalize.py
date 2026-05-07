"""Pure denormalizers — sidecar JSON → per-grain Elasticsearch docs.

One function per output grain, each taking a parsed sidecar dict (and an
optional `enrichments` mapping keyed by `record_id`) and returning a list
of ES-shaped dicts. The design contract from `docs/elasticsearch-indexing.md`:

* **Per-occurrence truth** — parent context (chamber, session_date,
  legislature) is denormalized down onto every child grain so queries
  filter against the speech doc, never join.
* **Refs flattened** — `references_mentioned[]` and `primary_references[]`
  are projected into per-type keyword arrays (`refs.bills`, `refs.laws`,
  `refs.codes`, ...) on each grain that carries them.
* **`is_substantive`** — derived strictly from `text_length >= 100` per
  Q5; this drives both the public-search default filter and the SEO
  indexability cutoff.
* **`url_path`** — populated from the slug minted in P2 (slug-once); the
  per-grain URL shape is the `Q1` table in the design doc.
* **Embeddings omitted** when no enrichment file carries them — ES
  `dense_vector` is sparse-tolerant; missing the field is the canonical
  "not yet embedded" signal.

These functions are pure: same sidecar + same enrichments → same docs,
deterministically. The indexer is the only caller; see `indexer.py` for
how docs are bulk-upserted via the per-grain write aliases.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import Any

# Identifiers must match the ES bootstrap helpers in `bootstrap.py` — the
# write-alias names are derived `<grain>-write`.
GRAIN_DOCUMENTS = "mo-documents"
GRAIN_AGENDA_ITEMS = "mo-agenda-items"
GRAIN_SPEECHES = "mo-speeches"
GRAIN_VOTES = "mo-votes"
GRAIN_INTERPELLATIONS = "mo-interpellations"
GRAIN_QUESTIONS = "mo-questions"
GRAIN_COMMITTEE_MEETINGS = "mo-committee-meetings"
GRAIN_REPORTS = "mo-reports"
GRAIN_PERSONS = "mo-persons"

GRAINS_WITH_DOCUMENT_PARENT: tuple[str, ...] = (
    GRAIN_DOCUMENTS,
    GRAIN_AGENDA_ITEMS,
    GRAIN_SPEECHES,
    GRAIN_VOTES,
    GRAIN_INTERPELLATIONS,
    GRAIN_QUESTIONS,
    GRAIN_COMMITTEE_MEETINGS,
    GRAIN_REPORTS,
)

# Speech length threshold for "substantive content" per Q5. p50 of speech
# length is 134 chars; chair phrases (Mulțumesc, ...) cluster <50.
SUBSTANTIVE_TEXT_LENGTH = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_date(value: Any) -> str | None:
    """Validate that `value` is a real ISO-8601 date or datetime string.

    Returns the original string if it parses cleanly, None otherwise.
    Catches the corpus-real failure mode where extractors emit calendar-
    impossible dates like `2022-11-31` (November has 30 days) — ES's
    date parser rejects these and would fail the whole bulk action.
    Sanitising here keeps the doc indexable with a null date, which is
    strictly better than losing the doc; the raw `dates[]` array (or
    equivalent) stays in the sidecar SOT for re-extraction.

    Pure-string guard: only `str` inputs are validated. Non-string
    truthy values (e.g. `int` epoch) pass through unchanged so we
    don't silently drop a legitimate type the mapping accepts.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return None
    # Date-only fast path; ISO date is the corpus's dominant form.
    try:
        date.fromisoformat(s[:10])
    except ValueError:
        # Maybe it's a full ISO datetime; try that too.
        try:
            datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return s


def _common_fields(
    *,
    record_id: str,
    document_id: str,
    sidecar: dict[str, Any],
    content_fingerprint: str | None = None,
    enrichments: dict[str, dict[str, Any]] | None = None,
    indexed_at: str | None = None,
) -> dict[str, Any]:
    """Project the keystone identity + provenance fields shared by every
    grain. The ES `_common_fields` component template defines these.

    `content_fingerprint` defaults to the sidecar's content_sha when no
    per-record fingerprint is supplied; downstream consumers use this to
    decide whether to re-pull a child page on cache invalidation.
    """
    enrichment_versions: dict[str, str] = {}
    if enrichments:
        entry = enrichments.get(record_id)
        if entry:
            for prod, payload in entry.items():
                if prod == "_meta":
                    continue
                meta = (
                    (payload or {}).get("_meta") if isinstance(payload, dict) else None
                )
                if isinstance(meta, dict) and meta.get("version"):
                    enrichment_versions[prod] = meta["version"]

    extractor_versions = (sidecar.get("extraction") or {}).get(
        "extractor_versions", {}
    ) or {}

    return {
        "record_id": record_id,
        "document_id": document_id,
        "content_fingerprint": content_fingerprint or sidecar.get("content_sha"),
        "content_sha_source": sidecar.get("content_sha"),
        "indexed_at": indexed_at or _now_iso(),
        "extractor_versions": dict(extractor_versions),
        "enrichment_versions": enrichment_versions,
        "schema_version": sidecar.get("schema_version"),
    }


def _short_id(record_id: str) -> str:
    """Pull the trailing `<short_id>` from a slug, or fall back to the
    record_id tail. Slugs follow `<keywords>-<short_id>` per Q7; the
    short_id is the last hyphen-separated token (~10 chars base32).
    """
    return record_id.split("/")[-1]


def _position_in_document(record: dict[str, Any]) -> int | None:
    """Pull `source_span.chars[0]` — the 0-indexed half-open char
    offset into the body — as the canonical source-order key.

    This is the load-bearing sort field for the per-document playback
    page (`/mo/<id>` rendering speeches + votes + interpellations + …
    in the order they appear in the original MO). Every sidecar record
    carries a `source_span.chars` block; an agenda item's span starts
    BEFORE its child activities, so a unified `ORDER BY
    position_in_document ASC` across grains correctly interleaves
    headers, activities, and trailing interpellations.

    Returns None when source_span is missing (defensive — callers
    treat None as "unknown position; sort to end").
    """
    span = record.get("source_span")
    if not isinstance(span, dict):
        return None
    chars = span.get("chars")
    if not isinstance(chars, list) or not chars:
        return None
    try:
        return int(chars[0])
    except (TypeError, ValueError):
        return None


def _slug_url_path(grain: str, slug: str | None, *, fallback_id: str) -> str | None:
    """Compose the public URL path per the Q1 grain → URL-shape table.

    Returns None when the grain is not publicly addressable (the persons
    grain falls back to the registry id since it has no slug yet).
    """
    if not slug:
        return None
    if grain == GRAIN_SPEECHES:
        return f"/discurs/{slug}"
    if grain == GRAIN_AGENDA_ITEMS:
        return f"/agenda/{slug}"
    if grain == GRAIN_INTERPELLATIONS:
        return f"/interpelare/{slug}"
    if grain == GRAIN_COMMITTEE_MEETINGS:
        return f"/comisie/{slug}"
    if grain == GRAIN_REPORTS:
        return f"/raport/{slug}"
    if grain == GRAIN_PERSONS:
        return f"/politicieni/{slug}"
    return None


def _flatten_refs(refs: Iterable[dict[str, Any]] | None) -> dict[str, list[str]]:
    """Project a `references_mentioned[]` or `primary_references[]` array
    onto per-type keyword arrays, plus a `types[]` fingerprint and the
    raw cite for human-debuggable filters.

    Per Q5: nested ref objects would double Lucene doc cardinality at no
    query benefit; flat per-type arrays serve the canonical query shape
    ("speeches mentioning bill PL-x 100/2018") with a single `term`.
    """
    bills: list[str] = []
    laws: list[str] = []
    codes: list[str] = []
    ougs: list[str] = []
    ogs: list[str] = []
    raws: list[str] = []
    types: list[str] = []
    if not refs:
        return {
            "types": types,
            "bills": bills,
            "laws": laws,
            "codes": codes,
            "ougs": ougs,
            "ogs": ogs,
            "raw": raws,
        }
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        ref_type = ref.get("type") or "unknown"
        types.append(ref_type)
        raw = ref.get("raw")
        if raw:
            raws.append(raw)
        number = ref.get("number")
        year = ref.get("year")
        if ref_type == "bill" and number and year:
            prefix = ref.get("prefix") or "PL-x"
            bills.append(f"{prefix}:{number}/{year}")
        elif ref_type == "law" and number and year:
            laws.append(f"{number}/{year}")
        elif ref_type == "code":
            name = ref.get("name") or ref.get("raw")
            if name:
                codes.append(name)
        elif ref_type == "oug" and number and year:
            ougs.append(f"{number}/{year}")
        elif ref_type == "og" and number and year:
            ogs.append(f"{number}/{year}")
    # Deduplicate while preserving first-seen order — keeps reproducible
    # output and avoids ES storing the same keyword twice on one doc.
    return {
        "types": _dedup(types),
        "bills": _dedup(bills),
        "laws": _dedup(laws),
        "codes": _dedup(codes),
        "ougs": _dedup(ougs),
        "ogs": _dedup(ogs),
        "raw": _dedup(raws),
    }


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parent_doc_context(sidecar: dict[str, Any]) -> dict[str, Any]:
    """Pre-compute the parent context fields that get denormalized onto
    every child grain. Order matters here only when sidecars omit
    optional fields — the dict is built once and reused.
    """
    metadata = sidecar.get("metadata") or {}
    body = sidecar.get("body") or {}
    session = body.get("session") if isinstance(body, dict) else None
    chamber = metadata.get("chamber")
    if not chamber and isinstance(session, dict):
        chambers_present = session.get("chambers_present")
        if isinstance(chambers_present, list) and chambers_present:
            chamber = ", ".join(chambers_present)
    return {
        "chamber": chamber,
        "session_date": _safe_date(
            metadata.get("session_date") or metadata.get("published")
        ),
        "session_type": metadata.get("session_type"),
        "legislature": metadata.get("legislature"),
        "year": metadata.get("year"),
        "mo_issue": metadata.get("issue"),
        "part": metadata.get("part"),
        "published": _safe_date(metadata.get("published")),
    }


def _enrichment_for(
    record_id: str, enrichments: dict[str, dict[str, Any]] | None
) -> dict[str, Any]:
    """Flatten the per-record enrichment payload (one dict per producer
    name) into a single dict ready for the ES `enrichments.*` namespace.

    Each producer's `_meta` block is dropped here — its only role is the
    versioning surface, already harvested by `_common_fields`.
    """
    if not enrichments:
        return {}
    entry = enrichments.get(record_id)
    if not entry:
        return {}
    out: dict[str, Any] = {}
    for prod, payload in entry.items():
        if prod == "_meta" or payload is None:
            continue
        if not isinstance(payload, dict):
            out[prod] = payload
            continue
        body = {k: v for k, v in payload.items() if k != "_meta"}
        # Single-key payloads collapse: a topics producer that emits
        # `{"topics": [...]}` projects directly onto `enrichments.topics`
        # rather than `enrichments.topics.topics`.
        if len(body) == 1 and prod in body:
            out[prod] = body[prod]
        else:
            out[prod] = body
    return out


# ---- per-grain denormalizers ----


def to_documents_doc(
    sidecar: dict[str, Any],
    *,
    enrichments: dict[str, dict[str, Any]] | None = None,
    s3_urls: dict[str, str] | None = None,
    indexed_at: str | None = None,
) -> dict[str, Any]:
    """Project the sidecar envelope into one `mo-documents` doc.

    `s3_urls` is an optional `{pdf, md, sidecar}` triple; we don't
    derive these from S3Config because the indexer is the only caller
    that holds that context.
    """
    document_id = sidecar.get("document_id")
    if not document_id:
        raise ValueError("sidecar missing document_id")
    parent = _parent_doc_context(sidecar)
    body = sidecar.get("body") or {}
    coverage = sidecar.get("coverage") or {}

    agenda_items = body.get("agenda_items") if isinstance(body, dict) else []
    speech_count = 0
    vote_count = 0
    if isinstance(agenda_items, list):
        for ai in agenda_items:
            for act in ai.get("activities") or []:
                if act.get("type") == "speech":
                    speech_count += 1
                elif act.get("type") == "vote":
                    vote_count += 1

    interpellation_count = 0
    if isinstance(body, dict):
        interps = body.get("interpellations")
        if isinstance(interps, list):
            interpellation_count = len(interps)

    question_count = 0
    if isinstance(body, dict):
        qs = body.get("questions")
        if isinstance(qs, list):
            question_count = len(qs)

    committee_count = 0
    if isinstance(body, dict):
        cmts = body.get("committees")
        if isinstance(cmts, list):
            for c in cmts:
                committee_count += len(c.get("meetings") or [])

    report_count = 1 if (isinstance(body, dict) and body.get("report")) else 0

    title = _document_title(sidecar)
    slug = _document_slug(sidecar)
    enrich_payload = _enrichment_for(document_id, enrichments)

    return {
        "_index": GRAIN_DOCUMENTS,
        "_id": document_id,
        "_source": {
            **_common_fields(
                record_id=document_id,
                document_id=document_id,
                sidecar=sidecar,
                content_fingerprint=sidecar.get("content_sha"),
                enrichments=enrichments,
                indexed_at=indexed_at,
            ),
            "chamber": parent["chamber"],
            "part": parent["part"],
            "issue": parent["mo_issue"],
            "year": parent["year"],
            "published": parent["published"],
            "session_date": parent["session_date"],
            "session_type": parent["session_type"],
            "legislature": parent["legislature"],
            "document_type": sidecar.get("document_type"),
            "title": title,
            "summary": enrich_payload.get("summary"),
            "agenda_count": len(agenda_items) if isinstance(agenda_items, list) else 0,
            "speech_count": speech_count,
            "vote_count": vote_count,
            "interpellation_count": interpellation_count,
            "question_count": question_count,
            "committee_count": committee_count,
            "report_count": report_count,
            "coverage": {
                "claimed_pct": coverage.get("claimed_pct"),
                "body_chars": coverage.get("body_chars"),
            },
            "s3_url_pdf": (s3_urls or {}).get("pdf"),
            "s3_url_md": (s3_urls or {}).get("md"),
            "s3_url_sidecar": (s3_urls or {}).get("sidecar"),
            "slug": slug,
            "url_path": _document_url_path(parent, sidecar.get("document_id")),
        },
    }


def _document_title(sidecar: dict[str, Any]) -> str | None:
    """A best-effort human title for the MO. report_facsimile carries an
    explicit `body.report.title`; plenary sidecars get a synthetic
    `<chamber> · <session_date>` so the persons / agenda pages have
    something to render in `<title>` while the model isn't yet rich
    enough for a per-doc summary.
    """
    body = sidecar.get("body") or {}
    if isinstance(body, dict):
        report = body.get("report")
        if isinstance(report, dict) and report.get("title"):
            return report["title"]
    metadata = sidecar.get("metadata") or {}
    chamber = metadata.get("chamber") or ""
    session_date = metadata.get("session_date") or metadata.get("published") or ""
    issue = metadata.get("issue") or ""
    year = metadata.get("year") or ""
    bits = [b for b in (chamber, f"MO {issue}/{year}", session_date) if b]
    return " · ".join(bits) or None


def _document_slug(sidecar: dict[str, Any]) -> str | None:
    """Derive a stable slug for `mo-documents`. report_facsimile carries
    one in `body.report.slug`; otherwise fall back to the document_id
    URI tail (`YYYY/PART/ISSUE`) which is already URL-safe.
    """
    body = sidecar.get("body") or {}
    if isinstance(body, dict):
        report = body.get("report")
        if isinstance(report, dict) and report.get("slug"):
            return report["slug"]
    document_id = sidecar.get("document_id") or ""
    return document_id.removeprefix("mo://").replace("/", "-") or None


def _document_url_path(parent: dict[str, Any], document_id: str | None) -> str | None:
    if not document_id:
        return None
    # mo://YYYY/PART/ISSUE → /mo/YYYY/PART/ISSUE
    return "/mo/" + document_id.removeprefix("mo://")


def to_agenda_items_docs(
    sidecar: dict[str, Any],
    *,
    enrichments: dict[str, dict[str, Any]] | None = None,
    indexed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Project plenary `body.agenda_items[]` into `mo-agenda-items` docs."""
    document_id = sidecar.get("document_id")
    if not document_id:
        raise ValueError("sidecar missing document_id")
    body = sidecar.get("body") or {}
    items = body.get("agenda_items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return []
    parent = _parent_doc_context(sidecar)
    out: list[dict[str, Any]] = []
    for ai in items:
        record_id = ai.get("id")
        if not record_id:
            continue
        refs = _flatten_refs(ai.get("primary_references"))
        speakers: list[str] = []
        vote_outcomes: list[str] = []
        total_votes = 0
        for act in ai.get("activities") or []:
            if act.get("type") == "vote":
                total_votes += 1
                outcome = act.get("outcome")
                if outcome:
                    vote_outcomes.append(outcome)
            sp = act.get("speaker") if isinstance(act, dict) else None
            if isinstance(sp, dict):
                pid = sp.get("person_id")
                if pid:
                    speakers.append(pid)
        topics_block = ai.get("topics") or {}
        topics_primary = topics_block.get("primary") or []
        enrich_payload = _enrichment_for(record_id, enrichments)
        out.append(
            {
                "_index": GRAIN_AGENDA_ITEMS,
                "_id": record_id,
                "_source": {
                    **_common_fields(
                        record_id=record_id,
                        document_id=document_id,
                        sidecar=sidecar,
                        content_fingerprint=ai.get("content_fingerprint"),
                        enrichments=enrichments,
                        indexed_at=indexed_at,
                    ),
                    "chamber": parent["chamber"],
                    "session_date": parent["session_date"],
                    "legislature": parent["legislature"],
                    "ordinal": _coerce_int(ai.get("ordinal")),
                    "position_in_document": _position_in_document(ai),
                    "category": ai.get("category"),
                    "title": ai.get("title"),
                    "outcome": ai.get("outcome"),
                    "confidence_type": ai.get("confidence_type"),
                    "requested_by_group": ai.get("requested_by_group"),
                    "reexamination_reason": ai.get("reexamination_reason"),
                    "topics_primary": topics_primary,
                    "refs": _refs_subset(
                        refs, ("types", "bills", "laws", "codes", "raw")
                    ),
                    "speaker_person_ids": _dedup(speakers),
                    "vote_summary": {
                        "total_votes": total_votes,
                        "outcomes": _dedup(vote_outcomes),
                    },
                    "enrichments": _enrichments_for_grain(
                        enrich_payload, ("summary", "topics", "embedding")
                    ),
                    "slug": ai.get("slug"),
                    "url_path": _slug_url_path(
                        GRAIN_AGENDA_ITEMS, ai.get("slug"), fallback_id=record_id
                    ),
                },
            }
        )
    return out


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _refs_subset(
    refs: dict[str, list[str]], keys: tuple[str, ...]
) -> dict[str, list[str]]:
    return {k: refs.get(k, []) for k in keys}


def _enrichments_for_grain(
    payload: dict[str, Any], allowed: tuple[str, ...]
) -> dict[str, Any]:
    """Subset the enrichment payload to the keys ES has mappings for.

    Producers may write whatever they want under their own key; we only
    project the keys the per-grain mapping declares. Unknown keys would
    still index (ES dynamic mapping is on by default for the
    `enrichments` namespace) but quietly burn shard storage.

    Special-cased: when the payload carries the `embedding` producer's
    nested shape (`{"vector": [...], "text_fingerprint": "..."}`), we
    flatten it into the two sibling ES fields the per-grain mappings
    declare — `embedding` (dense_vector) and `embedding_text_fingerprint`
    (keyword). The producer's nesting is what the indexer's loader
    naturally produces (one dict per producer key); the ES mapping
    expects flat fields so kNN can score directly off `enrichments.embedding`.
    """
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k == "embedding" and isinstance(v, dict):
            # Flatten the embedding producer's nested payload.
            vector = v.get("vector")
            if isinstance(vector, list) and "embedding" in allowed:
                out["embedding"] = vector
            fp = v.get("text_fingerprint")
            if isinstance(fp, str) and "embedding_text_fingerprint" in allowed:
                out["embedding_text_fingerprint"] = fp
            continue
        if k in allowed:
            out[k] = v
    return out


def to_speeches_docs(
    sidecar: dict[str, Any],
    *,
    enrichments: dict[str, dict[str, Any]] | None = None,
    indexed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Project speech activities (one per agenda × activity = `act-N`).

    Tiny chair turns (text_length < 100) are still indexed for the LLM
    agent's full-corpus tools but flagged `is_substantive: false`; the
    public default search filters them out.
    """
    document_id = sidecar.get("document_id")
    if not document_id:
        raise ValueError("sidecar missing document_id")
    body = sidecar.get("body") or {}
    items = body.get("agenda_items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return []
    parent = _parent_doc_context(sidecar)
    out: list[dict[str, Any]] = []
    for ai in items:
        agenda_title = ai.get("title")
        agenda_category = ai.get("category")
        agenda_outcome = ai.get("outcome")
        agenda_ordinal = _coerce_int(ai.get("ordinal"))
        position = 0
        for act in ai.get("activities") or []:
            if not isinstance(act, dict):
                continue
            if act.get("type") != "speech":
                continue
            record_id = act.get("id")
            if not record_id:
                continue
            position += 1
            speaker = act.get("speaker") or {}
            text = act.get("text") or ""
            text_length = len(text)
            refs = _flatten_refs(act.get("references_mentioned"))
            enrich_payload = _enrichment_for(record_id, enrichments)
            speech_enrich = _enrichments_for_grain(
                enrich_payload,
                (
                    "topics",
                    "summary",
                    "embedding",
                    "embedding_text_fingerprint",
                    "discourse",
                ),
            )
            out.append(
                {
                    "_index": GRAIN_SPEECHES,
                    "_id": record_id,
                    "_source": {
                        **_common_fields(
                            record_id=record_id,
                            document_id=document_id,
                            sidecar=sidecar,
                            content_fingerprint=act.get("content_fingerprint"),
                            enrichments=enrichments,
                            indexed_at=indexed_at,
                        ),
                        "chamber": parent["chamber"],
                        "session_date": parent["session_date"],
                        "session_type": parent["session_type"],
                        "legislature": parent["legislature"],
                        "year": parent["year"],
                        "mo_issue": parent["mo_issue"],
                        "agenda_ordinal": agenda_ordinal,
                        "agenda_title": agenda_title,
                        "agenda_category": agenda_category,
                        "agenda_outcome": agenda_outcome,
                        "speaker": _speaker_fields(speaker, act.get("delivery_mode")),
                        "text": text,
                        "text_length": text_length,
                        "is_substantive": text_length >= SUBSTANTIVE_TEXT_LENGTH,
                        "position_in_agenda": position,
                        "position_in_document": _position_in_document(act),
                        "refs": refs,
                        "enrichments": speech_enrich,
                        "slug": act.get("slug"),
                        "url_path": _slug_url_path(
                            GRAIN_SPEECHES, act.get("slug"), fallback_id=record_id
                        ),
                    },
                }
            )
    return out


def _speaker_fields(speaker: dict[str, Any], delivery_mode: Any) -> dict[str, Any]:
    """Project a Speaker dict into the per-grain `speaker.*` namespace.

    `name_search` is the analyzed-text version of the canonical name;
    `name_raw` is the original source string for exact match.
    """
    if not isinstance(speaker, dict):
        speaker = {}
    name = speaker.get("name") or speaker.get("raw")
    return {
        "person_id": speaker.get("person_id"),
        "name_raw": speaker.get("raw"),
        "name_search": name,
        "title": speaker.get("title"),
        "role": speaker.get("role"),
        "party_group_at_time": speaker.get("party_group"),
        "delivery_mode": delivery_mode,
    }


def to_votes_docs(
    sidecar: dict[str, Any],
    *,
    enrichments: dict[str, dict[str, Any]] | None = None,
    indexed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Project vote activities (`vote-N`) into `mo-votes`.

    `proposed_by.is_government` is True when the proposed_by Speaker
    is the Guvern sentinel populated by the proposed_by backfill.
    `defers_to` / `resolves` come from the cross-doc linker (Q6).
    """
    document_id = sidecar.get("document_id")
    if not document_id:
        raise ValueError("sidecar missing document_id")
    body = sidecar.get("body") or {}
    items = body.get("agenda_items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return []
    parent = _parent_doc_context(sidecar)
    out: list[dict[str, Any]] = []
    for ai in items:
        agenda_title = ai.get("title")
        agenda_category = ai.get("category")
        agenda_ordinal = _coerce_int(ai.get("ordinal"))
        # Refs flow from the parent agenda's primary_references — votes
        # don't carry their own. Bills + laws are the actionable subset
        # for the linker join.
        agenda_refs = _flatten_refs(ai.get("primary_references"))
        for act in ai.get("activities") or []:
            if not isinstance(act, dict):
                continue
            if act.get("type") != "vote":
                continue
            record_id = act.get("id")
            if not record_id:
                continue
            counts = act.get("counts") or {}
            for_count = counts.get("for")
            for_unanimous = for_count == "unanimous"
            for_int = (
                None
                if for_unanimous
                else _coerce_int(for_count if for_count != "unanimous" else None)
            )
            against = _coerce_int(counts.get("against"))
            abstain = _coerce_int(counts.get("abstain"))
            not_voting = _coerce_int(counts.get("not_voting"))
            total = _coerce_int(counts.get("total_voting"))
            outcome = act.get("outcome")
            proposed_by = act.get("proposed_by")
            proposed_by_proj = _proposed_by_fields(proposed_by)
            out.append(
                {
                    "_index": GRAIN_VOTES,
                    "_id": record_id,
                    "_source": {
                        **_common_fields(
                            record_id=record_id,
                            document_id=document_id,
                            sidecar=sidecar,
                            content_fingerprint=act.get("content_fingerprint"),
                            enrichments=enrichments,
                            indexed_at=indexed_at,
                        ),
                        "chamber": parent["chamber"],
                        "session_date": parent["session_date"],
                        "legislature": parent["legislature"],
                        "agenda_ordinal": agenda_ordinal,
                        "position_in_document": _position_in_document(act),
                        "agenda_title": agenda_title,
                        "agenda_category": agenda_category,
                        "motion_type": act.get("motion_type"),
                        "voting_method": act.get("voting_method"),
                        "outcome": outcome,
                        "counts": {
                            "for": for_int,
                            "for_unanimous": for_unanimous,
                            "against": against,
                            "abstain": abstain,
                            "not_voting": not_voting,
                            "total": total,
                        },
                        "quorum_met": act.get("quorum_announced") is not False
                        if act.get("quorum_announced") is not None
                        else None,
                        "deferred": outcome == "deferred",
                        "defers_to": act.get("defers_to"),
                        "resolves": act.get("resolves") or [],
                        "refs": _refs_subset(agenda_refs, ("types", "bills", "laws")),
                        "proposed_by": proposed_by_proj,
                        "url_path": _vote_url_path(act.get("slug"), record_id),
                    },
                }
            )
    return out


def _proposed_by_fields(proposed_by: Any) -> dict[str, Any] | None:
    """Pull person_id / name / is_government off a Speaker-shaped value
    (or pass through None when the agenda-extractor couldn't attribute).
    """
    if not isinstance(proposed_by, dict):
        return None
    person_id = proposed_by.get("person_id")
    name = proposed_by.get("name") or proposed_by.get("raw")
    is_gov = (
        person_id == "guvern"
        or (name and name.strip().lower() in ("guvern", "guvernul"))
        or proposed_by.get("role") == "guvern"
    )
    return {
        "person_id": person_id,
        "name": name,
        "is_government": bool(is_gov),
    }


def _vote_url_path(slug: str | None, record_id: str) -> str | None:
    """Per Q1: `/vot/<short_id>` — votes don't carry decorative
    keywords (the motion text is rarely human-meaningful at URL length).
    """
    if slug:
        # Slug ends in `-<short_id>`; take the last segment as the
        # canonical short_id so the URL is short.
        tail = slug.rsplit("-", 1)[-1]
        if tail:
            return f"/vot/{tail}"
    return f"/vot/{_short_id(record_id)}"


def to_interpellations_docs(
    sidecar: dict[str, Any],
    *,
    enrichments: dict[str, dict[str, Any]] | None = None,
    indexed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Project plenary `body.interpellations[]` into `mo-interpellations`."""
    document_id = sidecar.get("document_id")
    if not document_id:
        raise ValueError("sidecar missing document_id")
    body = sidecar.get("body") or {}
    interps = body.get("interpellations") if isinstance(body, dict) else None
    if not isinstance(interps, list):
        return []
    parent = _parent_doc_context(sidecar)
    out: list[dict[str, Any]] = []
    for itx in interps:
        record_id = itx.get("id")
        if not record_id:
            continue
        questioner = itx.get("questioner") or {}
        response = itx.get("response") or {}
        response_speaker = (
            response.get("speaker") if isinstance(response, dict) else None
        )
        enrich_payload = _enrichment_for(record_id, enrichments)
        out.append(
            {
                "_index": GRAIN_INTERPELLATIONS,
                "_id": record_id,
                "_source": {
                    **_common_fields(
                        record_id=record_id,
                        document_id=document_id,
                        sidecar=sidecar,
                        content_fingerprint=itx.get("content_fingerprint"),
                        enrichments=enrichments,
                        indexed_at=indexed_at,
                    ),
                    "chamber": parent["chamber"],
                    "session_date": parent["session_date"],
                    "legislature": parent["legislature"],
                    "interpellation_number": itx.get("interpellation_number"),
                    "position_in_document": _position_in_document(itx),
                    "questioner": _questioner_fields(questioner),
                    "addressed_to": itx.get("addressed_to"),
                    "addressed_to_normalized": itx.get("addressed_to_normalized"),
                    "topic": itx.get("topic"),
                    "question_text": itx.get("question_text"),
                    "response": _response_fields(response, response_speaker),
                    "response_deferred": itx.get("response_deferred"),
                    "genre": itx.get("genre"),
                    "delivery_mode": itx.get("delivery_mode"),
                    "enrichments": _enrichments_for_grain(
                        enrich_payload, ("topics", "summary", "embedding")
                    ),
                    "slug": itx.get("slug"),
                    "url_path": _slug_url_path(
                        GRAIN_INTERPELLATIONS, itx.get("slug"), fallback_id=record_id
                    ),
                },
            }
        )
    return out


def _questioner_fields(questioner: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(questioner, dict):
        questioner = {}
    return {
        "person_id": questioner.get("person_id"),
        "name": questioner.get("name") or questioner.get("raw"),
        "party_group_at_time": questioner.get("party_group"),
    }


def _response_fields(response: Any, response_speaker: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    speaker = response_speaker if isinstance(response_speaker, dict) else None
    return {
        "speaker": {
            "person_id": (speaker or {}).get("person_id"),
            "name": (speaker or {}).get("name") or (speaker or {}).get("raw"),
        }
        if speaker
        else None,
        "text": response.get("text"),
    }


def to_questions_docs(
    sidecar: dict[str, Any],
    *,
    enrichments: dict[str, dict[str, Any]] | None = None,
    indexed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Project `body.questions[]` (question_register) into `mo-questions`.

    URL shape `/intrebare/<regnum>-<regdate>` falls back to the slug or
    record_id tail when the natural key is missing — early-2000s docs
    sometimes lack a registration_date.
    """
    document_id = sidecar.get("document_id")
    if not document_id:
        raise ValueError("sidecar missing document_id")
    body = sidecar.get("body") or {}
    questions = body.get("questions") if isinstance(body, dict) else None
    if not isinstance(questions, list):
        return []
    metadata = sidecar.get("metadata") or {}
    chamber = body.get("chamber") if isinstance(body, dict) else None
    chamber = chamber or metadata.get("chamber")
    out: list[dict[str, Any]] = []
    for q in questions:
        record_id = q.get("id")
        if not record_id:
            continue
        addressee = q.get("addressee") or {}
        questioner = q.get("questioner") or {}
        regnum = q.get("registration_number")
        regdate = q.get("registration_date")
        url_path = _question_url_path(regnum, regdate, q.get("slug"), record_id)
        enrich_payload = _enrichment_for(record_id, enrichments)
        out.append(
            {
                "_index": GRAIN_QUESTIONS,
                "_id": record_id,
                "_source": {
                    **_common_fields(
                        record_id=record_id,
                        document_id=document_id,
                        sidecar=sidecar,
                        content_fingerprint=q.get("content_fingerprint"),
                        enrichments=enrichments,
                        indexed_at=indexed_at,
                    ),
                    "chamber": chamber,
                    "regnum": regnum,
                    "regdate": _safe_date(regdate),
                    "position_in_document": _position_in_document(q),
                    "questioner": _questioner_fields(questioner),
                    "addressee": {
                        "raw": addressee.get("ministry") or addressee.get("name"),
                        "ministry_normalized": addressee.get("ministry_normalized"),
                        "institutional_normalized": addressee.get(
                            "institutional_normalized"
                        ),
                    },
                    "topic": q.get("topic"),
                    "text": q.get("question_text"),
                    "delivery_mode": q.get("delivery_mode"),
                    # `regdate` is per-question and may be calendar-impossible
                    # in older sidecars; fall through to None on rejection.
                    "enrichments": _enrichments_for_grain(
                        enrich_payload, ("topics", "embedding")
                    ),
                    "url_path": url_path,
                },
            }
        )
    return out


def _question_url_path(
    regnum: Any, regdate: Any, slug: str | None, record_id: str
) -> str:
    if regnum and regdate:
        return f"/intrebare/{regnum}-{regdate}"
    if slug:
        return f"/intrebare/{slug}"
    return f"/intrebare/{_short_id(record_id)}"


def to_committee_meetings_docs(
    sidecar: dict[str, Any],
    *,
    enrichments: dict[str, dict[str, Any]] | None = None,
    indexed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Project committee_synthesis `body.committees[].meetings[]` into
    `mo-committee-meetings` (one ES doc per meeting, with nested
    `agenda_items` + `roster` per Q5).
    """
    document_id = sidecar.get("document_id")
    if not document_id:
        raise ValueError("sidecar missing document_id")
    body = sidecar.get("body") or {}
    committees = body.get("committees") if isinstance(body, dict) else None
    if not isinstance(committees, list):
        return []
    out: list[dict[str, Any]] = []
    for committee in committees:
        committee_name = committee.get("name")
        committee_kind = committee.get("kind")
        committee_id = committee.get("id") or _committee_slug_id(committee_name)
        joint_with = committee.get("joint_with") or []
        signatures = committee.get("signatures") or {}
        for meeting in committee.get("meetings") or []:
            record_id = meeting.get("id")
            if not record_id:
                continue
            meeting_date = _safe_date((meeting.get("dates") or [None])[0])
            agenda = []
            for ai in meeting.get("agenda") or []:
                agenda.append(
                    {
                        "ordinal": _coerce_int(ai.get("ordinal")),
                        "title": ai.get("title"),
                        "role": ai.get("committee_role"),
                        "outcome": (ai.get("vote_summary") or {}).get("outcome"),
                        "outcome_text": ai.get("outcome_text"),
                        "primary_references": _flat_ref_keys(
                            ai.get("primary_references")
                        ),
                    }
                )
            roster = []
            for entry in meeting.get("roster") or []:
                speaker = entry.get("speaker") or {}
                roster.append(
                    {
                        "person_id": speaker.get("person_id"),
                        "name": speaker.get("name") or speaker.get("raw"),
                        "status": entry.get("mode"),
                        "role": entry.get("intra_committee_role"),
                        "party_group_at_time": speaker.get("party_group"),
                    }
                )
            joint_value = joint_with or _project_joint_with(
                meeting.get("joint_with") or []
            )
            enrich_payload = _enrichment_for(record_id, enrichments)
            out.append(
                {
                    "_index": GRAIN_COMMITTEE_MEETINGS,
                    "_id": record_id,
                    "_source": {
                        **_common_fields(
                            record_id=record_id,
                            document_id=document_id,
                            sidecar=sidecar,
                            content_fingerprint=meeting.get("content_fingerprint"),
                            enrichments=enrichments,
                            indexed_at=indexed_at,
                        ),
                        "committee_id": committee_id,
                        "committee_name": committee_name,
                        "committee_kind": committee_kind,
                        "joint_with": joint_value,
                        "meeting_date": meeting_date,
                        "position_in_document": _position_in_document(meeting),
                        "format": meeting.get("format"),
                        "purpose": meeting.get("purpose"),
                        "agenda_items": agenda,
                        "roster": roster,
                        "signatures": _signatures_fields(signatures),
                        "enrichments": _enrichments_for_grain(
                            enrich_payload, ("summary", "topics", "embedding")
                        ),
                        "url_path": _committee_url_path(
                            committee_id, meeting_date, meeting.get("slug"), record_id
                        ),
                    },
                }
            )
    return out


def _committee_slug_id(name: Any) -> str | None:
    if not name:
        return None
    base = str(name).strip().lower()
    out = []
    for ch in base:
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-"):
            out.append("-")
    return "".join(out).strip("-") or None


def _flat_ref_keys(refs: Any) -> list[str]:
    if not isinstance(refs, list):
        return []
    out: list[str] = []
    for r in refs:
        if not isinstance(r, dict):
            continue
        ref_type = r.get("type") or "unknown"
        number = r.get("number")
        year = r.get("year")
        if number and year:
            prefix = r.get("prefix") or ref_type
            out.append(f"{prefix}:{number}/{year}")
        elif r.get("raw"):
            out.append(r["raw"])
    return _dedup(out)


def _project_joint_with(items: list[Any]) -> list[str]:
    out: list[str] = []
    for entry in items:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("committee_name")
            if name:
                out.append(name)
        elif isinstance(entry, str):
            out.append(entry)
    return _dedup(out)


def _signatures_fields(signatures: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(signatures, dict) or not signatures:
        return None
    pres = (
        (signatures.get("president") or {})
        if isinstance(signatures.get("president"), dict)
        else {}
    )
    sec = (
        (signatures.get("secretary") or {})
        if isinstance(signatures.get("secretary"), dict)
        else {}
    )
    return {
        "president_person_id": pres.get("person_id"),
        "secretary_person_id": sec.get("person_id"),
    }


def _committee_url_path(
    committee_id: str | None, meeting_date: Any, slug: str | None, record_id: str
) -> str | None:
    if committee_id and meeting_date:
        return f"/comisie/{committee_id}/{meeting_date}"
    if slug:
        return f"/comisie/{slug}"
    return f"/comisie/{_short_id(record_id)}"


def to_reports_docs(
    sidecar: dict[str, Any],
    *,
    enrichments: dict[str, dict[str, Any]] | None = None,
    indexed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Project report_facsimile `body.report` into a single `mo-reports`
    doc; the trailing nested `headings[]` array carries the H1/H2 outline.
    """
    document_id = sidecar.get("document_id")
    if not document_id:
        raise ValueError("sidecar missing document_id")
    body = sidecar.get("body") or {}
    report = body.get("report") if isinstance(body, dict) else None
    if not isinstance(report, dict):
        return []
    record_id = report.get("id") or document_id
    headings = body.get("headings") or []
    nested_headings = []
    if isinstance(headings, list):
        for h in headings:
            if isinstance(h, dict):
                nested_headings.append(
                    {"level": _coerce_int(h.get("level")), "text": h.get("text")}
                )
    received_at = report.get("received_at") or {}
    reporting_period = report.get("reporting_period") or {}
    enrich_payload = _enrichment_for(record_id, enrichments)
    return [
        {
            "_index": GRAIN_REPORTS,
            "_id": record_id,
            "_source": {
                **_common_fields(
                    record_id=record_id,
                    document_id=document_id,
                    sidecar=sidecar,
                    content_fingerprint=report.get("content_fingerprint"),
                    enrichments=enrichments,
                    indexed_at=indexed_at,
                ),
                "issuing_body": report.get("issuing_body"),
                "issuing_body_normalized": report.get("issuing_body_normalized"),
                "title": report.get("title"),
                "reporting_period": {
                    "from": _safe_date(reporting_period.get("from")),
                    "to": _safe_date(reporting_period.get("to")),
                },
                "received_at": {
                    "session_date": _safe_date(received_at.get("session_date")),
                    "session_kind": received_at.get("session_kind"),
                    "received_in_document": received_at.get("received_in_document"),
                },
                "headings": nested_headings,
                "enrichments": _enrichments_for_grain(
                    enrich_payload, ("summary", "topics", "embedding")
                ),
                "url_path": _slug_url_path(
                    GRAIN_REPORTS, report.get("slug"), fallback_id=record_id
                ),
            },
        }
    ]


def to_persons_docs(
    persons: list[dict[str, Any]] | dict[str, Any],
    *,
    indexed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Project the `persons.json` registry into `mo-persons` docs.

    Unlike the other grains, persons aren't derived from a sidecar — the
    indexer's --rebuild flow walks the registry directly. Stats are
    computed by a separate aggregation job (see Q4 caveats in the design
    doc), so we leave the `stats` block null on first index.
    """
    if isinstance(persons, dict):
        entries = persons.get("entries") or []
    else:
        entries = persons or []
    out: list[dict[str, Any]] = []
    for p in entries:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if not pid:
            continue
        slug = pid  # registry id IS the slug per Q4
        out.append(
            {
                "_index": GRAIN_PERSONS,
                "_id": pid,
                "_source": {
                    "id": pid,
                    "canonical_name": p.get("canonical_name"),
                    "diacritic_form": p.get("diacritic_form"),
                    "aliases": p.get("aliases") or [],
                    "wikidata_qid": p.get("wikidata_qid"),
                    "birth_date": _safe_date(p.get("birth_date")),
                    "mandates": [
                        {
                            **m,
                            "from": _safe_date(m.get("from"))
                            if isinstance(m, dict)
                            else None,
                            "to": _safe_date(m.get("to"))
                            if isinstance(m, dict)
                            else None,
                        }
                        if isinstance(m, dict)
                        else m
                        for m in (p.get("mandates") or [])
                    ],
                    "homonym_disambiguation": p.get("homonym_disambiguation"),
                    "slug": slug,
                    "url_path": f"/politicieni/{slug}",
                    "stats": None,
                    "indexed_at": indexed_at or _now_iso(),
                },
            }
        )
    return out


# ---- top-level dispatcher used by the indexer ----


def denormalize_sidecar(
    sidecar: dict[str, Any],
    *,
    enrichments: dict[str, dict[str, Any]] | None = None,
    grains: tuple[str, ...] | None = None,
    s3_urls: dict[str, str] | None = None,
    indexed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Run every applicable grain denormalizer against one sidecar.

    The dispatcher decides per `document_type` which grains apply (e.g.
    a question_register sidecar produces only `mo-documents` +
    `mo-questions`; report_facsimile produces only `mo-documents` +
    `mo-reports`). `grains` filter narrows the output further.
    """
    indexed_at = indexed_at or _now_iso()
    docs: list[dict[str, Any]] = []
    doc_type = sidecar.get("document_type")

    def keep(g: str) -> bool:
        return grains is None or g in grains

    if keep(GRAIN_DOCUMENTS):
        docs.append(
            to_documents_doc(
                sidecar,
                enrichments=enrichments,
                s3_urls=s3_urls,
                indexed_at=indexed_at,
            )
        )

    if doc_type in ("plenary_stenogram", "plenary_joint_session"):
        if keep(GRAIN_AGENDA_ITEMS):
            docs.extend(
                to_agenda_items_docs(
                    sidecar, enrichments=enrichments, indexed_at=indexed_at
                )
            )
        if keep(GRAIN_SPEECHES):
            docs.extend(
                to_speeches_docs(
                    sidecar, enrichments=enrichments, indexed_at=indexed_at
                )
            )
        if keep(GRAIN_VOTES):
            docs.extend(
                to_votes_docs(sidecar, enrichments=enrichments, indexed_at=indexed_at)
            )
        if keep(GRAIN_INTERPELLATIONS):
            docs.extend(
                to_interpellations_docs(
                    sidecar, enrichments=enrichments, indexed_at=indexed_at
                )
            )
    elif doc_type == "question_register":
        if keep(GRAIN_QUESTIONS):
            docs.extend(
                to_questions_docs(
                    sidecar, enrichments=enrichments, indexed_at=indexed_at
                )
            )
    elif doc_type == "committee_synthesis":
        if keep(GRAIN_COMMITTEE_MEETINGS):
            docs.extend(
                to_committee_meetings_docs(
                    sidecar, enrichments=enrichments, indexed_at=indexed_at
                )
            )
    elif doc_type == "report_facsimile":
        if keep(GRAIN_REPORTS):
            docs.extend(
                to_reports_docs(sidecar, enrichments=enrichments, indexed_at=indexed_at)
            )

    return docs


def child_record_ids(docs: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Group bulk-action dicts by grain → list of record_ids.

    Used by the indexer to compute the orphan-delete diff between the
    previous run's `child_record_ids` (from `es_indexed`) and the
    current run's output.
    """
    out: dict[str, list[str]] = {}
    for d in docs:
        idx = d.get("_index")
        rid = d.get("_id")
        if not idx or not rid:
            continue
        out.setdefault(idx, []).append(rid)
    return out


def _safe_value(value: Any) -> Any:
    """Strip `None` from nested dicts so ES doesn't index null leafs.

    Currently unused — ES is happy with null fields and the tests assert
    against the canonical shape including nulls. Keeping the helper here
    for future per-grain trimming if storage cost ever becomes a concern.
    """
    return value
