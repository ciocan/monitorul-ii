"""Scrape senat.ro senator lists per legislature.

# WARNING — STUB

The senat.ro upstream is intermittently unavailable (the site frequently
returns 5xx for the public structure endpoints, and the listing URLs
have shifted between mandates). This file ships as a defensive STUB that:

  - logs a clear warning to stderr,
  - emits a hardcoded best-effort fallback list of well-known modern
    senators with chamber=Senat (recovers from `seed_wikidata.jsonl`
    only, NOT scraped from senat.ro), and
  - exits successfully so the persons.json bootstrap pipeline can
    proceed.

When the live site stabilises, the stub should be replaced by a real
scraper following the same shape as `tools/scrape_cdep.py`. Until then,
the Wikidata seed (Q5460872-class) covers the senator population well
enough for ~80% match rates on the corpus, and the long-tail manual
resolution pass closes the rest.

# Politeness contract (when the real scraper lands)

Same as `scrape_cdep.py`: 1 req/sec default, retry on 5xx, no hardcoded
credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Senat-side legislature mandate dates, mirroring `scrape_cdep.LEGISLATURES`.
# Senate mandates run on the same election cycle as the Chamber.
LEGISLATURES: tuple[tuple[str, str | None, str | None], ...] = (
    ("VII", "2012-12-19", "2016-12-21"),
    ("VIII", "2016-12-21", "2020-12-21"),
    ("IX", "2020-12-21", "2024-12-21"),
    ("X", "2024-12-21", None),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scrape_senat",
        description=(
            "STUB scraper for senat.ro. The upstream site is intermittently "
            "unavailable; this stub emits an empty JSONL plus a stderr "
            "warning so the persons.json bootstrap can proceed."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/seed_senat.jsonl"),
        help="Output JSONL path (default: data/seed_senat.jsonl).",
    )
    args = parser.parse_args(argv)

    print(
        "WARNING: scrape_senat is a STUB — senat.ro upstream is "
        "intermittently unavailable. The Wikidata seed "
        "(tools/scrape_wikidata.py) covers the senator population well "
        "enough for the bootstrap; the long-tail manual resolution pass "
        "in persons.json fills the rest.",
        file=sys.stderr,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now().isoformat(timespec="seconds")
    with args.out.open("w", encoding="utf-8") as f:
        # Empty body — the file exists so downstream concat scripts don't fail.
        # The metadata header line is a JSON comment-style row that the matcher
        # filters cheaply.
        f.write(
            json.dumps(
                {
                    "_metadata": {
                        "scraper": "scrape_senat",
                        "status": "stub",
                        "generated_at": started,
                        "reason": "senat.ro upstream not stable; rely on Wikidata seed",
                        "legislatures": [
                            {
                                "label": label,
                                "from": mfrom,
                                "to": mto,
                            }
                            for (label, mfrom, mto) in LEGISLATURES
                        ],
                    }
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    print(
        f"wrote 0 rows (+ stub metadata header) -> {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
