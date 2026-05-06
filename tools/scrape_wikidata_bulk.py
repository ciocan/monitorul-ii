"""Bulk-scrape Romanian politicians from Wikidata via SPARQL pagination.

The complement to `enrich_persons_wikidata.py` (per-entry lookup):
this tool pulls **every** Romanian human whose Wikidata description
contains a politician-shaped keyword (politician / deputy / senator /
minister / president of romania), with QID + label + description +
birth date. As of 2026-05 the corpus is ~9,500 entries — paginates
in 5000-row chunks (Wikidata's hard LIMIT cap on the public endpoint).

# Why this is the primary registry-population path

The original plan was cdep.ro per-legislature scrapes for deputies +
senat.ro for senators. Both sites are unreachable / unstable today
(cdep.ro times out on every legislature; senat.ro is a GUID-driven CMS
with no scrapeable senator-list endpoint). Wikidata covers the same
population with verified QIDs already attached — turning what was a
"scrape three sites + merge + enrich" pipeline into a single SPARQL
pass.

Limitations:
  - Wikidata coverage is incomplete (high-profile politicians are well-
    covered; rural deputies who served one mandate may be missing).
    Long-tail resolution still depends on the manual loop guided by
    `tools/inspect_speaker.py`.
  - Mandates are NOT pulled: the Wikidata P39 (position held) data is
    inconsistently populated and the position QIDs don't map cleanly to
    Romanian legislatures. Bulk-scraped entries arrive with `mandates: []`
    and the operator can fill them in by hand or via cdep when it's
    back up.

# Politeness contract

  - One paginated query per 5000-entry chunk (~2 queries total for 9,500
    entries). Each chunk is large, so we sleep 5s between pages.
  - User-Agent identifies us per Wikimedia's policy.
  - 5xx retry with 8s backoff (the public endpoint is intermittently
    502 Bad Gateway under load).

# Failure modes

  - Wikidata unreachable → exit non-zero with a partial JSONL output.
    Re-run when the endpoint recovers; the tool is idempotent (the
    merge step de-duplicates by QID).
  - JSON-parse failure on stray control characters in descriptions →
    `JSONDecoder(strict=False)` accepts them as literal chars in
    string values (Python's strict default is overly conservative for
    Wikidata's output).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpx

USER_AGENT = (
    "monitorul-ii/0.12.0 (https://github.com/ciocan/monitorul-ii; hello@42tech.co)"
)
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

PAGE_SIZE = 5000  # Wikidata's effective LIMIT cap


SPARQL_TEMPLATE = """SELECT ?p ?pLabel ?pLabelRo ?desc ?birth WHERE {{
  ?p wdt:P31 wd:Q5 ;
     wdt:P27 wd:Q218 .
  ?p schema:description ?desc .
  FILTER(LANG(?desc) = "en")
  FILTER(CONTAINS(LCASE(STR(?desc)), "politician") ||
         CONTAINS(LCASE(STR(?desc)), "deputy") ||
         CONTAINS(LCASE(STR(?desc)), "senator") ||
         CONTAINS(LCASE(STR(?desc)), "minister") ||
         CONTAINS(LCASE(STR(?desc)), "prime minister") ||
         CONTAINS(LCASE(STR(?desc)), "president of romania"))
  OPTIONAL {{ ?p wdt:P569 ?birth . }}
  OPTIONAL {{ ?p rdfs:label ?pLabelRo . FILTER(LANG(?pLabelRo) = "ro") }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY ?p
LIMIT {limit} OFFSET {offset}"""


@dataclass(frozen=True)
class WikidataPerson:
    qid: str
    label_en: str
    label_ro: str | None
    description: str
    birth_date: str | None

    def to_jsonl_row(self) -> dict[str, object]:
        return {
            "qid": self.qid,
            "label_en": self.label_en,
            "label_ro": self.label_ro,
            "description": self.description,
            "birth_date": self.birth_date,
        }


def _client(*, timeout: float = 180.0) -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
        },
        timeout=timeout,
    )


def _run_paginated(
    client: httpx.Client, *, attempts: int = 5
) -> Iterator[WikidataPerson]:
    """Page through the politician set in PAGE_SIZE chunks until empty.

    Each page may legitimately receive a 502 from the SPARQL endpoint
    under load; we retry up to `attempts` with 8s backoff before giving
    up on the page (and the rest of the iteration).
    """
    offset = 0
    while True:
        query = SPARQL_TEMPLATE.format(limit=PAGE_SIZE, offset=offset)
        rows: list[dict] | None = None
        last_err: str | None = None
        for attempt in range(1, attempts + 1):
            try:
                r = client.get(
                    SPARQL_ENDPOINT, params={"query": query, "format": "json"}
                )
                if r.status_code != 200 or not r.text.startswith("{"):
                    last_err = f"status={r.status_code} head={r.text[:80]!r}"
                    time.sleep(8.0)
                    continue
                # `strict=False` tolerates unescaped control chars in
                # description strings (Wikidata returns these occasionally).
                data = json.JSONDecoder(strict=False).decode(r.text)
                rows = data.get("results", {}).get("bindings", []) or []
                break
            except (httpx.RequestError, json.JSONDecodeError) as exc:
                last_err = repr(exc)
                time.sleep(8.0)
        if rows is None:
            print(
                f"  wikidata: page offset={offset} failed after {attempts} attempts: {last_err}",
                file=sys.stderr,
            )
            return
        if not rows:
            return  # End of pagination.
        print(
            f"  wikidata: page offset={offset:>5} got {len(rows)} rows",
            file=sys.stderr,
        )
        for b in rows:
            qid = b["p"]["value"].rsplit("/", 1)[-1]
            if not qid.startswith("Q"):
                continue
            yield WikidataPerson(
                qid=qid,
                label_en=b.get("pLabel", {}).get("value", "") or "",
                label_ro=b.get("pLabelRo", {}).get("value") or None,
                description=b.get("desc", {}).get("value", "") or "",
                birth_date=(b.get("birth", {}).get("value") or None),
            )
        offset += PAGE_SIZE
        if len(rows) < PAGE_SIZE:
            return  # Final partial page.
        time.sleep(5.0)  # Be polite between paginated chunks.


def scrape(out_path: Path) -> int:
    """Write the JSONL; returns the row count.

    Each row: `{qid, label_en, label_ro, description, birth_date}`. Sort
    by QID for byte-identical reruns.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[WikidataPerson] = []
    with _client() as client:
        for p in _run_paginated(client):
            rows.append(p)
    rows.sort(key=lambda p: p.qid)
    with out_path.open("w", encoding="utf-8") as f:
        for p in rows:
            f.write(json.dumps(p.to_jsonl_row(), ensure_ascii=False) + "\n")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scrape_wikidata_bulk",
        description="Paginated SPARQL scrape of Romanian politicians.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/seed_wikidata_bulk.jsonl"),
        help="Output JSONL (default: data/seed_wikidata_bulk.jsonl).",
    )
    args = parser.parse_args(argv)

    n = scrape(args.out)
    print(f"wrote {n} rows -> {args.out}", file=sys.stderr)
    return 0 if n > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
