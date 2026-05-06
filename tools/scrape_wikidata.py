"""DEPRECATED — superseded by `tools/enrich_persons_wikidata.py`.

The original implementation queried Wikidata's SPARQL endpoint by P39
(position held) with QIDs intended to identify "Member of the Romanian
Chamber of Deputies" / "Member of the Romanian Senate" / etc. The QIDs
the v0.1 implementation used (`Q1130721`, `Q1110923`) returned 0
bindings — they were not the correct position-held QIDs for the
Romanian parliamentary roles. The fallback hand-curation in
`persons.json` produced ~31 hallucinated QIDs that pointed at unrelated
entities (Canadian hockey players, French actors, mountains, films,
disambiguation pages). Every one was wrong.

The replacement (`tools/enrich_persons_wikidata.py`) takes the inverse
approach: per-entry name lookup against Wikidata, restricted to
Romanian nationals (P27 = Q218), with description-keyword + birth-date
scoring to disambiguate homonyms. It hits the public SPARQL endpoint
once per registry entry (~50 queries for the seed registry, well below
any sensible rate limit), produces a suggestion JSONL for review, and
optionally writes verified QIDs back into `persons.json` via `--apply`.

Use it instead:

    uv run python -m tools.enrich_persons_wikidata
    cat data/persons_wikidata_suggestions.jsonl | less
    uv run python -m tools.enrich_persons_wikidata --apply

This file remains as a deprecation marker so existing references in
docs / code don't 404; running it prints the pointer and exits.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    print(
        "scrape_wikidata.py is deprecated; use tools/enrich_persons_wikidata.py "
        "(per-entry name lookup against the SPARQL endpoint, with description-"
        "keyword + birth-date scoring for homonym disambiguation).\n"
        "\n"
        "  uv run python -m tools.enrich_persons_wikidata\n"
        "  cat data/persons_wikidata_suggestions.jsonl | less\n"
        "  uv run python -m tools.enrich_persons_wikidata --apply",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
