from __future__ import annotations

import os

# Disable onnxruntime / OpenMP intra-op threading before pymupdf4llm imports it.
# pymupdf-layout creates an ort.InferenceSession internally; if ORT auto-threads,
# our outer ThreadPoolExecutor (-j N) competes with inner threads for the same
# cores and throughput collapses (8 PDFs: 110 s default → 28.7 s with OMP=1 -j 8).
# `setdefault` keeps explicit user overrides intact.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("ORT_INTRA_OP_NUM_THREADS", "1")

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from dataclasses import replace  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from monitorul_ii.classifier import (  # noqa: E402
    classify_file,
    collect_mds,
)
from monitorul_ii.converter import (  # noqa: E402
    ConvertEvent,
    ConvertEventPayload,
    collect_pdfs,
    convert_all,
)
from monitorul_ii.db import DB  # noqa: E402
from monitorul_ii.elasticsearch import ESConfig  # noqa: E402
from monitorul_ii.elasticsearch import bootstrap as es_bootstrap  # noqa: E402
from monitorul_ii.elasticsearch.client import build_client as _build_es_client  # noqa: E402
from monitorul_ii.extraction import extract as _extract_md  # noqa: E402
from monitorul_ii.extraction.pipeline import EXTRACTOR_LABEL  # noqa: E402, F401
from monitorul_ii.scraper import (  # noqa: E402
    DayResult,
    FileEvent,
    FileEventPayload,
    _client,
    daterange,
    scrape_day,
)
from monitorul_ii.uploader import S3Config, Uploader  # noqa: E402

_FETCH_LABELS: dict[FileEvent, str] = {
    "skip": "skip ",
    "download": "ok   ",
    "error": "ERR  ",
}
_CONVERT_LABELS: dict[ConvertEvent, str] = {
    "skip": "skip ",
    "convert": "ok   ",
    "error": "ERR  ",
}

_HEARTBEAT_EVERY = 100  # days, for `fetch`
_CONVERT_HEARTBEAT_EVERY = 50  # PDFs, for `convert` in pipes


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid date {s!r}: expected YYYY-MM-DD"
        ) from e


def _add_s3_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip uploading to S3 even when S3_* env vars are set.",
    )
    p.add_argument(
        "--bucket",
        default=None,
        help="Override S3_BUCKET from env.",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="monitorul-ii",
        description="Scrape Monitorul Oficial PDFs and convert them to markdown.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser(
        "fetch",
        help="Download PDFs for a date or date range.",
        description="Download Monitorul Oficial Partea a II-a PDFs for a date or date range.",
    )
    fetch.add_argument("date", type=_parse_date, help="Date (YYYY-MM-DD)")
    fetch.add_argument(
        "--until",
        type=_parse_date,
        default=None,
        help="End of date range (inclusive). If omitted, only `date` is fetched.",
    )
    fetch.add_argument(
        "--out",
        type=Path,
        default=Path("pdfs"),
        help="Output directory (default: ./pdfs). PDFs land directly here; the date is in the filename.",
    )
    fetch.add_argument(
        "--part",
        default="II",
        help="Roman-numeral Partea to fetch (default: II).",
    )
    fetch.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait between PDF downloads (default: 0.5).",
    )
    fetch.add_argument(
        "--proxy",
        default=None,
        help="Proxy URL (e.g. http://user:pass@host:port). Overrides PROXY_URL from env.",
    )
    fetch.add_argument(
        "--no-proxy",
        action="store_true",
        help="Bypass any proxy configured via PROXY_URL or --proxy.",
    )
    _add_s3_args(fetch)
    fetch.add_argument(
        "--db",
        type=Path,
        default=Path("data/monitorul.db"),
        help="SQLite path for the audit log + resume gate (default: data/monitorul.db).",
    )
    fetch.add_argument(
        "--no-db",
        action="store_true",
        help="Disable the SQLite audit log entirely. Re-runs lose 'empty day' memory.",
    )
    fetch.add_argument(
        "--reverse",
        action="store_true",
        help="Walk the date range newest→oldest. A partial run leaves you with the most recent stretch.",
    )
    fetch.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch every day's index regardless of DB status.",
    )
    fetch.add_argument(
        "--rescrape-recent",
        type=int,
        default=0,
        metavar="N",
        help="Re-fetch the last N days regardless of DB status (default: 0). Today is always re-fetched.",
    )
    fetch.add_argument(
        "--retry-gone",
        action="store_true",
        help="Reset every issue currently marked 'gone' (permanent failure: server returned non-PDF or 4xx) back to 'pending' before walking the range. Use after the source site has presumably restored missing documents.",
    )
    fetch.set_defaults(func=cmd_fetch)

    convert = sub.add_parser(
        "convert",
        help="Convert downloaded PDFs to markdown.",
        description=(
            "Convert one or more local PDFs to markdown next to the source "
            "(e.g. pdfs/<basename>.md). Optionally mirrors to the same S3 bucket."
        ),
    )
    convert.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="PDF files or directories containing PDFs (non-recursive).",
    )
    convert.add_argument(
        "--force",
        action="store_true",
        help="Re-convert PDFs that already have a non-empty .md alongside.",
    )
    convert.add_argument(
        "-j",
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        metavar="N",
        help="Parallel conversion threads (default: CPU count). Set to 1 for sequential.",
    )
    convert.add_argument(
        "--reverse",
        action="store_true",
        help="Process PDFs in reverse order (newest→oldest, since filenames are date-prefixed). A partial run leaves you with the most recent stretch.",
    )
    _add_s3_args(convert)
    convert.set_defaults(func=cmd_convert)

    classify = sub.add_parser(
        "classify",
        help="Classify converted MDs by document type (step 1 of extraction).",
        description=(
            "Run the v1.3.0 type detector over one or more MD files or "
            "directories and emit one JSONL row per file to stdout. The "
            "load-bearing output is `--outliers`, which filters to the docs "
            "that fell into `other` or that match multiple types ambiguously "
            "— those are the unknown unknowns the schema discovery loop "
            "wants to inspect."
        ),
    )
    classify.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="MD files or directories containing MDs (non-recursive).",
    )
    classify.add_argument(
        "--outliers",
        action="store_true",
        help="Only emit rows that classified as `other` or are ambiguous (top-vs-second margin below --ambiguity-threshold).",
    )
    classify.add_argument(
        "--ambiguity-threshold",
        type=float,
        default=0.2,
        metavar="MARGIN",
        help="Ambiguity threshold: doc is flagged ambiguous when top_score - second_score < MARGIN (default: 0.2).",
    )
    classify.add_argument(
        "--reverse",
        action="store_true",
        help="Process MDs in reverse order (newest→oldest).",
    )
    classify.set_defaults(func=cmd_classify)

    extract = sub.add_parser(
        "extract",
        help="Extract structured JSON sidecars from converted MDs (step 2 of extraction).",
        description=(
            "Run the per-type extractor over one or more MD files or "
            "directories and emit a `<basename>.extraction.json` sidecar "
            "next to each MD. Document type is determined by the v1.6.0 "
            "classifier; types whose extractor has not yet shipped are "
            "skipped with a `not-yet-implemented` reason. Use `--type` to "
            "override the classifier on a single doc."
        ),
    )
    extract.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="MD files or directories containing MDs (non-recursive).",
    )
    extract.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even when an existing sidecar's schema_version + extractor_versions match the current code.",
    )
    extract.add_argument(
        "--type",
        dest="override_type",
        default=None,
        choices=(
            "plenary_stenogram",
            "plenary_joint_session",
            "committee_synthesis",
            "report_facsimile",
            "question_register",
            "other",
        ),
        help="Override the classifier and use this document type for every input. Use sparingly — only when classify is wrong on a specific doc.",
    )
    extract.add_argument(
        "--coverage-below",
        type=float,
        default=None,
        metavar="MARGIN",
        help="Diagnostic: print one JSONL line per extracted doc whose claimed_pct < MARGIN (with top-3 gap previews) to stdout. Does not affect writes — extraction proceeds normally regardless.",
    )
    extract.add_argument(
        "--reverse",
        action="store_true",
        help="Process MDs in reverse order (newest→oldest, since filenames are date-prefixed). A partial run leaves you with the most recent stretch.",
    )
    extract.add_argument(
        "--identity-only",
        action="store_true",
        help="Skip per-type extractors and re-run only the identity producer (mints id/content_fingerprint/slug on every grain) against the existing sidecar. A fast path for backfilling the schema 1.13.0 identity layer onto already-extracted sidecars without paying a full re-extract; preserves slugs from any prior identity pass (slug-once contract). Idempotent: a second run with the same identity version skips.",
    )
    _add_s3_args(extract)
    extract.set_defaults(func=cmd_extract)

    link = sub.add_parser(
        "link",
        help="Cross-document + cross-reference linker: report→session, vote-pair, art-N xref.",
        description=(
            "Walk `*.extraction.json` sidecars under one or more paths and run "
            "three linker passes:\n"
            "  (1) report→session (cross-doc) — fills each report_facsimile's "
            "`received_at.received_in_document` with the document_id of the "
            "joint-session (or single-chamber) stenogram that received it.\n"
            "  (2) vote-pair (cross-doc) — pairs deferred votes "
            "(outcome=deferred) in stenogram N with their resolving votes in "
            "a later stenogram M, writing `defers_to` (forward) on the "
            "deferring vote and `resolves` (back-link) on the resolver. "
            "60-day window; earliest-resolver-wins. Match key derived from "
            "agenda primary_references bill cite (or motion title hash for "
            "motion-class votes).\n"
            "  (3) cross-reference / xref (intra-doc) — resolves bare `art. N` "
            "unknown references in plenary + question_register sidecars to "
            "their owning law/code/bill anchor in the same paragraph (with "
            "fall-through to same-activity-span). Writes "
            "`unknown.resolved_to.char_offsets` pointing at the anchor.\n"
            "Default runs all three passes; --report-only / --vote-only / "
            "--xref-only restrict to a single pass. "
            "Pre-write schema validation; atomic write via .part rename. "
            "Idempotent — already-linked entries are skipped unless --force."
        ),
    )
    link.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Sidecar JSON files or directories (non-recursive).",
    )
    link.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-link sidecars whose targets are already populated. Applies to "
            "all selected passes."
        ),
    )
    link.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be linked without modifying any files.",
    )
    pass_group = link.add_mutually_exclusive_group()
    pass_group.add_argument(
        "--report-only",
        action="store_true",
        help=(
            "Run only the report→session pass; skip the vote-pair and "
            "xref passes. Mutually exclusive with --vote-only / --xref-only."
        ),
    )
    pass_group.add_argument(
        "--vote-only",
        action="store_true",
        help=(
            "Run only the vote-pair pass; skip the report→session and "
            "xref passes. Mutually exclusive with --report-only / --xref-only."
        ),
    )
    pass_group.add_argument(
        "--xref-only",
        action="store_true",
        help=(
            "Run only the cross-reference (art-N) pass; skip the report→"
            "session and vote-pair passes. Mutually exclusive with "
            "--report-only / --vote-only."
        ),
    )
    _add_s3_args(link)
    link.set_defaults(func=cmd_link)

    backfill = sub.add_parser(
        "backfill",
        help="Registry-driven backfills: populate *_normalized slots from curated registries.",
        description=(
            "Walk `*.extraction.json` sidecars and join curated registries "
            "into the schema's *_normalized slots. Each --kind targets one "
            "registry/field pair. Pre-write schema validation; atomic write "
            "via .part rename; idempotent (already-filled entries skip "
            "unless --force).\n"
            "  --kind=issuing_body — fills "
            "report_facsimile.body.report.issuing_body_normalized from the "
            "institutional bodies registry.\n"
            "  --kind=all — runs every shipped pass."
        ),
    )
    backfill.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Sidecar JSON files or directories (non-recursive).",
    )
    backfill.add_argument(
        "--kind",
        choices=("issuing_body", "ministry", "proposed_by", "persons", "all"),
        default="all",
        help=(
            "Which backfill pass to run (default: all). `issuing_body` "
            "fills `report_facsimile.body.report.issuing_body_normalized`; "
            "`ministry` fills "
            "`question_register.body.questions[].addressee.ministry_normalized` "
            "and "
            "`plenary_*.body.interpellations[].addressed_to_normalized` "
            "from the ministries registry (with institutional-body "
            "fallback); `proposed_by` fills "
            "`plenary_*.body.agenda_items[].activities[].proposed_by` "
            "(Guvern) on votes whose parent agenda carries an OUG/OG cite "
            "or title pattern; `persons` walks every Speaker dict (chair, "
            "agenda activities, interpellation questioner / response, "
            "committee roster, signatures, qr questioners) and fills "
            "`speaker.person_id` from `persons.json`, with the MO year "
            "feeding homonym disambiguation. `all` runs every shipped "
            "pass; forward-compatible with future registries."
        ),
    )
    backfill.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing *_normalized values when the registry "
            "yields a different canonical id."
        ),
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing to disk or S3.",
    )
    backfill.add_argument(
        "--reupload-on-skip",
        action="store_true",
        help=(
            "Also re-upload sidecars where the backfill skipped with "
            "reason 'already filled' (every slot already carries the "
            "current canonical id). Closes the historical staleness "
            "gap on the S3 bucket from pre-overwrite-fix runs that "
            "left the bucket holding the original `extract` bytes "
            "while subsequent link / backfill modifications stayed "
            "local-only. No effect on other skip reasons (`no "
            "speakers in body`, `no raw value`, `no registry match`, "
            "`no government-proposed agendas`) — those mean the local "
            "file simply doesn't carry data this pass would emit, so "
            "the bucket can't be 'stale' relative to one. Has no "
            "effect when running with `--no-upload` or when S3 is "
            "not configured."
        ),
    )
    backfill.add_argument(
        "-j",
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        metavar="N",
        help=(
            "Process-pool worker count for the per-sidecar match loop "
            "(default: CPU count). Each worker rebuilds the matcher's "
            "lru-cached alias index once (~1-2s) and amortizes that cost "
            "across its slice. Use 1 for sequential — necessary for "
            "deterministic output ordering or when debugging."
        ),
    )
    _add_s3_args(backfill)
    backfill.set_defaults(func=cmd_backfill)

    es_init = sub.add_parser(
        "es-init",
        help="Provision Elasticsearch templates, indices, aliases, and API keys.",
        description=(
            "Bootstrap the v1 Elasticsearch surface for the monitorul.ai "
            "projection layer: install the `mo-analyzers` and "
            "`mo-common-fields` component templates, the nine per-grain "
            "index templates (mo-documents, mo-agenda-items, mo-speeches, "
            "mo-votes, mo-interpellations, mo-questions, mo-committee-meetings, "
            "mo-reports, mo-persons), one concrete index per grain with "
            "blue-green naming `<grain>-<YYYYMMDD>-v1` plus a read alias "
            "(`<grain>`) and write alias (`<grain>-write`), and the two "
            "API keys (`monitorul_reader`, `monitorul_indexer`). "
            "Idempotent — already-present entities are left untouched. "
            "Run a smoke index/get round-trip on `mo-documents` at the end "
            "to confirm the wiring.\n"
            "Reads `ES_URL`, `ES_API_KEY`, and `ES_VERIFY_CERTS` from the "
            "environment (or `.env` via python-dotenv). Pass `--dry-run` "
            "to print what would be created without contacting ES."
        ),
    )
    es_init.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the templates, indices, aliases, and API keys that "
            "would be created without contacting Elasticsearch. Useful "
            "for CI sanity checks."
        ),
    )
    es_init.add_argument(
        "--generation-suffix",
        default=None,
        metavar="SUFFIX",
        help=(
            "Override the generation suffix (default: today's "
            "`YYYYMMDD-v1`). Pass an explicit value when scripting a "
            "blue-green major-trigger rebuild (e.g. `20260615-v2`)."
        ),
    )
    es_init.add_argument(
        "--skip-smoke",
        action="store_true",
        help=(
            "Skip the post-bootstrap index/get round-trip on "
            "`mo-documents`. Only useful when you want to validate the "
            "templates + aliases shape without leaving a smoke document "
            "behind."
        ),
    )
    es_init.add_argument(
        "--update-mappings",
        action="store_true",
        help=(
            "Apply additive mapping changes from the on-disk "
            "`mappings/<grain>.json` JSONs to the live indices that "
            "each `<grain>` read alias resolves to. Use this after "
            "adding a new field to a mapping JSON (e.g. "
            "`position_in_document`) to push the diff without "
            "minting a new generation. ES `put_mapping` is additive "
            "only — adding fields is safe and idempotent; changing "
            "field types would error out. Skips the rest of the "
            "bootstrap (templates, indices, API keys, smoke). "
            "Pair with `monitorul-ii index pdfs/ --force` to "
            "backfill the new field on every existing doc."
        ),
    )
    es_init.set_defaults(func=cmd_es_init)

    index_cmd = sub.add_parser(
        "index",
        help="Index sidecars + enrichments into Elasticsearch.",
        description=(
            "Walk `*.extraction.json` sidecars, denormalise across the "
            "9 mo-* grains, bulk-upsert via per-grain write aliases, "
            "track state in `data/monitorul.db` for idempotency, and "
            "delete-by-query orphans for record_ids that disappeared "
            "between runs (e.g. when a re-extract merges two adjacent "
            "speeches into one).\n"
            "Idempotency triple: (sidecar_content_sha, "
            "enrichment_fingerprint, index_generation). All three "
            "match → skip; any one differs → reindex.\n"
            "`--target=<index-name>` writes into a specific blue-green "
            "generation (`mo-speeches-20260615-v2`); `--mirror` writes "
            "to BOTH the live alias AND `--target` so a catch-up run "
            "doesn't miss new ingestion. `--rebuild` is a convenience "
            "alias for `--force` against a target generation.\n"
            "Reads `ES_URL` / `ES_API_KEY` / `ES_VERIFY_CERTS` from "
            "the environment (or `.env`)."
        ),
    )
    index_cmd.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Sidecar JSON files or directories (non-recursive).",
    )
    index_cmd.add_argument(
        "--force",
        action="store_true",
        help=(
            "Reindex every sidecar regardless of the idempotency triple. "
            "Useful after schema bumps, mapping changes, or to verify a "
            "freshly-cut blue-green generation against the live one."
        ),
    )
    index_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the denormalisation + enrichment merge end-to-end "
            "without contacting Elasticsearch or writing to the DB. "
            "Prints per-doc grain counts to stdout."
        ),
    )
    index_cmd.add_argument(
        "--target",
        default=None,
        metavar="INDEX",
        help=(
            "Override the write target with a specific generation "
            "(e.g. `mo-speeches-20260615-v2`). Only docs whose grain "
            "matches the target's prefix are redirected — the rest "
            "still flow through their respective live `<grain>-write` "
            "aliases. Pair with `--mirror` to keep the live target in "
            "sync during blue-green catch-up."
        ),
    )
    index_cmd.add_argument(
        "--mirror",
        action="store_true",
        help=(
            "When set with `--target`, write to BOTH the target "
            "generation AND the live `<grain>-write` alias. The "
            "blue-green Q6 catch-up flow."
        ),
    )
    index_cmd.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Force a full re-index against `--target` (implies "
            "`--force` and skips state-row idempotency). Operationally "
            "the same as `--force --target=<gen>`; provided as a "
            "single-flag convenience for the bootstrap rebuild."
        ),
    )
    index_cmd.add_argument(
        "--grain",
        default=None,
        action="append",
        choices=(
            "mo-documents",
            "mo-agenda-items",
            "mo-speeches",
            "mo-votes",
            "mo-interpellations",
            "mo-questions",
            "mo-committee-meetings",
            "mo-reports",
            "mo-persons",
        ),
        help=(
            "Restrict projection to a single grain (or repeat for "
            "multiple). Useful for targeted re-pass after a per-grain "
            "mapping bump (`--grain=mo-speeches --force` after the "
            "speech analyzer config changes)."
        ),
    )
    index_cmd.add_argument(
        "--db",
        type=Path,
        default=Path("data/monitorul.db"),
        help=(
            "SQLite path for the indexer state table (default: "
            "data/monitorul.db, shared with `fetch`)."
        ),
    )
    index_cmd.add_argument(
        "--index-generation",
        default="live",
        metavar="LABEL",
        help=(
            "Third leg of the idempotency triple (default: `live`). "
            "Set when running `--target` so the state row tracks the "
            "right generation independently of the live one."
        ),
    )
    index_cmd.add_argument(
        "-j",
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help=(
            "ThreadPoolExecutor worker count (default: 1, sequential). "
            "Per-sidecar work is network-bound on ES round-trips "
            "(bulk + delete_by_query), both of which release the GIL "
            "via urllib3 — so threads scale near-linearly up to the "
            "cluster's bulk-throughput ceiling. Each worker opens its "
            "own `DB(db_path)` connection (SQLite forbids cross-thread "
            "sharing); WAL mode handles concurrent reads + serialised "
            "writes fine at this rate (one row per sidecar, microsec "
            "per write while ES round-trips are 500 ms+). Output is "
            "in completion order (not input order) when N > 1; set "
            "N=1 for deterministic ordering or single-process "
            "debugging. 20-core box: try -j 16 for a 5–10× speedup."
        ),
    )
    index_cmd.add_argument(
        "--include-persons",
        action="store_true",
        help=(
            "Also project the curated `persons.json` registry into "
            "`mo-persons` after the sidecar loop finishes. Persons "
            "aren't sidecar-derived (Q4 of the design doc — they live "
            "in `src/monitorul_ii/registries/persons.json`), so the "
            "default daily-cron run leaves `mo-persons` alone. Pair "
            "with the bootstrap rebuild or after a registry bump "
            "(stub merges, Wikidata enrichment). Idempotent: a "
            "`__persons_registry__` sentinel row in `es_indexed` "
            "stores the registry's content hash; subsequent runs skip "
            "until persons.json changes. Orphan-delete fires when an "
            "entry is removed from the registry, pulling its "
            "`/politicieni/<slug>` page out of `mo-persons` so the "
            "public site stops serving stale content."
        ),
    )
    index_cmd.set_defaults(func=cmd_index)

    query = sub.add_parser(
        "query",
        help="Run a named reference query against the live `mo-*` indices.",
        description=(
            "Debug CLI for the typed query layer in "
            "`monitorul_ii.elasticsearch.queries`. Pick a query by name "
            "(`--name search_speeches`), pass parameters as JSON "
            '(`--params \'{"q":"educație","page_size":5}\'`), and the '
            "result prints as pretty-formatted JSON to stdout. Designed "
            "for ad-hoc inspection during the P5 webapp build-out — the "
            "same functions back the production `lib/search.ts` layer, "
            "so a green query here is a green query there.\n"
            "Reads `ES_URL` / `ES_API_KEY` / `ES_VERIFY_CERTS` from the "
            "environment (or `.env` via python-dotenv)."
        ),
    )
    query.add_argument(
        "--name",
        required=True,
        metavar="QUERY",
        help=(
            "Named query to run. One of: search_speeches, get_document, "
            "list_documents_by_date, get_agenda_item, get_speech, "
            "person_page, search_persons, list_committee_meetings, "
            "get_report, agg_speeches_by_party_year. See "
            "`monitorul_ii.elasticsearch.queries` for signatures."
        ),
    )
    query.add_argument(
        "--params",
        default="{}",
        metavar="JSON",
        help=(
            "JSON object whose keys map to the query function's keyword "
            "arguments. Positional args (`document_id`, `record_id`, "
            "`person_slug`, `committee_id`, `q`, `date`) may also be "
            "passed via this dict — the CLI promotes them to positional "
            "as needed. Default: `{}` (no parameters)."
        ),
    )
    query.add_argument(
        "--explain",
        action="store_true",
        help=(
            "Print the request body before running the query, in "
            "addition to the result. Useful for debugging the filter / "
            "agg shape against the ES query DSL docs."
        ),
    )
    query.set_defaults(func=cmd_query)

    return p


def _day_summary_line(
    r: DayResult, uploaded: int, in_bucket: int, upload_errors: int
) -> str | None:
    if not r.fetched_index and r.found == 0 and r.skipped == 0 and r.downloaded == 0:
        # Pure DB-cached skip — progress bar carries the day count, don't spam logs.
        return None
    line = (
        f"{r.day}: found={r.found} downloaded={r.downloaded} "
        f"skipped={r.skipped} errors={len(r.errors)}"
    )
    if not r.fetched_index:
        line += " (cached)"
    if uploaded or in_bucket or upload_errors:
        line += (
            f" | s3 uploaded={uploaded} in-bucket={in_bucket} errors={upload_errors}"
        )
    return line


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_bytes(b: int) -> str:
    if b >= 1_000_000_000:
        return f"{b / 1_000_000_000:.2f} GB"
    return f"{b / 1_000_000:.1f} MB"


def _progress_line(
    days_done: int,
    days_total: int,
    totals: dict[str, int],
    counters: dict[str, int],
    elapsed: float,
) -> str:
    rate = days_done / elapsed if elapsed > 0 else 0.0
    eta = (days_total - days_done) / rate if rate > 0 else 0.0
    pct = days_done / days_total * 100 if days_total else 0.0
    return (
        f"{days_done:,}/{days_total:,} ({pct:.1f}%) | "
        f"found={totals['found']:,} downloaded={totals['downloaded']:,} "
        f"({_fmt_bytes(counters['download_bytes'])}) "
        f"failed={totals['failed']:,} | "
        f"s3 uploaded={counters['uploaded']:,} "
        f"in-bucket={counters['in_bucket']:,} "
        f"errors={counters['upload_errors']:,} | "
        f"elapsed={_fmt_duration(elapsed)} ETA={_fmt_duration(eta)}"
    )


class _ProgressReporter:
    """Live tty bar (rich) when stderr is a terminal, periodic heartbeat otherwise."""

    def __init__(
        self,
        days_total: int,
        totals: dict[str, int],
        counters: dict[str, int],
    ) -> None:
        self.days_total = days_total
        self.totals = totals
        self.counters = counters
        self.start = time.monotonic()
        self.days_done = 0
        self._tty = sys.stderr.isatty()
        self._progress = None
        self._task = None
        if self._tty:
            from rich.console import Console
            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                TextColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )

            self._progress = Progress(
                BarColumn(bar_width=None),
                TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
                MofNCompleteColumn(),
                TextColumn("·"),
                TextColumn("[cyan]{task.description}"),
                TextColumn("·"),
                TimeElapsedColumn(),
                TextColumn("ETA"),
                TimeRemainingColumn(),
                console=Console(stderr=True),
                transient=False,
            )
            self._task = self._progress.add_task(self._desc(), total=days_total)

    def _desc(self) -> str:
        t, c = self.totals, self.counters
        return (
            f"found={t['found']:,} dl={t['downloaded']:,} ({_fmt_bytes(c['download_bytes'])})"
            f" fail={t['failed']:,}"
            f" · s3 up={c['uploaded']:,} have={c['in_bucket']:,} err={c['upload_errors']:,}"
        )

    def __enter__(self) -> "_ProgressReporter":
        if self._progress is not None:
            self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._progress is not None:
            self._progress.stop()

    def print(self, line: str, *, err: bool = False) -> None:
        if self._progress is not None:
            self._progress.console.print(line, highlight=False)
            self._progress.update(self._task, description=self._desc())
        else:
            print(line, file=sys.stderr if err else sys.stdout)

    def advance(self) -> None:
        self.days_done += 1
        if self._progress is not None:
            self._progress.update(self._task, advance=1, description=self._desc())
            return
        if self.days_done % _HEARTBEAT_EVERY == 0 and self.days_done < self.days_total:
            elapsed = time.monotonic() - self.start
            print(
                "progress: "
                + _progress_line(
                    self.days_done, self.days_total, self.totals, self.counters, elapsed
                ),
                file=sys.stderr,
            )


class _ConvertProgressReporter:
    """Live `rich` bar for `convert` when stderr is a tty; heartbeat otherwise.

    Mirrors `_ProgressReporter`'s shape but speaks the convert vocabulary
    (converted/skipped/errors + the same s3 trio).
    """

    def __init__(self, total: int, counters: dict[str, int]) -> None:
        self.total = total
        self.counters = counters
        self.start = time.monotonic()
        self.done = 0
        self._tty = sys.stderr.isatty()
        self._progress = None
        self._task = None
        if self._tty and total > 0:
            from rich.console import Console
            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                TextColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )

            self._progress = Progress(
                BarColumn(bar_width=None),
                TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
                MofNCompleteColumn(),
                TextColumn("·"),
                TextColumn("[cyan]{task.description}"),
                TextColumn("·"),
                TimeElapsedColumn(),
                TextColumn("ETA"),
                TimeRemainingColumn(),
                console=Console(stderr=True),
                transient=False,
            )
            self._task = self._progress.add_task(self._desc(), total=total)

    def _desc(self) -> str:
        c = self.counters
        s = f"ok={c['converted']:,} skip={c['skipped']:,} err={c['errors']:,}"
        if c["uploaded"] or c["in_bucket"] or c["upload_errors"]:
            s += (
                f" · s3 up={c['uploaded']:,} have={c['in_bucket']:,}"
                f" err={c['upload_errors']:,}"
            )
        return s

    def __enter__(self) -> "_ConvertProgressReporter":
        if self._progress is not None:
            self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._progress is not None:
            self._progress.stop()

    def print(self, line: str, *, err: bool = False) -> None:
        if self._progress is not None:
            self._progress.console.print(line, highlight=False)
            self._progress.update(self._task, description=self._desc())
        else:
            print(line, file=sys.stderr if err else sys.stdout)

    def advance(self) -> None:
        self.done += 1
        if self._progress is not None:
            self._progress.update(self._task, advance=1, description=self._desc())
            return
        if self.done % _CONVERT_HEARTBEAT_EVERY == 0 and self.done < self.total:
            elapsed = time.monotonic() - self.start
            rate = self.done / elapsed if elapsed > 0 else 0.0
            eta = (self.total - self.done) / rate if rate > 0 else 0.0
            pct = self.done / self.total * 100 if self.total else 0.0
            print(
                f"progress: {self.done:,}/{self.total:,} ({pct:.1f}%) | "
                f"{self._desc()} | "
                f"elapsed={_fmt_duration(elapsed)} ETA={_fmt_duration(eta)}",
                file=sys.stderr,
            )


def _resolve_uploader(args: argparse.Namespace) -> Uploader | None:
    if args.no_upload:
        return None
    cfg = S3Config.from_env()
    if cfg is None:
        return None
    if args.bucket:
        cfg = replace(cfg, bucket=args.bucket)
    up = Uploader(cfg)
    try:
        up.validate()
    except Exception as exc:
        print(
            f"s3: cannot reach bucket {cfg.bucket!r} at {cfg.endpoint}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    print(f"uploading to s3://{cfg.bucket} at {cfg.endpoint}", file=sys.stderr)
    return up


def cmd_fetch(args: argparse.Namespace) -> int:
    end = args.until or args.date
    if end < args.date:
        print("error: --until must be >= date", file=sys.stderr)
        return 2

    proxy: str | None = None
    if not args.no_proxy:
        proxy = args.proxy or os.environ.get("PROXY_URL") or None
    if proxy:
        print(f"using proxy {_redact_proxy(proxy)}", file=sys.stderr)

    uploader = _resolve_uploader(args)

    db: DB | None = None
    if not args.no_db:
        db = DB(args.db)
        print(f"db: {args.db}", file=sys.stderr)
        if args.retry_gone:
            n = db.reset_gone()
            print(f"reset {n} 'gone' issue(s) → 'pending'", file=sys.stderr)
    elif args.retry_gone:
        print("--retry-gone has no effect with --no-db", file=sys.stderr)

    today = datetime.now(timezone.utc).date()
    counters = {
        "uploaded": 0,
        "in_bucket": 0,
        "upload_errors": 0,
        "download_bytes": 0,
    }
    days_total = (end - args.date).days + 1
    totals = {"found": 0, "downloaded": 0, "failed": 0}
    total_errors = 0
    reporter = _ProgressReporter(days_total, totals, counters)

    def on_event(p: FileEventPayload) -> None:
        label = _FETCH_LABELS[p.kind]
        if p.kind == "error":
            reporter.print(f"  {label} {p.path.name}  ({p.detail})", err=True)
        else:
            reporter.print(f"  {label} {p.path.name}")

        if p.kind == "download" and p.size_bytes:
            counters["download_bytes"] += p.size_bytes

        if uploader is None or p.kind == "error":
            return
        if not (p.path.exists() and p.path.stat().st_size > 0):
            return
        if (
            db is not None
            and db.issue_status(p.day, p.issue.part, p.issue.number, p.issue.year)
            == "uploaded"
        ):
            counters["in_bucket"] += 1
            return
        try:
            result = uploader.upload_if_missing(p.path)
            if result.uploaded:
                counters["uploaded"] += 1
                reporter.print(f"  s3+   {p.path.name}")
            else:
                counters["in_bucket"] += 1
                reporter.print(f"  s3=   {p.path.name}")
            if db is not None:
                db.record_issue_uploaded(
                    p.day,
                    p.issue.part,
                    p.issue.number,
                    p.issue.year,
                    s3_etag=result.etag,
                )
        except Exception as exc:
            counters["upload_errors"] += 1
            reporter.print(f"  s3!   {p.path.name}  ({exc})", err=True)

    try:
        with reporter, _client(proxy=proxy) as client:
            for day in daterange(args.date, end, reverse=args.reverse):
                day_uploaded_before = counters["uploaded"]
                day_inbucket_before = counters["in_bucket"]
                day_uperr_before = counters["upload_errors"]
                r = scrape_day(
                    client,
                    day,
                    args.out,
                    part=args.part,
                    delay=args.delay,
                    on_event=on_event,
                    db=db,
                    today=today,
                    force=args.force,
                    rescrape_recent_days=args.rescrape_recent,
                )
                summary = _day_summary_line(
                    r,
                    counters["uploaded"] - day_uploaded_before,
                    counters["in_bucket"] - day_inbucket_before,
                    counters["upload_errors"] - day_uperr_before,
                )
                if summary is not None:
                    reporter.print(summary)
                total_errors += len(r.errors)
                totals["found"] += r.found
                totals["downloaded"] += r.downloaded
                totals["failed"] += len(r.errors)
                reporter.advance()
    except KeyboardInterrupt:
        print(
            "\ninterrupted: "
            + _progress_line(
                reporter.days_done,
                days_total,
                totals,
                counters,
                time.monotonic() - reporter.start,
            ),
            file=sys.stderr,
        )
        return 130
    finally:
        if db is not None:
            db.close()

    total_errors += counters["upload_errors"]
    return 1 if total_errors else 0


def _convert_summary_line(counters: dict[str, int], *, prefix: str = "") -> str:
    line = (
        f"{prefix}converted={counters['converted']} "
        f"skipped={counters['skipped']} errors={counters['errors']}"
    )
    if counters["uploaded"] or counters["in_bucket"] or counters["upload_errors"]:
        line += (
            f" | s3 uploaded={counters['uploaded']} "
            f"in-bucket={counters['in_bucket']} "
            f"errors={counters['upload_errors']}"
        )
    return line


def cmd_convert(args: argparse.Namespace) -> int:
    pdfs = collect_pdfs(list(args.paths), reverse=args.reverse)
    if not pdfs:
        print("no PDFs found", file=sys.stderr)
        return 0

    uploader = _resolve_uploader(args)
    counters = {
        "converted": 0,
        "skipped": 0,
        "errors": 0,
        "uploaded": 0,
        "in_bucket": 0,
        "upload_errors": 0,
    }

    with _ConvertProgressReporter(len(pdfs), counters) as report:

        def on_event(p: ConvertEventPayload) -> None:
            if p.kind == "skip":
                counters["skipped"] += 1
            elif p.kind == "convert":
                counters["converted"] += 1
            else:
                counters["errors"] += 1

            label = _CONVERT_LABELS[p.kind]
            if p.kind == "error":
                report.print(f"  {label} {p.md_path.name}  ({p.detail})", err=True)
            else:
                report.print(f"  {label} {p.md_path.name}")

            if uploader is not None and p.kind != "error":
                if p.md_path.exists() and p.md_path.stat().st_size > 0:
                    try:
                        result = uploader.upload_if_missing(
                            p.md_path, content_type="text/markdown"
                        )
                        if result.uploaded:
                            counters["uploaded"] += 1
                            report.print(f"  s3+   {p.md_path.name}")
                        else:
                            counters["in_bucket"] += 1
                            report.print(f"  s3=   {p.md_path.name}")
                    except Exception as exc:
                        counters["upload_errors"] += 1
                        report.print(f"  s3!   {p.md_path.name}  ({exc})", err=True)

            report.advance()

        try:
            convert_all(pdfs, force=args.force, workers=args.workers, on_event=on_event)
        except KeyboardInterrupt:
            report.print(
                _convert_summary_line(counters, prefix="interrupted: "),
                err=True,
            )
            return 130

    print(_convert_summary_line(counters))
    return 1 if counters["errors"] or counters["upload_errors"] else 0


def cmd_classify(args: argparse.Namespace) -> int:
    mds = collect_mds(list(args.paths), reverse=args.reverse)
    if not mds:
        print("no MDs found", file=sys.stderr)
        return 0

    counters: dict[str, int] = {}
    ambiguous = 0
    emitted = 0
    threshold = args.ambiguity_threshold

    for md in mds:
        try:
            result = classify_file(md)
        except Exception as exc:
            print(f"  ERR  {md}  ({exc})", file=sys.stderr)
            counters["error"] = counters.get("error", 0) + 1
            continue

        is_amb = result.is_ambiguous(threshold)
        is_other = result.top_type == "other"
        counters[result.top_type] = counters.get(result.top_type, 0) + 1
        if is_amb:
            ambiguous += 1

        if args.outliers and not (is_other or is_amb):
            continue

        row = {
            "file": str(md),
            "top_type": result.top_type,
            "top_score": result.top_score,
            "second_type": result.second_type,
            "second_score": result.second_score,
            "ambiguous": is_amb,
            "all_scores": result.all_scores,
            "matched_signals": result.matched_signals,
        }
        print(json.dumps(row, ensure_ascii=False))
        emitted += 1

    print(f"classified {len(mds)} docs:", file=sys.stderr)
    for t in sorted(counters, key=lambda k: -counters[k]):
        print(f"  {t:25s} {counters[t]:>5}", file=sys.stderr)
    if ambiguous:
        print(
            f"  ambiguous (margin < {threshold}): {ambiguous}",
            file=sys.stderr,
        )
    if args.outliers:
        print(f"  emitted (outliers only): {emitted}", file=sys.stderr)

    return 0


_EXTRACT_LABELS = {
    "extract": "ok   ",
    "skip": "skip ",
    "error": "ERR  ",
}
_EXTRACT_HEARTBEAT_EVERY = 50  # MDs, for `extract` in pipes


class _ExtractProgressReporter:
    """Live `rich` bar for `extract` when stderr is a tty; heartbeat otherwise.

    Mirrors `_ConvertProgressReporter` but speaks the extract vocabulary
    (extracted/skipped/errors + s3 trio + a coverage-pct rolling mean for
    the docs that actually extracted this run).
    """

    def __init__(self, total: int, counters: dict[str, int]) -> None:
        self.total = total
        self.counters = counters
        self.start = time.monotonic()
        self.done = 0
        self._tty = sys.stderr.isatty()
        self._progress = None
        self._task = None
        if self._tty and total > 0:
            from rich.console import Console
            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                TextColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )

            self._progress = Progress(
                BarColumn(bar_width=None),
                TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
                MofNCompleteColumn(),
                TextColumn("·"),
                TextColumn("[cyan]{task.description}"),
                TextColumn("·"),
                TimeElapsedColumn(),
                TextColumn("ETA"),
                TimeRemainingColumn(),
                console=Console(stderr=True),
                transient=False,
            )
            self._task = self._progress.add_task(self._desc(), total=total)

    def _desc(self) -> str:
        c = self.counters
        s = f"ok={c['extracted']:,} skip={c['skipped']:,} err={c['errors']:,}"
        if c["coverage_n"]:
            mean = c["coverage_sum"] / c["coverage_n"]
            s += f" · cov μ={mean:.3f}"
        if c["uploaded"] or c["in_bucket"] or c["upload_errors"]:
            s += (
                f" · s3 up={c['uploaded']:,} have={c['in_bucket']:,}"
                f" err={c['upload_errors']:,}"
            )
        return s

    def __enter__(self) -> "_ExtractProgressReporter":
        if self._progress is not None:
            self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._progress is not None:
            self._progress.stop()

    def print(self, line: str, *, err: bool = False) -> None:
        if self._progress is not None:
            self._progress.console.print(line, highlight=False)
            self._progress.update(self._task, description=self._desc())
        else:
            print(line, file=sys.stderr if err else sys.stdout)

    def advance(self) -> None:
        self.done += 1
        if self._progress is not None:
            self._progress.update(self._task, advance=1, description=self._desc())
            return
        if self.done % _EXTRACT_HEARTBEAT_EVERY == 0 and self.done < self.total:
            elapsed = time.monotonic() - self.start
            rate = self.done / elapsed if elapsed > 0 else 0.0
            eta = (self.total - self.done) / rate if rate > 0 else 0.0
            pct = self.done / self.total * 100 if self.total else 0.0
            print(
                f"progress: {self.done:,}/{self.total:,} ({pct:.1f}%) | "
                f"{self._desc()} | "
                f"elapsed={_fmt_duration(elapsed)} ETA={_fmt_duration(eta)}",
                file=sys.stderr,
            )


def _extract_summary_line(counters: dict[str, int], *, prefix: str = "") -> str:
    line = (
        f"{prefix}extracted={counters['extracted']} "
        f"skipped={counters['skipped']} errors={counters['errors']}"
    )
    if counters["coverage_n"]:
        mean = counters["coverage_sum"] / counters["coverage_n"]
        line += f" | cov mean={mean:.4f} (n={counters['coverage_n']})"
    if counters["uploaded"] or counters["in_bucket"] or counters["upload_errors"]:
        line += (
            f" | s3 uploaded={counters['uploaded']} "
            f"in-bucket={counters['in_bucket']} "
            f"errors={counters['upload_errors']}"
        )
    return line


def cmd_extract(args: argparse.Namespace) -> int:
    mds = collect_mds(list(args.paths), reverse=args.reverse)
    if not mds:
        print("no MDs found", file=sys.stderr)
        return 0

    uploader = _resolve_uploader(args)

    counters: dict[str, int] = {
        "extracted": 0,
        "skipped": 0,
        "errors": 0,
        "uploaded": 0,
        "in_bucket": 0,
        "upload_errors": 0,
        "coverage_sum": 0.0,
        "coverage_n": 0,
    }
    skip_reasons: dict[str, int] = {}
    threshold = args.coverage_below

    try:
        with _ExtractProgressReporter(len(mds), counters) as report:
            for md in mds:
                try:
                    result = _extract_md(
                        md,
                        force=args.force,
                        override_type=args.override_type,
                        identity_only=args.identity_only,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    counters["errors"] += 1
                    report.print(f"  ERR   {md.name}  ({exc!r})", err=True)
                    report.advance()
                    continue

                label = _EXTRACT_LABELS[result.status]
                if result.status == "extract":
                    counters["extracted"] += 1
                    report.print(
                        f"  {label} {result.sidecar_path.name}  "
                        f"[{result.doc_type}, cov={result.coverage_pct:.4f}]"
                    )
                    if result.coverage_pct is not None:
                        counters["coverage_sum"] += result.coverage_pct
                        counters["coverage_n"] += 1
                    if (
                        threshold is not None
                        and result.coverage_pct is not None
                        and result.coverage_pct < threshold
                    ):
                        _emit_coverage_outlier(result, report)
                elif result.status == "skip":
                    counters["skipped"] += 1
                    reason = result.reason or "unknown"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    report.print(
                        f"  {label} {md.name}  [{result.doc_type or '?'}, {reason}]"
                    )
                else:
                    counters["errors"] += 1
                    detail = result.reason or "unknown"
                    if result.rejected_path:
                        detail += f" (rejected dump: {result.rejected_path.name})"
                    report.print(f"  {label} {md.name}  ({detail})", err=True)

                if (
                    uploader is not None
                    and result.status == "extract"
                    and result.sidecar_path.exists()
                    and result.sidecar_path.stat().st_size > 0
                ):
                    try:
                        up = uploader.upload_if_missing(
                            result.sidecar_path,
                            content_type="application/json",
                            overwrite=True,
                        )
                        if up.uploaded:
                            counters["uploaded"] += 1
                            report.print(f"  s3+   {result.sidecar_path.name}")
                        else:
                            counters["in_bucket"] += 1
                            report.print(f"  s3=   {result.sidecar_path.name}")
                    except Exception as exc:
                        counters["upload_errors"] += 1
                        report.print(
                            f"  s3!   {result.sidecar_path.name}  ({exc})",
                            err=True,
                        )

                report.advance()
    except KeyboardInterrupt:
        print(
            _extract_summary_line(counters, prefix="\ninterrupted: "),
            file=sys.stderr,
        )
        return 130

    print(_extract_summary_line(counters))
    if skip_reasons:
        print("skip reasons:", file=sys.stderr)
        for reason, n in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {reason}: {n}", file=sys.stderr)
    return 1 if counters["errors"] or counters["upload_errors"] else 0


def _emit_coverage_outlier(result: object, report: object) -> None:
    """Print the coverage-outlier JSONL row to stdout for the discovery loop.

    Reads the just-written sidecar from disk so we get the gaps + previews
    that compute_coverage already produced, without re-deriving them here.
    """
    try:
        text = result.sidecar_path.read_text(encoding="utf-8")  # type: ignore[attr-defined]
        sidecar = json.loads(text)
    except Exception:
        return
    cov = sidecar.get("coverage") or {}
    gaps = cov.get("gaps") or []
    row = {
        "file": str(result.md_path),  # type: ignore[attr-defined]
        "doc_type": sidecar.get("document_type"),
        "claimed_pct": cov.get("claimed_pct"),
        "gap_count": len(gaps),
        "top_gaps": [
            {
                "lines": g.get("lines"),
                "chars": g.get("chars"),
                "preview": g.get("preview"),
            }
            for g in gaps[:3]
        ],
    }
    print(json.dumps(row, ensure_ascii=False))


_LINK_LABELS = {
    "linked": "ok   ",
    "skip": "skip ",
    "error": "ERROR",
}


def _collect_sidecars(paths: list[Path]) -> list[Path]:
    """Resolve a mix of files and directories into a sidecar list.

    Files must end with `.extraction.json`. Directories are globbed for
    `*.extraction.json` non-recursively (mirroring `collect_mds` for MDs).
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        p = raw if isinstance(raw, Path) else Path(raw)
        if p.is_dir():
            for child in sorted(p.glob("*.extraction.json")):
                if child not in seen:
                    seen.add(child)
                    out.append(child)
        elif p.is_file() and p.name.endswith(".extraction.json"):
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _run_report_pass(
    sidecars: list[Path],
    *,
    force: bool,
    write: bool,
    uploader: object | None,
    counters: dict[str, int],
    skip_reasons: dict[str, int],
) -> None:
    """Pass (1): link each report_facsimile to its receiving stenogram."""
    from monitorul_ii.extraction.linker import build_session_index, link_report

    session_index = build_session_index(sidecars)
    print(
        f"[report-pass] indexed {len(session_index)} receiving sessions "
        f"across {len(sidecars)} sidecars",
        file=sys.stderr,
    )

    for path in sidecars:
        try:
            with path.open(encoding="utf-8") as f:
                head = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if head.get("document_type") != "report_facsimile":
            continue
        try:
            result = link_report(
                path,
                session_index=session_index,
                force=force,
                write=write,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            counters["errors"] += 1
            print(f"  ERROR {path.name}  ({exc!r})", file=sys.stderr)
            continue

        label = _LINK_LABELS[result.status]
        line = f"  {label} {path.name}"
        if result.status == "linked":
            counters["linked"] += 1
            line += f"  -> {result.target_document_id}"
        elif result.status == "skip":
            counters["skipped"] += 1
            reason = result.reason or "unknown"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            line += f"  ({reason})"
        else:
            counters["errors"] += 1
            line += f"  ({result.reason or 'unknown'})"
        print(line, flush=True)
        if result.status == "error":
            print(line, file=sys.stderr, flush=True)

        if (
            uploader is not None
            and result.status == "linked"
            and write
            and path.exists()
        ):
            try:
                up = uploader.upload_if_missing(  # type: ignore[attr-defined]
                    path,
                    content_type="application/json",
                    overwrite=True,
                )
                if up.uploaded:
                    counters["uploaded"] += 1
                    print(f"  s3+   {path.name}")
                else:
                    counters["in_bucket"] += 1
                    print(f"  s3=   {path.name}")
            except Exception as exc:
                counters["upload_errors"] += 1
                print(f"  s3!   {path.name}  ({exc})", file=sys.stderr)


def _run_vote_pass(
    sidecars: list[Path],
    *,
    force: bool,
    write: bool,
    uploader: object | None,
    counters: dict[str, int],
    skip_reasons: dict[str, int],
) -> None:
    """Pass (2): pair deferred votes with their resolvers in later docs."""
    from monitorul_ii.extraction.linker import (
        build_vote_index,
        link_vote,
    )
    from monitorul_ii.extraction.linker import (
        _build_pairs as _linker_build_pairs,  # noqa: PLC2701
    )

    vote_index = build_vote_index(sidecars)
    forward_links, back_links = _linker_build_pairs(vote_index)
    print(
        f"[vote-pass] {len(forward_links)} forward + "
        f"{len(back_links)} back-link sites across "
        f"{sum(len(v) for v in vote_index.values())} indexed votes",
        file=sys.stderr,
    )

    # Set of paths that actually need an update — avoids re-parsing every
    # plenary sidecar when only a few got updates.
    touched: set[Path] = set()
    for key in forward_links.keys() | back_links.keys():
        for entries in vote_index.values():
            for e in entries:
                if (e.document_id, e.agenda_index, e.activity_index) == key:
                    touched.add(e.sidecar_path)
                    break

    for path in sidecars:
        if path not in touched:
            continue
        try:
            result = link_vote(
                path,
                forward_links=forward_links,
                back_links=back_links,
                force=force,
                write=write,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            counters["errors"] += 1
            print(f"  ERROR {path.name}  ({exc!r})", file=sys.stderr)
            continue

        label = _LINK_LABELS[result.status]
        line = f"  {label} {path.name}"
        if result.status == "linked":
            counters["linked"] += 1
            line += f"  forward={result.pairs_written} back={result.backlinks_written}"
        elif result.status == "skip":
            counters["skipped"] += 1
            reason = result.reason or "unknown"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            line += f"  ({reason})"
        else:
            counters["errors"] += 1
            line += f"  ({result.reason or 'unknown'})"
        print(line, flush=True)
        if result.status == "error":
            print(line, file=sys.stderr, flush=True)

        if (
            uploader is not None
            and result.status == "linked"
            and write
            and path.exists()
        ):
            try:
                up = uploader.upload_if_missing(  # type: ignore[attr-defined]
                    path,
                    content_type="application/json",
                    overwrite=True,
                )
                if up.uploaded:
                    counters["uploaded"] += 1
                    print(f"  s3+   {path.name}")
                else:
                    counters["in_bucket"] += 1
                    print(f"  s3=   {path.name}")
            except Exception as exc:
                counters["upload_errors"] += 1
                print(f"  s3!   {path.name}  ({exc})", file=sys.stderr)


def _run_xref_pass(
    sidecars: list[Path],
    *,
    force: bool,
    write: bool,
    uploader: object | None,
    counters: dict[str, int],
    skip_reasons: dict[str, int],
) -> None:
    """Pass (3): resolve bare `art. N` unknowns to their owning anchor."""
    from monitorul_ii.extraction.cross_reference_linker import link_xrefs

    eligible = [p for p in sidecars if p.name.endswith(".extraction.json")]
    print(
        f"[xref-pass] running over {len(eligible)} sidecars "
        f"(plenary + question_register only)",
        file=sys.stderr,
    )

    total_resolved = 0
    total_unresolved = 0
    for path in eligible:
        try:
            result = link_xrefs(path, force=force, write=write)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            counters["errors"] += 1
            print(f"  ERROR {path.name}  ({exc!r})", file=sys.stderr)
            continue

        label = _LINK_LABELS[result.status]
        line = f"  {label} {path.name}"
        if result.status == "linked":
            counters["linked"] += 1
            total_resolved += result.resolved
            total_unresolved += result.unresolved
            line += (
                f"  resolved={result.resolved} "
                f"unresolved={result.unresolved} "
                f"already={result.skipped_already}"
            )
        elif result.status == "skip":
            counters["skipped"] += 1
            reason = result.reason or "unknown"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            line += f"  ({reason})"
            total_unresolved += result.unresolved
        else:
            counters["errors"] += 1
            line += f"  ({result.reason or 'unknown'})"
        print(line, flush=True)
        if result.status == "error":
            print(line, file=sys.stderr, flush=True)

        if (
            uploader is not None
            and result.status == "linked"
            and write
            and path.exists()
        ):
            try:
                up = uploader.upload_if_missing(  # type: ignore[attr-defined]
                    path,
                    content_type="application/json",
                    overwrite=True,
                )
                if up.uploaded:
                    counters["uploaded"] += 1
                    print(f"  s3+   {path.name}")
                else:
                    counters["in_bucket"] += 1
                    print(f"  s3=   {path.name}")
            except Exception as exc:
                counters["upload_errors"] += 1
                print(f"  s3!   {path.name}  ({exc})", file=sys.stderr)

    grand_total = total_resolved + total_unresolved
    if grand_total:
        pct = total_resolved / grand_total * 100
        print(
            f"[xref-pass] resolved={total_resolved} unresolved={total_unresolved} "
            f"({pct:.1f}% resolution rate)",
            file=sys.stderr,
        )


def cmd_link(args: argparse.Namespace) -> int:
    sidecars = _collect_sidecars(list(args.paths))
    if not sidecars:
        print("no .extraction.json files found", file=sys.stderr)
        return 0

    uploader = _resolve_uploader(args) if not args.dry_run else None

    counters: dict[str, int] = {
        "linked": 0,
        "skipped": 0,
        "errors": 0,
        "uploaded": 0,
        "in_bucket": 0,
        "upload_errors": 0,
    }
    skip_reasons: dict[str, int] = {}

    report_only = getattr(args, "report_only", False)
    vote_only = getattr(args, "vote_only", False)
    xref_only = getattr(args, "xref_only", False)
    any_only = report_only or vote_only or xref_only
    run_report = report_only or not any_only
    run_vote = vote_only or not any_only
    run_xref = xref_only or not any_only

    try:
        if run_report:
            _run_report_pass(
                sidecars,
                force=args.force,
                write=not args.dry_run,
                uploader=uploader,
                counters=counters,
                skip_reasons=skip_reasons,
            )
        if run_vote:
            _run_vote_pass(
                sidecars,
                force=args.force,
                write=not args.dry_run,
                uploader=uploader,
                counters=counters,
                skip_reasons=skip_reasons,
            )
        if run_xref:
            _run_xref_pass(
                sidecars,
                force=args.force,
                write=not args.dry_run,
                uploader=uploader,
                counters=counters,
                skip_reasons=skip_reasons,
            )
    except KeyboardInterrupt:
        print(
            f"\ninterrupted: linked={counters['linked']} "
            f"skipped={counters['skipped']} errors={counters['errors']}",
            file=sys.stderr,
        )
        return 130

    summary = (
        f"linked={counters['linked']} "
        f"skipped={counters['skipped']} "
        f"errors={counters['errors']}"
    )
    if counters["uploaded"] or counters["in_bucket"] or counters["upload_errors"]:
        summary += (
            f" | s3 uploaded={counters['uploaded']} "
            f"in-bucket={counters['in_bucket']} "
            f"errors={counters['upload_errors']}"
        )
    print(summary, flush=True)
    if skip_reasons:
        print("skip reasons:", file=sys.stderr)
        for reason, n in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {reason}: {n}", file=sys.stderr)
    return 1 if counters["errors"] or counters["upload_errors"] else 0


_BACKFILL_LABELS = {
    "filled": "ok   ",
    "skip": "skip ",
    "error": "ERROR",
}

_BACKFILL_HEARTBEAT_EVERY = 100  # sidecars, for backfill in pipes


class _BackfillProgressReporter:
    """Live `rich` bar for one backfill pass when stderr is a tty;
    heartbeat every `_BACKFILL_HEARTBEAT_EVERY` results otherwise.

    Per-pass instance — each `_run_*_backfill` helper opens one for its
    own bar / counters scope. The shared `counters` dict carries
    fill/skip/err/s3 counts across passes for the final summary; the
    bar's description string reads them live so a multi-pass run shows
    the running totals as each pass progresses.

    Mirrors `_ExtractProgressReporter` / `_ConvertProgressReporter` for
    consistency; the only pass-specific bit is the leading `[<pass>]`
    label in the description.
    """

    def __init__(self, total: int, counters: dict[str, int], pass_label: str) -> None:
        self.total = total
        self.counters = counters
        self.pass_label = pass_label
        self.start = time.monotonic()
        self.done = 0
        self._tty = sys.stderr.isatty()
        self._progress = None
        self._task = None
        if self._tty and total > 0:
            from rich.console import Console
            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                TextColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )

            self._progress = Progress(
                BarColumn(bar_width=None),
                TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
                MofNCompleteColumn(),
                TextColumn("·"),
                TextColumn("[cyan]{task.description}"),
                TextColumn("·"),
                TimeElapsedColumn(),
                TextColumn("ETA"),
                TimeRemainingColumn(),
                console=Console(stderr=True),
                transient=False,
            )
            self._task = self._progress.add_task(self._desc(), total=total)

    def _desc(self) -> str:
        c = self.counters
        s = (
            f"[{self.pass_label}] fill={c['filled']:,} "
            f"skip={c['skipped']:,} err={c['errors']:,}"
        )
        if c["uploaded"] or c["in_bucket"] or c["upload_errors"]:
            s += (
                f" · s3 up={c['uploaded']:,} have={c['in_bucket']:,}"
                f" err={c['upload_errors']:,}"
            )
        return s

    def __enter__(self) -> "_BackfillProgressReporter":
        if self._progress is not None:
            self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._progress is not None:
            self._progress.stop()

    def print(self, line: str, *, err: bool = False) -> None:
        if self._progress is not None:
            self._progress.console.print(line, highlight=False)
            self._progress.update(self._task, description=self._desc())
        else:
            print(line, file=sys.stderr if err else sys.stdout, flush=True)

    def advance(self) -> None:
        self.done += 1
        if self._progress is not None:
            self._progress.update(self._task, advance=1, description=self._desc())
            return
        if self.done % _BACKFILL_HEARTBEAT_EVERY == 0 and self.done < self.total:
            elapsed = time.monotonic() - self.start
            rate = self.done / elapsed if elapsed > 0 else 0.0
            eta = (self.total - self.done) / rate if rate > 0 else 0.0
            pct = self.done / self.total * 100 if self.total else 0.0
            print(
                f"progress: {self.done:,}/{self.total:,} ({pct:.1f}%) | "
                f"{self._desc()} | "
                f"elapsed={_fmt_duration(elapsed)} ETA={_fmt_duration(eta)}",
                file=sys.stderr,
                flush=True,
            )


def _should_upload_after_backfill(result: object, *, reupload_on_skip: bool) -> bool:
    """Decide whether to push a backfill result's sidecar to S3.

    Default: upload when status="filled" (the local file got rewritten).
    With `reupload_on_skip=True`: also upload when status="skip" AND
    reason starts with "already filled" — the local-side decision was
    "no work needed" (every slot already holds the current canonical
    id), but the bucket may carry stale bytes from a pre-overwrite-fix
    run. Other skip reasons (`no raw value`, `no speakers in body`,
    `no registry match`, `no government-proposed agendas`) stay
    upload-skipped — those mean the local file simply doesn't carry
    data this pass emits, so the bucket can't be "stale" relative to
    one. Errors never upload.

    The reason check uses `startswith("already filled")` so all four
    pass-specific formats match: issuing_body's literal "already
    filled with same canonical id"; ministry / proposed_by / persons
    aggregated forms like "already filled (N records|votes|speakers)".
    """
    status = getattr(result, "status", None)
    if status == "filled":
        return True
    if (
        reupload_on_skip
        and status == "skip"
        and (getattr(result, "reason", None) or "").startswith("already filled")
    ):
        return True
    return False


def _run_issuing_body_backfill(
    sidecars: list[Path],
    *,
    force: bool,
    write: bool,
    uploader: object | None,
    counters: dict[str, int],
    skip_reasons: dict[str, int],
    matched_via_counts: dict[str, int],
    workers: int = 1,
    reupload_on_skip: bool = False,
) -> None:
    """Pass: report_facsimile.issuing_body → issuing_body_normalized."""
    from monitorul_ii.extraction.backfills import (
        backfill_all_issuing_bodies,
        backfill_all_issuing_bodies_parallel,
    )

    n_reports = sum(1 for p in sidecars if _read_doctype(p) == "report_facsimile")
    suffix = f" (workers={workers})" if workers > 1 else ""
    print(
        f"[issuing_body] running over {n_reports} report_facsimile sidecars "
        f"(of {len(sidecars)} total){suffix}",
        file=sys.stderr,
    )

    if workers > 1:
        results = backfill_all_issuing_bodies_parallel(
            sidecars, force=force, write=write, workers=workers
        )
    else:
        results = backfill_all_issuing_bodies(sidecars, force=force, write=write)

    with _BackfillProgressReporter(
        total=len(sidecars), counters=counters, pass_label="issuing_body"
    ) as report:
        for result in results:
            label = _BACKFILL_LABELS[result.status]
            line = f"  {label} {result.sidecar_path.name}"
            if result.status == "filled":
                counters["filled"] += 1
                matched_via_counts[result.matched_via or "unknown"] = (
                    matched_via_counts.get(result.matched_via or "unknown", 0) + 1
                )
                line += (
                    f"  -> {result.canonical_id}  [{result.matched_via}]"
                    f"  (raw={result.raw_value!r})"
                )
            elif result.status == "skip":
                counters["skipped"] += 1
                reason = result.reason or "unknown"
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                line += f"  ({reason})"
                if result.raw_value and reason == "no registry match":
                    line += f"  raw={result.raw_value!r}"
            else:
                counters["errors"] += 1
                line += f"  ({result.reason or 'unknown'})"
            report.print(line, err=(result.status == "error"))

            if (
                uploader is not None
                and write
                and result.sidecar_path.exists()
                and _should_upload_after_backfill(
                    result, reupload_on_skip=reupload_on_skip
                )
            ):
                try:
                    up = uploader.upload_if_missing(  # type: ignore[attr-defined]
                        result.sidecar_path,
                        content_type="application/json",
                        overwrite=True,
                    )
                    if up.uploaded:
                        counters["uploaded"] += 1
                        report.print(f"  s3+   {result.sidecar_path.name}")
                    else:
                        counters["in_bucket"] += 1
                        report.print(f"  s3=   {result.sidecar_path.name}")
                except Exception as exc:
                    counters["upload_errors"] += 1
                    report.print(
                        f"  s3!   {result.sidecar_path.name}  ({exc})",
                        err=True,
                    )

            report.advance()


def _read_doctype(path: Path) -> str | None:
    """Cheap document_type peek for the per-pass progress header."""
    try:
        with path.open(encoding="utf-8") as f:
            head = json.load(f)
        return head.get("document_type")
    except (OSError, json.JSONDecodeError):
        return None


def _run_ministry_backfill(
    sidecars: list[Path],
    *,
    force: bool,
    write: bool,
    uploader: object | None,
    counters: dict[str, int],
    skip_reasons: dict[str, int],
    matched_via_counts: dict[str, int],
    workers: int = 1,
    reupload_on_skip: bool = False,
) -> None:
    """Pass: qr/plenary addressee → ministry/addressed_to normalized."""
    from monitorul_ii.extraction.backfills import (
        backfill_all_ministries,
        backfill_all_ministries_parallel,
    )

    n_relevant = sum(
        1
        for p in sidecars
        if _read_doctype(p)
        in ("question_register", "plenary_stenogram", "plenary_joint_session")
    )
    suffix = f" (workers={workers})" if workers > 1 else ""
    print(
        f"[ministry] running over {n_relevant} ministry-bearing sidecars "
        f"(of {len(sidecars)} total){suffix}",
        file=sys.stderr,
    )

    if workers > 1:
        results = backfill_all_ministries_parallel(
            sidecars, force=force, write=write, workers=workers
        )
    else:
        results = backfill_all_ministries(sidecars, force=force, write=write)

    with _BackfillProgressReporter(
        total=len(sidecars), counters=counters, pass_label="ministry"
    ) as report:
        for result in results:
            label = _BACKFILL_LABELS[result.status]
            line = f"  {label} {result.sidecar_path.name}"
            if result.status == "filled":
                counters["filled"] += 1
                matched_via_counts[result.matched_via or "unknown"] = (
                    matched_via_counts.get(result.matched_via or "unknown", 0) + 1
                )
                line += f"  [{result.reason}]"
                if result.canonical_id and result.matched_via:
                    line += f"  first={result.canonical_id} via={result.matched_via}"
            elif result.status == "skip":
                counters["skipped"] += 1
                reason = result.reason or "unknown"
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                line += f"  ({reason})"
            else:
                counters["errors"] += 1
                line += f"  ({result.reason or 'unknown'})"
            report.print(line, err=(result.status == "error"))

            if (
                uploader is not None
                and write
                and result.sidecar_path.exists()
                and _should_upload_after_backfill(
                    result, reupload_on_skip=reupload_on_skip
                )
            ):
                try:
                    up = uploader.upload_if_missing(  # type: ignore[attr-defined]
                        result.sidecar_path,
                        content_type="application/json",
                        overwrite=True,
                    )
                    if up.uploaded:
                        counters["uploaded"] += 1
                        report.print(f"  s3+   {result.sidecar_path.name}")
                    else:
                        counters["in_bucket"] += 1
                        report.print(f"  s3=   {result.sidecar_path.name}")
                except Exception as exc:
                    counters["upload_errors"] += 1
                    report.print(
                        f"  s3!   {result.sidecar_path.name}  ({exc})",
                        err=True,
                    )

            report.advance()


def _run_proposed_by_backfill(
    sidecars: list[Path],
    *,
    force: bool,
    write: bool,
    uploader: object | None,
    counters: dict[str, int],
    skip_reasons: dict[str, int],
    workers: int = 1,
    reupload_on_skip: bool = False,
) -> None:
    """Pass: plenary votes → proposed_by=Guvern (when OUG/OG-derived)."""
    from monitorul_ii.extraction.backfills import (
        backfill_all_proposed_by,
        backfill_all_proposed_by_parallel,
    )

    n_relevant = sum(
        1
        for p in sidecars
        if _read_doctype(p) in ("plenary_stenogram", "plenary_joint_session")
    )
    suffix = f" (workers={workers})" if workers > 1 else ""
    print(
        f"[proposed_by] running over {n_relevant} plenary sidecars "
        f"(of {len(sidecars)} total){suffix}",
        file=sys.stderr,
    )

    if workers > 1:
        results = backfill_all_proposed_by_parallel(
            sidecars, force=force, write=write, workers=workers
        )
    else:
        results = backfill_all_proposed_by(sidecars, force=force, write=write)

    with _BackfillProgressReporter(
        total=len(sidecars), counters=counters, pass_label="proposed_by"
    ) as report:
        for result in results:
            label = _BACKFILL_LABELS[result.status]
            line = f"  {label} {result.sidecar_path.name}"
            if result.status == "filled":
                counters["filled"] += 1
                line += f"  [{result.reason}]"
            elif result.status == "skip":
                counters["skipped"] += 1
                reason = result.reason or "unknown"
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                line += f"  ({reason})"
            else:
                counters["errors"] += 1
                line += f"  ({result.reason or 'unknown'})"
            report.print(line, err=(result.status == "error"))

            if (
                uploader is not None
                and write
                and result.sidecar_path.exists()
                and _should_upload_after_backfill(
                    result, reupload_on_skip=reupload_on_skip
                )
            ):
                try:
                    up = uploader.upload_if_missing(  # type: ignore[attr-defined]
                        result.sidecar_path,
                        content_type="application/json",
                        overwrite=True,
                    )
                    if up.uploaded:
                        counters["uploaded"] += 1
                        report.print(f"  s3+   {result.sidecar_path.name}")
                    else:
                        counters["in_bucket"] += 1
                        report.print(f"  s3=   {result.sidecar_path.name}")
                except Exception as exc:
                    counters["upload_errors"] += 1
                    report.print(
                        f"  s3!   {result.sidecar_path.name}  ({exc})",
                        err=True,
                    )

            report.advance()


def _run_persons_backfill(
    sidecars: list[Path],
    *,
    force: bool,
    write: bool,
    uploader: object | None,
    counters: dict[str, int],
    skip_reasons: dict[str, int],
    matched_via_counts: dict[str, int],
    workers: int = 1,
    reupload_on_skip: bool = False,
) -> None:
    """Pass: every Speaker dict → person_id from persons.json registry."""
    from monitorul_ii.extraction.backfills import (
        backfill_all_persons,
        backfill_all_persons_parallel,
    )

    suffix = f" (workers={workers})" if workers > 1 else ""
    print(
        f"[persons] running over {len(sidecars)} sidecars "
        f"(every doc type carries Speaker dicts){suffix}",
        file=sys.stderr,
    )

    if workers > 1:
        results = backfill_all_persons_parallel(
            sidecars, force=force, write=write, workers=workers
        )
    else:
        results = backfill_all_persons(sidecars, force=force, write=write)

    with _BackfillProgressReporter(
        total=len(sidecars), counters=counters, pass_label="persons"
    ) as report:
        for result in results:
            label = _BACKFILL_LABELS[result.status]
            line = f"  {label} {result.sidecar_path.name}"
            if result.status == "filled":
                counters["filled"] += 1
                matched_via_counts[result.matched_via or "unknown"] = (
                    matched_via_counts.get(result.matched_via or "unknown", 0) + 1
                )
                line += f"  [{result.reason}]"
                if result.canonical_id and result.matched_via:
                    line += f"  first={result.canonical_id} via={result.matched_via}"
            elif result.status == "skip":
                counters["skipped"] += 1
                reason = result.reason or "unknown"
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                line += f"  ({reason})"
            else:
                counters["errors"] += 1
                line += f"  ({result.reason or 'unknown'})"
            report.print(line, err=(result.status == "error"))

            if (
                uploader is not None
                and write
                and result.sidecar_path.exists()
                and _should_upload_after_backfill(
                    result, reupload_on_skip=reupload_on_skip
                )
            ):
                try:
                    up = uploader.upload_if_missing(  # type: ignore[attr-defined]
                        result.sidecar_path,
                        content_type="application/json",
                        overwrite=True,
                    )
                    if up.uploaded:
                        counters["uploaded"] += 1
                        report.print(f"  s3+   {result.sidecar_path.name}")
                    else:
                        counters["in_bucket"] += 1
                        report.print(f"  s3=   {result.sidecar_path.name}")
                except Exception as exc:
                    counters["upload_errors"] += 1
                    report.print(
                        f"  s3!   {result.sidecar_path.name}  ({exc})",
                        err=True,
                    )

            report.advance()


def cmd_backfill(args: argparse.Namespace) -> int:
    sidecars = _collect_sidecars(list(args.paths))
    if not sidecars:
        print("no .extraction.json files found", file=sys.stderr)
        return 0

    uploader = _resolve_uploader(args) if not args.dry_run else None

    counters: dict[str, int] = {
        "filled": 0,
        "skipped": 0,
        "errors": 0,
        "uploaded": 0,
        "in_bucket": 0,
        "upload_errors": 0,
    }
    skip_reasons: dict[str, int] = {}
    matched_via_counts: dict[str, int] = {}

    run_issuing_body = args.kind in ("issuing_body", "all")
    run_ministry = args.kind in ("ministry", "all")
    run_proposed_by = args.kind in ("proposed_by", "all")
    run_persons = args.kind in ("persons", "all")

    # Default to 1 when the namespace doesn't carry --workers (some
    # test harnesses build SimpleNamespace directly instead of going
    # through the parser). Same pattern for reupload_on_skip.
    workers = max(1, int(getattr(args, "workers", 1) or 1))
    reupload_on_skip = bool(getattr(args, "reupload_on_skip", False))
    try:
        if run_issuing_body:
            _run_issuing_body_backfill(
                sidecars,
                force=args.force,
                write=not args.dry_run,
                uploader=uploader,
                counters=counters,
                skip_reasons=skip_reasons,
                matched_via_counts=matched_via_counts,
                workers=workers,
                reupload_on_skip=reupload_on_skip,
            )
        if run_ministry:
            _run_ministry_backfill(
                sidecars,
                force=args.force,
                write=not args.dry_run,
                uploader=uploader,
                counters=counters,
                skip_reasons=skip_reasons,
                matched_via_counts=matched_via_counts,
                workers=workers,
                reupload_on_skip=reupload_on_skip,
            )
        if run_proposed_by:
            _run_proposed_by_backfill(
                sidecars,
                force=args.force,
                write=not args.dry_run,
                uploader=uploader,
                counters=counters,
                skip_reasons=skip_reasons,
                workers=workers,
                reupload_on_skip=reupload_on_skip,
            )
        if run_persons:
            _run_persons_backfill(
                sidecars,
                force=args.force,
                write=not args.dry_run,
                uploader=uploader,
                counters=counters,
                skip_reasons=skip_reasons,
                matched_via_counts=matched_via_counts,
                workers=workers,
                reupload_on_skip=reupload_on_skip,
            )
    except KeyboardInterrupt:
        # First Ctrl+C: graceful exit. Print summary, return 130, let
        # the multiprocessing atexit handler join workers (they ignore
        # SIGINT — see backfills._worker_ignore_sigint — and finish
        # their current sidecar atomically; a few seconds in the
        # steady state).
        #
        # Subsequent Ctrl+Cs: hard escape. Install a handler that
        # calls `os._exit(130)` on the next SIGINT. The user gets out
        # immediately if workers are stuck in init or some atexit
        # handler is blocking longer than they're willing to wait.
        # Trade-off: orphaned workers may leave `.part` files behind
        # (the atomic-rename contract means the canonical sidecars
        # are still safe — the `.part` is the sacrificial scratch
        # file mid-write). A subsequent run reads through them
        # cleanly, and `find pdfs/ -name '*.part' -delete` reaps any
        # leftovers.
        import os as _os
        import signal as _signal

        def _hard_exit(*_args: object) -> None:
            _os._exit(130)

        _signal.signal(_signal.SIGINT, _hard_exit)
        print(
            f"\ninterrupted: filled={counters['filled']} "
            f"skipped={counters['skipped']} errors={counters['errors']} "
            "(Ctrl+C again to force-exit immediately)",
            file=sys.stderr,
        )
        return 130

    summary = (
        f"filled={counters['filled']} "
        f"skipped={counters['skipped']} "
        f"errors={counters['errors']}"
    )
    if counters["uploaded"] or counters["in_bucket"] or counters["upload_errors"]:
        summary += (
            f" | s3 uploaded={counters['uploaded']} "
            f"in-bucket={counters['in_bucket']} "
            f"errors={counters['upload_errors']}"
        )
    print(summary, flush=True)
    if matched_via_counts:
        print("matched_via:", file=sys.stderr)
        for via, n in sorted(matched_via_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {via}: {n}", file=sys.stderr)
    if skip_reasons:
        print("skip reasons:", file=sys.stderr)
        for reason, n in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {reason}: {n}", file=sys.stderr)
    return 1 if counters["errors"] or counters["upload_errors"] else 0


def _print_es_init_dry_run(suffix: str) -> None:
    """Render the same plan the live bootstrap would execute.

    The plan is *deterministic* given the suffix — no ES call needed —
    so the dry-run never has to authenticate against the cluster.
    """
    print("dry-run: would create the following Elasticsearch entities:")
    print("  component_templates:")
    print(f"    - {es_bootstrap.COMPONENT_ANALYZERS}")
    print(f"    - {es_bootstrap.COMPONENT_COMMON_FIELDS}")
    print("  index_templates:")
    for grain in es_bootstrap.GRAINS:
        print(f"    - {grain}-template")
    print("  indices (with read+write aliases):")
    for grain in es_bootstrap.GRAINS:
        index_name = f"{grain}-{suffix}"
        print(f"    - {index_name}  (aliases: {grain}, {grain}-write)")
    print("  api_keys:")
    print(f"    - {es_bootstrap.API_KEY_READER}  (read on mo-*)")
    print(f"    - {es_bootstrap.API_KEY_INDEXER}  (read+write on mo-*)")


def cmd_es_init(args: argparse.Namespace) -> int:
    suffix = args.generation_suffix or es_bootstrap._generation_suffix()
    if args.dry_run:
        _print_es_init_dry_run(suffix)
        return 0

    cfg = ESConfig.from_env()
    if cfg is None:
        print(
            "es-init: missing ES_URL or ES_API_KEY in environment "
            "(set both, or pass --dry-run for a no-cluster preview)",
            file=sys.stderr,
        )
        return 2

    es = _build_es_client(cfg)
    print(f"es: {cfg.url} (verify_certs={cfg.verify_certs})")

    if args.update_mappings:
        # `--update-mappings` is operationally a different action than the
        # bootstrap dance — additive PUT _mapping against existing live
        # indices, no template changes, no API keys, no smoke. Short-
        # circuit before the rest of the bootstrap dance.
        for entity in es_bootstrap.update_live_mappings(es):
            marker = "+" if entity.created else "-"
            detail = f"  ({entity.detail})" if entity.detail else ""
            print(f"  {marker} mapping             {entity.name}{detail}")
        return 0

    # Decompose the bootstrap so an API-key failure (e.g. derived
    # bootstrap keys, which ES refuses to use as a creator for keys
    # carrying explicit role descriptors) doesn't block the smoke
    # round-trip — templates + indices are the load-bearing wiring,
    # API-keys are operational extras.
    for entity in es_bootstrap.create_component_templates(es):
        marker = "+" if entity.created else "="
        print(f"  {marker} component_template  {entity.name}")
    for entity in es_bootstrap.create_index_templates(es):
        marker = "+" if entity.created else "="
        print(f"  {marker} index_template      {entity.name}")
    for entity in es_bootstrap.create_indices(es, generation_suffix=suffix):
        marker = "+" if entity.created else "="
        detail = f"  ({entity.detail})" if entity.detail else ""
        print(f"  {marker} index               {entity.name}{detail}")

    api_key_error: Exception | None = None
    api_keys: dict[str, dict[str, str]] = {}
    try:
        api_keys = es_bootstrap.create_api_keys(es)
    except Exception as exc:  # noqa: BLE001 — bubble to the user
        api_key_error = exc

    if api_key_error is not None:
        print("")
        print(
            "  ! api_keys           NOT minted — "
            f"{type(api_key_error).__name__}: {api_key_error}",
            file=sys.stderr,
        )
        print(
            "    (the bootstrap ES_API_KEY may itself be a derived API key. "
            "Re-run es-init with a primary credential — username/password "
            "or a non-derived API key — to mint the role-scoped keys.)",
            file=sys.stderr,
        )
    elif api_keys:
        print("")
        print("api keys (SAVE THESE — ES will not return the encoded value again):")
        for name, key in api_keys.items():
            print(f"  {name}:")
            print(f"    id:      {key['id']}")
            print(f"    encoded: {key['encoded']}")
    else:
        print("  = api_keys           (already provisioned; values not retrievable)")

    if args.skip_smoke:
        print("smoke: skipped (--skip-smoke)")
        return 1 if api_key_error is not None else 0

    test_id = "mo://test/PII/0"
    try:
        ok = es_bootstrap.smoke_roundtrip(es)
    except Exception as exc:  # noqa: BLE001 — bubble specific cause
        print(f"smoke: indexed {test_id} → ERROR: {exc}", file=sys.stderr)
        return 1
    status = "ok" if ok else "MISMATCH"
    print(f"smoke: indexed {test_id} → retrieved → match: {status}")
    if not ok:
        return 1
    # Templates + indices + smoke all green — the load-bearing wiring is
    # confirmed. A failed api-key step still warrants a non-zero exit so
    # the operator notices, but the cluster is usable for the indexer.
    return 1 if api_key_error is not None else 0


_INDEX_HEARTBEAT_EVERY = 50


class _IndexProgressReporter:
    """Live `rich` bar for `index` when stderr is a tty; heartbeat
    every `_INDEX_HEARTBEAT_EVERY` results in pipes / cron.

    Mirrors the other reporters (`_ExtractProgressReporter`,
    `_BackfillProgressReporter`); the only vocabulary difference is
    `idx/skip/orphans/err` instead of fill / converted / extracted.
    """

    def __init__(self, total: int, counters: dict[str, int]) -> None:
        self.total = total
        self.counters = counters
        self.start = time.monotonic()
        self.done = 0
        self._tty = sys.stderr.isatty()
        self._progress = None
        self._task = None
        if self._tty and total > 0:
            from rich.console import Console
            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                TextColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )

            self._progress = Progress(
                BarColumn(bar_width=None),
                TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
                MofNCompleteColumn(),
                TextColumn("·"),
                TextColumn("[cyan]{task.description}"),
                TextColumn("·"),
                TimeElapsedColumn(),
                TextColumn("ETA"),
                TimeRemainingColumn(),
                console=Console(stderr=True),
                transient=False,
            )
            self._task = self._progress.add_task(self._desc(), total=total)

    def _desc(self) -> str:
        c = self.counters
        return (
            f"idx={c['indexed']:,} skip={c['skipped']:,} "
            f"orphans={c['orphans_deleted']:,} err={c['errors']:,}"
        )

    def __enter__(self) -> "_IndexProgressReporter":
        if self._progress is not None:
            self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._progress is not None:
            self._progress.stop()

    def print(self, line: str, *, err: bool = False) -> None:
        if self._progress is not None:
            self._progress.console.print(line, highlight=False)
            self._progress.update(self._task, description=self._desc())
        else:
            print(line, file=sys.stderr if err else sys.stdout, flush=True)

    def advance(self) -> None:
        self.done += 1
        if self._progress is not None:
            self._progress.update(self._task, advance=1, description=self._desc())
            return
        if self.done % _INDEX_HEARTBEAT_EVERY == 0 and self.done < self.total:
            elapsed = time.monotonic() - self.start
            rate = self.done / elapsed if elapsed > 0 else 0.0
            eta = (self.total - self.done) / rate if rate > 0 else 0.0
            pct = self.done / self.total * 100 if self.total else 0.0
            print(
                f"progress: {self.done:,}/{self.total:,} ({pct:.1f}%) | "
                f"{self._desc()} | "
                f"elapsed={_fmt_duration(elapsed)} ETA={_fmt_duration(eta)}",
                file=sys.stderr,
                flush=True,
            )


def cmd_index(args: argparse.Namespace) -> int:
    sidecars = _collect_sidecars(list(args.paths))
    if not sidecars:
        print("no .extraction.json files found", file=sys.stderr)
        return 0

    # `--rebuild` is the operator's "force a full re-index against this
    # generation" shortcut; it implies --force and demands --target.
    if args.rebuild and not args.target:
        print(
            "index: --rebuild requires --target=<generation> "
            "(otherwise it would force-rewrite the live alias, which "
            "is the daily indexer's job — use `--force` for that).",
            file=sys.stderr,
        )
        return 2
    force = args.force or args.rebuild

    grains_filter: tuple[str, ...] | None = tuple(args.grain) if args.grain else None

    cfg = ESConfig.from_env()
    if cfg is None and not args.dry_run:
        print(
            "index: missing ES_URL or ES_API_KEY in environment "
            "(set both, or pass --dry-run for a no-cluster preview)",
            file=sys.stderr,
        )
        return 2

    es = _build_es_client(cfg) if (cfg is not None and not args.dry_run) else None
    if es is not None:
        print(f"es: {cfg.url} (verify_certs={cfg.verify_certs})")

    counters: dict[str, int] = {
        "indexed": 0,
        "skipped": 0,
        "orphans_deleted": 0,
        "errors": 0,
        "dry_run": 0,
    }

    from monitorul_ii.elasticsearch.indexer import (
        index_all_parallel as _index_all_parallel,
    )
    from monitorul_ii.elasticsearch.indexer import index_one as _index_one

    workers = max(1, int(getattr(args, "workers", 1) or 1))

    def _handle(result, *, report) -> None:
        """Project one IndexResult into counters + log lines.

        Shared between the sequential and parallel paths so output
        formatting stays consistent regardless of `-j N`.
        """
        if result.action == "indexed":
            counters["indexed"] += 1
            grain_summary = " ".join(
                f"{g.removeprefix('mo-')}={n}"
                for g, n in sorted(result.grain_counts.items())
            )
            line = f"  ok   {result.document_id}  [{grain_summary}]"
            if result.orphans_deleted:
                counters["orphans_deleted"] += result.orphans_deleted
                line += f" orphans={result.orphans_deleted}"
            report.print(line)
        elif result.action == "skipped":
            counters["skipped"] += 1
            report.print(
                f"  skip {result.document_id}  "
                f"[children={len(result.child_record_ids)}]"
            )
        elif result.action == "dry-run":
            counters["dry_run"] += 1
            grain_summary = " ".join(
                f"{g.removeprefix('mo-')}={n}"
                for g, n in sorted(result.grain_counts.items())
            )
            report.print(f"  dry  {result.document_id}  [{grain_summary}]")
        elif result.action == "orphans-only":
            counters["orphans_deleted"] += result.orphans_deleted
            report.print(
                f"  orph {result.document_id}  orphans={result.orphans_deleted}"
            )
        else:
            counters["errors"] += 1
            msg = "; ".join(result.errors) or "unknown"
            report.print(f"  ERR  {result.document_id}  ({msg})", err=True)
        report.advance()

    try:
        with _IndexProgressReporter(len(sidecars), counters) as report:
            if workers > 1 and not args.dry_run:
                # Parallel path — each thread opens its own DB
                # connection inside _index_all_parallel; the main
                # thread doesn't hold one. Yields results in
                # completion order, not input order.
                for result in _index_all_parallel(
                    es,  # type: ignore[arg-type]
                    args.db,
                    sidecars,
                    workers=workers,
                    target=args.target,
                    mirror=args.mirror,
                    force=force,
                    dry_run=False,
                    grains=grains_filter,
                    index_generation=args.index_generation,
                ):
                    _handle(result, report=report)
            else:
                # Sequential path — one shared DB connection on the
                # main thread. Used for --dry-run (we never touch ES
                # so threading buys nothing) and for `--workers=1`
                # debugging / deterministic-output runs.
                db = DB(args.db)
                try:
                    for path in sidecars:
                        try:
                            result = _index_one(
                                None if args.dry_run else es,  # type: ignore[arg-type]
                                db,
                                path,
                                target=args.target,
                                mirror=args.mirror,
                                force=force,
                                dry_run=args.dry_run,
                                grains=grains_filter,
                                index_generation=args.index_generation,
                            )
                        except KeyboardInterrupt:
                            raise
                        except Exception as exc:
                            counters["errors"] += 1
                            report.print(f"  ERR   {path.name}  ({exc!r})", err=True)
                            report.advance()
                            continue
                        _handle(result, report=report)
                finally:
                    db.close()
    except KeyboardInterrupt:
        print(
            f"\ninterrupted: indexed={counters['indexed']} "
            f"skipped={counters['skipped']} "
            f"orphans={counters['orphans_deleted']} "
            f"errors={counters['errors']}",
            file=sys.stderr,
        )
        return 130

    # --include-persons: project the curated registry into mo-persons.
    # Runs after the sidecar loop so the operator gets the full picture
    # in one CLI invocation. Idempotent via the sentinel state row, so
    # subsequent runs no-op until persons.json changes.
    if getattr(args, "include_persons", False):
        from monitorul_ii.elasticsearch.indexer import (
            index_persons_with_state as _index_persons,
        )
        from monitorul_ii.registries import load_persons

        persons = load_persons()
        db = DB(args.db)
        try:
            persons_result = _index_persons(
                None if args.dry_run else es,  # type: ignore[arg-type]
                db,
                persons,
                target=args.target,
                force=force,
                dry_run=args.dry_run,
                index_generation=args.index_generation,
            )
        finally:
            db.close()

        if persons_result.action == "indexed":
            print(
                f"  ok   persons-registry  "
                f"[mo-persons={persons_result.grain_counts.get('mo-persons', 0)}]"
                + (
                    f" orphans={persons_result.orphans_deleted}"
                    if persons_result.orphans_deleted
                    else ""
                )
            )
        elif persons_result.action == "skipped":
            print(
                f"  skip persons-registry  "
                f"[entries={len(persons_result.child_record_ids)}]"
            )
        elif persons_result.action == "dry-run":
            print(
                f"  dry  persons-registry  "
                f"[mo-persons={persons_result.grain_counts.get('mo-persons', 0)}]"
            )
        elif persons_result.errors:
            print(
                f"  ERR  persons-registry  ({'; '.join(persons_result.errors)})",
                file=sys.stderr,
            )
            counters["errors"] += 1

    summary = (
        f"indexed={counters['indexed']} "
        f"skipped={counters['skipped']} "
        f"orphans={counters['orphans_deleted']} "
        f"errors={counters['errors']}"
    )
    if counters["dry_run"]:
        summary += f" dry_run={counters['dry_run']}"
    print(summary, flush=True)
    return 1 if counters["errors"] else 0


# Each named query gets a tuple of param names that should be promoted
# from the --params dict to positional arguments at call time. Keep
# this small + explicit — it's the only place CLI ↔ Python signature
# translation happens, and getting it wrong silently swaps a positional
# for a kwarg and surfaces as a misleading TypeError.
_QUERY_POSITIONALS: dict[str, tuple[str, ...]] = {
    "search_speeches": (),
    "list_document_children": ("document_id",),
    "get_document": ("document_id",),
    "list_documents_by_date": ("date",),
    "get_agenda_item": ("record_id",),
    "get_speech": ("record_id",),
    "person_page": ("person_slug",),
    "search_persons": ("q",),
    "list_committee_meetings": ("committee_id",),
    "get_report": ("record_id",),
    "agg_speeches_by_party_year": (),
}


def _render_query_result(result: object) -> str:
    """Pretty-print a query function's return value as JSON.

    Dataclasses (`SearchResult`, `PersonPage`, `SearchHit`) round-trip
    through `dataclasses.asdict`; plain dicts (the lookup-by-id
    helpers) pass through; None becomes the literal `null`.
    """
    import dataclasses

    if result is None:
        return "null"
    if dataclasses.is_dataclass(result):
        payload = dataclasses.asdict(result)
    else:
        payload = result
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def cmd_query(args: argparse.Namespace) -> int:
    from monitorul_ii.elasticsearch import queries as es_queries

    name = args.name
    func = es_queries.NAMED_QUERIES.get(name)
    if func is None:
        print(
            f"query: unknown name {name!r}. "
            f"Choose one of: {', '.join(sorted(es_queries.NAMED_QUERIES))}",
            file=sys.stderr,
        )
        return 2

    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as exc:
        print(f"query: --params is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print("query: --params must decode to a JSON object (dict).", file=sys.stderr)
        return 2

    cfg = ESConfig.from_env()
    if cfg is None:
        print(
            "query: missing ES_URL or ES_API_KEY in environment "
            "(set both via `.env` or shell exports).",
            file=sys.stderr,
        )
        return 2
    es = _build_es_client(cfg)

    if args.explain:
        # Light-weight tracer: wrap es.search to print the body before
        # hitting the cluster. The query layer also calls es.get for
        # lookup-by-id helpers; trace those too for completeness.
        original_search = es.search
        original_get = es.get

        def _traced_search(*a: object, **kw: object) -> object:
            print("# es.search", file=sys.stderr)
            print(
                json.dumps(
                    {"index": kw.get("index"), "body": kw.get("body")},
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                file=sys.stderr,
            )
            return original_search(*a, **kw)

        def _traced_get(*a: object, **kw: object) -> object:
            print(
                f"# es.get index={kw.get('index')} id={kw.get('id')}",
                file=sys.stderr,
            )
            return original_get(*a, **kw)

        es.search = _traced_search  # type: ignore[method-assign]
        es.get = _traced_get  # type: ignore[method-assign]

    positionals = []
    for pname in _QUERY_POSITIONALS[name]:
        if pname not in params:
            print(
                f"query: {name!r} requires {pname!r} in --params "
                f"(positional argument).",
                file=sys.stderr,
            )
            return 2
        positionals.append(params.pop(pname))

    try:
        result = func(es, *positionals, **params)
    except TypeError as exc:
        print(f"query: bad parameters for {name!r}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — surface to operator
        print(f"query: ES error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(_render_query_result(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


def _redact_proxy(url: str) -> str:
    """Mask any password embedded in the proxy URL before logging."""
    from urllib.parse import urlparse, urlunparse

    p = urlparse(url)
    if p.password:
        netloc = f"{p.username}:***@{p.hostname}"
        if p.port:
            netloc += f":{p.port}"
        return urlunparse(p._replace(netloc=netloc))
    return url


if __name__ == "__main__":
    raise SystemExit(main())
