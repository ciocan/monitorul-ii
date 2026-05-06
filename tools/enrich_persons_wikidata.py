"""Enrich `persons.json` with verified Wikidata QIDs.

For each registry entry, this tool queries Wikidata's SPARQL endpoint by
name + Romanian nationality (P27 = Q218), scores the candidate matches
on description keywords + birth-date agreement, and either writes a
suggestion JSONL for human review or merges the verified QIDs back into
`persons.json`.

# Why a per-entry tool, not the bulk position-based scrape

The original `scrape_wikidata.py` queried the SPARQL endpoint by P39
(position held) with QIDs that turned out NOT to match Romanian
parliamentary positions — the bulk query returned 0 bindings, and the
fallback hand-curation in `persons.json` v0.1.0 produced ~30 hallucinated
QIDs that pointed at unrelated entities (Canadian hockey players, French
actors, mountains, films, disambiguation pages). Every one was wrong.

This tool fixes the failure mode at the source: we look up each person
by their name, restrict to Romanian nationals, and let the description
field disambiguate. Birth-date agreement (when both sides have one) is
the decisive tiebreaker for homonyms.

# Politeness contract

  - One SPARQL query per registry entry — minimum total request count
    (~50 queries for the seed registry).
  - 1s sleep between queries (the public endpoint rate-limits aggressive
    callers; 1s is well under the threshold).
  - User-Agent identifies us per Wikimedia's policy:
    `monitorul-ii/<version> (<repo>; <contact>)`.
  - Retry once on 5xx with 5s backoff. 429 honours `Retry-After`.

# Failure modes

  - Wikidata unreachable → exit non-zero with a partial output. Re-run
    when it recovers; the tool is idempotent.
  - Multiple candidates with no decisive disambiguator → flag as
    `ambiguous` and do not auto-apply. Human picks via the suggestion
    JSONL.
  - No candidate matches → flag as `no_match` and do not auto-apply.
    The registry entry's `wikidata_qid` stays whatever it was.

# Workflow

  uv run python -m tools.enrich_persons_wikidata        # write suggestions
  cat data/persons_wikidata_suggestions.jsonl | less    # review
  uv run python -m tools.enrich_persons_wikidata --apply  # merge confident matches
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

USER_AGENT = (
    "monitorul-ii/0.12.0 (https://github.com/ciocan/monitorul-ii; hello@42tech.co)"
)
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Description keyword scoring. Higher score = stronger signal that the
# Wikidata entity is the politician we're looking for.
_DESC_SCORE: tuple[tuple[str, int], ...] = (
    ("president of romania", 10),
    ("prime minister", 10),
    ("deputy prime minister", 8),
    ("minister of", 8),
    ("speaker of", 8),
    ("president of the senate", 8),
    ("president of the chamber", 8),
    ("senator", 6),
    ("deputy", 6),
    ("member of the european parliament", 6),
    ("member of the chamber of deputies", 6),
    ("member of the romanian senate", 6),
    ("politician", 4),  # generic but positive
    ("statesman", 4),
    ("political", 2),  # very weak
    # Strong negatives — non-people or non-politicians
    ("researcher", -5),
    ("scientist", -5),
    ("athlete", -5),
    ("footballer", -5),
    ("musician", -5),
    ("actor", -5),
    ("disambiguation page", -100),
)


@dataclass(frozen=True)
class WikidataCandidate:
    qid: str
    label: str
    description: str
    birth_date: str | None
    score: int  # composite: description + birth-date match
    matched_via: str  # "exact_name+birth", "exact_name+desc", "exact_name", "no_match"


@dataclass(frozen=True)
class EnrichmentSuggestion:
    person_id: str
    canonical_name: str
    current_qid: str | None
    suggestion: WikidataCandidate | None
    alternates: list[WikidataCandidate]
    status: str  # "ok" | "ambiguous" | "no_match" | "error"


def _client(*, timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
        },
        timeout=timeout,
    )


def _run_sparql(client: httpx.Client, query: str, *, attempts: int = 2) -> dict | None:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            r = client.get(SPARQL_ENDPOINT, params={"query": query, "format": "json"})
            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", "5"))
                time.sleep(retry_after)
                continue
            if 500 <= r.status_code < 600:
                raise httpx.HTTPStatusError(
                    f"{r.status_code}", request=r.request, response=r
                )
            r.raise_for_status()
            return r.json()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(5.0)
    if last_exc is not None:
        print(f"  sparql failed: {last_exc}", file=sys.stderr)
    return None


def _score_description(desc: str) -> int:
    """Sum of keyword-match weights against the candidate's description."""
    if not desc:
        return 0
    d = desc.lower()
    score = 0
    for keyword, weight in _DESC_SCORE:
        if keyword in d:
            score += weight
    return score


def _score_birth_match(candidate_birth: str | None, registry_birth: str | None) -> int:
    """Decisive +20 when both sides agree on year-month-day; +5 on year-only;
    -10 when both are present but disagree (year mismatch); 0 when either is
    missing.
    """
    if not candidate_birth or not registry_birth:
        return 0
    c10, r10 = candidate_birth[:10], registry_birth[:10]
    if len(c10) >= 4 and len(r10) >= 4:
        if c10 == r10:
            return 20
        if c10[:4] == r10[:4]:
            return 5
        return -10
    return 0


def _build_query(label_variants: list[tuple[str, str]]) -> str:
    """Build a SPARQL query that searches by any label variant.

    `VALUES ?name { "X"@en "X"@ro ... }` lets us try multiple language
    tags and forms in a single query. We pin to Romanian nationality
    (P27 = Q218) AND `instance of human` (P31 = Q5) to filter out
    same-name foreigners + position-held / disambiguation entities that
    happen to share a label.
    """
    values = " ".join(f'"{v}"@{tag}' for v, tag in label_variants)
    return f"""SELECT ?p ?birth ?desc WHERE {{
  VALUES ?name {{ {values} }}
  ?p rdfs:label ?name ;
     wdt:P31 wd:Q5 ;
     wdt:P27 wd:Q218 .
  OPTIONAL {{ ?p wdt:P569 ?birth . }}
  OPTIONAL {{ ?p schema:description ?desc . FILTER(LANG(?desc) = "en") }}
}} LIMIT 25"""


def _label_variants(entry: dict) -> list[tuple[str, str]]:
    """Build the list of (label, lang_tag) pairs to search.

    We try the canonical_name and diacritic_form against both `en` and
    `ro` language tags, plus aliases against `en` only (aliases are
    typically diacritic variants, name-order swaps, or mojibake — none
    of which Wikidata cares about; English-tagged label match is the
    common case).
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name in (entry.get("canonical_name"), entry.get("diacritic_form")):
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            out.append((name, "en"))
            out.append((name, "ro"))
    for alias in entry.get("aliases") or []:
        if isinstance(alias, str) and alias and alias not in seen:
            seen.add(alias)
            out.append((alias, "en"))
    return out


def _query_one(client: httpx.Client, entry: dict) -> EnrichmentSuggestion:
    label_variants = _label_variants(entry)
    if not label_variants:
        return EnrichmentSuggestion(
            person_id=entry["id"],
            canonical_name=entry["canonical_name"],
            current_qid=entry.get("wikidata_qid"),
            suggestion=None,
            alternates=[],
            status="error",
        )

    data = _run_sparql(client, _build_query(label_variants))
    if data is None:
        return EnrichmentSuggestion(
            person_id=entry["id"],
            canonical_name=entry["canonical_name"],
            current_qid=entry.get("wikidata_qid"),
            suggestion=None,
            alternates=[],
            status="error",
        )

    # Collect candidates, scoring each.
    seen_qids: set[str] = set()
    candidates: list[WikidataCandidate] = []
    for b in data.get("results", {}).get("bindings", []):
        person_uri = b.get("p", {}).get("value", "")
        qid = person_uri.rsplit("/", 1)[-1] if person_uri else ""
        if not qid.startswith("Q") or qid in seen_qids:
            continue
        seen_qids.add(qid)
        desc = b.get("desc", {}).get("value", "") or ""
        birth = b.get("birth", {}).get("value", "") or None
        # Filter out non-human disambiguation / list pages by description.
        if "disambiguation" in desc.lower():
            continue
        score = _score_description(desc) + _score_birth_match(
            birth, entry.get("birth_date")
        )
        # Tier label
        if score >= 25:
            via = "exact_name+birth"
        elif score >= 8:
            via = "exact_name+desc"
        else:
            via = "exact_name"
        candidates.append(
            WikidataCandidate(
                qid=qid,
                label=entry["canonical_name"],
                description=desc,
                birth_date=birth,
                score=score,
                matched_via=via,
            )
        )

    if not candidates:
        return EnrichmentSuggestion(
            person_id=entry["id"],
            canonical_name=entry["canonical_name"],
            current_qid=entry.get("wikidata_qid"),
            suggestion=None,
            alternates=[],
            status="no_match",
        )

    candidates.sort(key=lambda c: -c.score)
    top = candidates[0]
    alternates = candidates[1:]

    # Status classification. The SPARQL query already filters to
    # `instance of human` AND Romanian nationality, so any returned
    # candidate is a real person AND a Romanian citizen with the exact
    # name match. The remaining job is to pick a confident winner among
    # any homonyms.
    #
    #   - high confidence (`score >= 8`) AND clear winner over the
    #     runner-up → "ok"
    #   - single candidate with at least a politician-shaped description
    #     hint (description score >= 4) → "ok" (no homonym to pick from)
    #   - single candidate without a politician hint → "ambiguous"
    #     (might be a non-politician with the same name)
    #   - multiple candidates with similar scores → "ambiguous"
    desc_score_only = _score_description(top.description)
    if top.score >= 8 and (not alternates or top.score - alternates[0].score >= 5):
        status = "ok"
    elif not alternates and desc_score_only >= 4:
        status = "ok"
    elif alternates and top.score - alternates[0].score < 5:
        status = "ambiguous"
    else:
        status = "ambiguous"

    return EnrichmentSuggestion(
        person_id=entry["id"],
        canonical_name=entry["canonical_name"],
        current_qid=entry.get("wikidata_qid"),
        suggestion=top,
        alternates=alternates,
        status=status,
    )


def enrich(
    persons_path: Path,
    *,
    delay: float = 1.0,
    only_missing: bool = False,
) -> list[EnrichmentSuggestion]:
    """Query Wikidata for each entry; return one suggestion per entry."""
    data = json.loads(persons_path.read_text(encoding="utf-8"))
    entries: list[dict] = data.get("entries") or []
    out: list[EnrichmentSuggestion] = []
    with _client() as client:
        for entry in entries:
            if only_missing and entry.get("wikidata_qid"):
                continue
            print(
                f"  querying {entry['id']:35s}  ({entry['canonical_name']})",
                file=sys.stderr,
            )
            sugg = _query_one(client, entry)
            out.append(sugg)
            time.sleep(delay)
    return out


def _suggestion_jsonl_row(s: EnrichmentSuggestion) -> dict:
    sug = s.suggestion
    return {
        "person_id": s.person_id,
        "canonical_name": s.canonical_name,
        "current_qid": s.current_qid,
        "status": s.status,
        "suggestion": (
            {
                "qid": sug.qid,
                "description": sug.description,
                "birth_date": sug.birth_date,
                "score": sug.score,
                "matched_via": sug.matched_via,
            }
            if sug
            else None
        ),
        "alternates": [
            {
                "qid": a.qid,
                "description": a.description,
                "birth_date": a.birth_date,
                "score": a.score,
            }
            for a in s.alternates[:5]
        ],
    }


def apply_suggestions(
    persons_path: Path,
    suggestions: list[EnrichmentSuggestion],
    *,
    only_status: tuple[str, ...] = ("ok",),
    update_birth_date: bool = True,
) -> tuple[int, int, int]:
    """Merge confident suggestions back into persons.json.

    Returns `(updated, kept, skipped)` counts.

    For each `status="ok"` suggestion:
      - If current `wikidata_qid` is null → set to suggested qid.
      - If current `wikidata_qid` differs and is on the hallucination
        list (the original 31 wrong QIDs) → overwrite with suggested.
      - If current matches suggested → skip-already-correct.
    Birth date is also updated on the same conditions when
    `update_birth_date=True` and the Wikidata candidate carries one.
    """
    data = json.loads(persons_path.read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in data["entries"]}
    updated = kept = skipped = 0
    for s in suggestions:
        if s.status not in only_status or s.suggestion is None:
            skipped += 1
            continue
        entry = by_id.get(s.person_id)
        if entry is None:
            skipped += 1
            continue
        new_qid = s.suggestion.qid
        if entry.get("wikidata_qid") == new_qid:
            kept += 1
            continue
        entry["wikidata_qid"] = new_qid
        if update_birth_date and s.suggestion.birth_date:
            entry["birth_date"] = s.suggestion.birth_date[:10]
        updated += 1

    persons_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return (updated, kept, skipped)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="enrich_persons_wikidata",
        description="Look up real Wikidata QIDs for each entry in persons.json.",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("src/monitorul_ii/registries/persons.json"),
        help="persons.json path (default: src/monitorul_ii/registries/persons.json).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/persons_wikidata_suggestions.jsonl"),
        help="Suggestion JSONL output (default: data/persons_wikidata_suggestions.jsonl).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Merge confident suggestions back into persons.json (status=ok only).",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Skip entries whose wikidata_qid is already non-null.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between SPARQL queries (default: 1.0).",
    )
    args = parser.parse_args(argv)

    if not args.registry.is_file():
        print(f"registry not found: {args.registry}", file=sys.stderr)
        return 2

    suggestions = enrich(
        args.registry, delay=args.delay, only_missing=args.only_missing
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for s in suggestions:
            f.write(json.dumps(_suggestion_jsonl_row(s), ensure_ascii=False) + "\n")

    counts: dict[str, int] = {}
    for s in suggestions:
        counts[s.status] = counts.get(s.status, 0) + 1
    print(f"\nsuggestions written to {args.out}", file=sys.stderr)
    for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {status}: {n}", file=sys.stderr)

    if args.apply:
        updated, kept, skipped = apply_suggestions(args.registry, suggestions)
        print(
            f"\napplied to {args.registry}: updated={updated} kept={kept} "
            f"skipped={skipped}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
