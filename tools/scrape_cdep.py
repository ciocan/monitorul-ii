"""Scrape cdep.ro per-legislature deputy lists.

Hits the public structure pages (`https://www.cdep.ro/pls/parlam/structura.mp?leg=YYYY`)
and parses the HTML table of deputies for each post-1990 legislature.
Emits one JSONL row per deputy entry to `data/seed_cdep.jsonl`:

    {
      "name": "Florin Iordache",
      "role": "deputat",
      "chamber": "Camera Deputaților",
      "legislature": "VIII",
      "from": "2016-12-21",
      "to": "2020-12-21",
      "party": "PSD",
      "source": "cdep.ro",
      "source_url": "https://www.cdep.ro/pls/parlam/structura.mp?...",
      "wikidata_qid": null
    }

# Politeness contract

  - Default 1 request per second between page fetches (`--delay`).
  - Retry on 5xx with exponential backoff (1s → 2s → 4s, max 3 tries).
  - 4xx (other than 429) raises immediately; 429 sleeps `Retry-After` if present.
  - User-Agent identifies us as `monitorul-ii/persons-bootstrap (+contact)`.
  - No hardcoded credentials. cdep.ro pages are anonymous-public; no login required.

# Failure modes

If cdep.ro is unreachable or its HTML structure changes, the scraper logs
the failure to stderr and exits non-zero with an empty (or partial) JSONL
output. The persons.json bootstrap pipeline tolerates partial seed data —
the manual long-tail pass + Wikidata seed cover gaps. Re-run when the
site recovers.

This scraper is intentionally stand-alone: it does not depend on any of
the `monitorul_ii.*` package modules so it can be vendored or reused
elsewhere (test harness, downstream tooling).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin

import httpx

USER_AGENT = "monitorul-ii/persons-bootstrap (https://github.com/ciocan/monitorul-ii)"
BASE = "https://www.cdep.ro"


# Post-1990 legislature mandates and their start dates (best-effort canonical).
# cdep.ro uses the year the legislature started as its `idl` query param.
LEGISLATURES: tuple[tuple[str, int, str | None, str | None], ...] = (
    # (roman_label, idl_year, mandate_from, mandate_to)
    ("I", 1990, "1990-06-18", "1992-10-21"),
    ("II", 1992, "1992-10-21", "1996-11-22"),
    ("III", 1996, "1996-11-22", "2000-12-11"),
    ("IV", 2000, "2000-12-11", "2004-12-13"),
    ("V", 2004, "2004-12-13", "2008-12-19"),
    ("VI", 2008, "2008-12-19", "2012-12-19"),
    ("VII", 2012, "2012-12-19", "2016-12-21"),
    ("VIII", 2016, "2016-12-21", "2020-12-21"),
    ("IX", 2020, "2020-12-21", "2024-12-21"),
    ("X", 2024, "2024-12-21", None),
)


@dataclass(frozen=True)
class CdepDeputy:
    name: str
    party: str | None
    legislature: str
    legislature_idl: int
    mandate_from: str | None
    mandate_to: str | None
    source_url: str

    def to_jsonl_row(self) -> dict[str, object]:
        return {
            "name": self.name,
            "role": "deputat",
            "chamber": "Camera Deputaților",
            "legislature": self.legislature,
            "from": self.mandate_from,
            "to": self.mandate_to,
            "party": self.party,
            "source": "cdep.ro",
            "source_url": self.source_url,
            "wikidata_qid": None,
        }


def _client(*, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )


def _get_with_retry(
    client: httpx.Client, url: str, *, attempts: int = 3
) -> httpx.Response:
    last_exc: Exception | None = None
    backoff = 1.0
    for attempt in range(1, attempts + 1):
        try:
            r = client.get(url)
            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", "5"))
                time.sleep(retry_after)
                continue
            if 500 <= r.status_code < 600:
                raise httpx.HTTPStatusError(
                    f"{r.status_code} on {url}", request=r.request, response=r
                )
            r.raise_for_status()
            return r
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(backoff)
                backoff *= 2
    assert last_exc is not None
    raise last_exc


# Two HTML extraction shapes seen on cdep.ro across legislatures:
#
#   (a) Modern (~legislatures VII+): `structura.mp?leg=YYYY` returns a
#       table whose rows look like
#         <tr><td><a href="...">SURNAME Firstname</a></td><td>PARTY</td>...</tr>
#       split across `<tr>` rows.
#
#   (b) Older (I-VI): same path returns a flatter list with `<a>` anchors
#       only and party in a sibling cell. Same regex pattern works.
#
# We grep with two cooperating regexes: one for the deputy anchor URL
# (`href="structura.mp?idm=...&leg=YYYY"`) and one for the inline party
# label (`>...<` short uppercase token in the next cell).
_DEPUTY_ROW_RE = re.compile(
    r'<a\s+href="(?P<href>structura\.mp\?[^"]*idm=\d+[^"]*)"[^>]*>\s*(?P<name>[^<]{2,80}?)\s*</a>',
    re.IGNORECASE,
)
_PARTY_AFTER_NAME_RE = re.compile(
    r"</a>\s*</td>\s*<td[^>]*>\s*(?:<[^>]+>\s*)?(?P<party>[A-ZĂÎȘȚÂ][A-ZĂÎȘȚÂ0-9./\- ]{1,30}?)\s*(?:<|</td)",
    re.IGNORECASE,
)


def _scrape_legislature(
    client: httpx.Client,
    *,
    label: str,
    idl: int,
    mandate_from: str | None,
    mandate_to: str | None,
    delay: float,
) -> Iterator[CdepDeputy]:
    """Fetch + parse one legislature listing page.

    Yields CdepDeputy rows. Yields nothing on parse failure (scraper
    logs to stderr and continues to the next legislature).
    """
    url = f"{BASE}/pls/parlam/structura.mp?leg={idl}"
    try:
        r = _get_with_retry(client, url)
    except Exception as exc:
        print(f"  cdep: legislature {label} ({url}) failed: {exc}", file=sys.stderr)
        return

    html = r.text
    rows = list(_DEPUTY_ROW_RE.finditer(html))
    if not rows:
        print(
            f"  cdep: legislature {label}: no rows matched (HTML structure may have changed)",
            file=sys.stderr,
        )
        return

    print(
        f"  cdep: legislature {label}: {len(rows)} deputy anchors found",
        file=sys.stderr,
    )
    seen_names: set[str] = set()
    for m in rows:
        href = m.group("href")
        name_raw = m.group("name").strip()
        # cdep displays SURNAME Firstname; we keep that order — the matcher
        # has token-set tier so order doesn't matter for resolution.
        name = re.sub(r"\s+", " ", name_raw)
        if name in seen_names or len(name) < 4:
            continue
        seen_names.add(name)

        # Look for a party token in the ~200 chars after the </a>.
        tail = html[m.end() : m.end() + 200]
        party_m = _PARTY_AFTER_NAME_RE.search(tail)
        party = party_m.group("party").strip() if party_m else None

        yield CdepDeputy(
            name=name,
            party=party,
            legislature=label,
            legislature_idl=idl,
            mandate_from=mandate_from,
            mandate_to=mandate_to,
            source_url=urljoin(BASE + "/pls/parlam/", href),
        )

    if delay > 0:
        time.sleep(delay)


def scrape_all(
    *,
    legislatures: tuple[tuple[str, int, str | None, str | None], ...] = LEGISLATURES,
    delay: float = 1.0,
) -> Iterator[CdepDeputy]:
    with _client() as client:
        for label, idl, mfrom, mto in legislatures:
            yield from _scrape_legislature(
                client,
                label=label,
                idl=idl,
                mandate_from=mfrom,
                mandate_to=mto,
                delay=delay,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scrape_cdep",
        description="Scrape cdep.ro per-legislature deputy lists for the persons.json bootstrap.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/seed_cdep.jsonl"),
        help="Output JSONL path (default: data/seed_cdep.jsonl).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between page fetches (default: 1.0).",
    )
    parser.add_argument(
        "--legislatures",
        type=str,
        default=None,
        help="Comma-separated subset of legislature labels (e.g. 'IX,X'). Default: all.",
    )
    args = parser.parse_args(argv)

    chosen = LEGISLATURES
    if args.legislatures:
        wanted = {x.strip() for x in args.legislatures.split(",") if x.strip()}
        chosen = tuple(t for t in LEGISLATURES if t[0] in wanted)
        if not chosen:
            print(f"no legislatures match {args.legislatures!r}", file=sys.stderr)
            return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    started = datetime.now().isoformat(timespec="seconds")
    print(f"cdep scrape started {started}", file=sys.stderr)
    with args.out.open("w", encoding="utf-8") as f:
        for dep in scrape_all(legislatures=chosen, delay=args.delay):
            f.write(json.dumps(dep.to_jsonl_row(), ensure_ascii=False) + "\n")
            n += 1
    print(f"wrote {n} rows -> {args.out}", file=sys.stderr)
    return 0 if n > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
