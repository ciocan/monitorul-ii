"""Coverage statistics across the `mo-*` Elasticsearch indices.

Bird's-eye view of how much of the corpus is touched by each LLM-driven
enrichment producer:

* **Discourse** (`monitorul-ii analyze` → `enrichments.discourse.*` on
  `mo-speeches`). Four-cell rubric — Hawkins populism / voice
  attribution / DQI deliberative quality / V-Party anti-pluralism.
  Reports total / substantive / coded counts plus per-framework marker
  rates and breakdowns by year and chamber.
* **Embedding** (`monitorul-ii embed` → `enrichments.embedding` as a
  1024-dim dense_vector). Reports per-grain `total` / `embedded` /
  share. dense_vector is sparse-tolerant (missing field = "not yet
  embedded") so an `exists` query is the canonical coverage probe.

Computation is fan-out of independent `_count` calls plus a couple of
small bucketed aggregations. Cheap to run against a live cluster (every
probe is O(1) on doc count) and idempotent — no state, no writes. The
module is consumed by the `monitorul-ii coverage` CLI handler; nothing
else depends on it.

Design rules:

* **Read aliases only.** Probes hit `mo-speeches` / `mo-agenda-items` /
  ... — never the dated indices directly. Blue-green cutovers are
  transparent here.
* **Exists-query semantics.** The denormaliser writes
  `enrichments.discourse_producer` as the keystone keyword whenever any
  framework payload lands on a doc, so an `exists` probe over that
  field is the unambiguous "this doc has been coded" signal. Per
  framework, the `framework_version` keyword carries the same contract.
* **Format isolation.** `compute_coverage` returns a frozen dataclass
  graph. `format_text` / `format_json` are pure projections — no I/O,
  no mutation. Tests can build a synthetic report and exercise the
  formatters without an ES client in scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from elasticsearch import Elasticsearch


# Grains that carry embeddable text (per the embedding producer; Q3 / Q8
# of `docs/elasticsearch-indexing.md`). `mo-documents` / `mo-votes` /
# `mo-persons` have no embeddable text payload and are intentionally
# absent — their inclusion would just dilute the % with always-zero rows.
EMBEDDABLE_GRAINS: tuple[str, ...] = (
    "mo-speeches",
    "mo-agenda-items",
    "mo-interpellations",
    "mo-questions",
    "mo-committee-meetings",
    "mo-reports",
)

# Discourse framework keys (the four rubrics produced by
# `monitorul-ii analyze`). Order is presentation order in the report.
DISCOURSE_FRAMEWORKS: tuple[str, ...] = ("hawkins", "voice", "dqi", "vparty")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrameworkCoverage:
    """Coverage row for one of the four discourse rubrics."""

    framework: str
    coded: int
    """Docs with `framework_version` populated — the unambiguous
    'this framework ran on this speech' signal."""

    with_markers: int | None
    """Docs where the framework emitted at least one marker. `None`
    for DQI (which emits per-axis levels, not a markers array — count
    is not meaningful in the same way).
    """


@dataclass(frozen=True, slots=True)
class DiscourseCoverage:
    """Discourse-LLM coverage over `mo-speeches`."""

    total_speeches: int
    substantive: int
    coded_any: int
    frameworks: tuple[FrameworkCoverage, ...]
    by_year: tuple[tuple[int, int], ...] = ()
    """(year, coded_count) tuples, sorted ascending by year."""

    by_chamber: tuple[tuple[str, int], ...] = ()
    """(chamber, coded_count) tuples, sorted ascending by chamber."""


@dataclass(frozen=True, slots=True)
class GrainEmbedding:
    """Embedding coverage row for one embeddable grain."""

    grain: str
    total: int
    embedded: int

    @property
    def pct(self) -> float:
        if self.total == 0:
            return 0.0
        return self.embedded / self.total


@dataclass(frozen=True, slots=True)
class EmbeddingCoverage:
    """Embedding coverage across every embeddable grain."""

    grains: tuple[GrainEmbedding, ...]


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Top-level coverage report — what `compute_coverage` returns."""

    generated_at: str
    """ISO-8601 UTC timestamp; recorded so a JSON dump is reproducible."""

    discourse: DiscourseCoverage
    embedding: EmbeddingCoverage
    notes: tuple[str, ...] = field(default_factory=tuple)
    """Operator-facing footnotes describing semantics (e.g. 'voice
    runs only when Hawkins emits ≥1 marker'). Empty for tests that
    don't care."""


# ---------------------------------------------------------------------------
# ES probes
# ---------------------------------------------------------------------------


def _count(
    es: Elasticsearch, *, index: str, query: dict[str, Any] | None = None
) -> int:
    body = {"query": query} if query is not None else {}
    return int(es.count(index=index, **body)["count"])


def _terms_agg(
    es: Elasticsearch,
    *,
    index: str,
    field: str,
    query: dict[str, Any] | None = None,
    size: int = 50,
) -> list[tuple[Any, int]]:
    body: dict[str, Any] = {
        "size": 0,
        "aggs": {"by": {"terms": {"field": field, "size": size}}},
    }
    if query is not None:
        body["query"] = query
    resp = es.search(index=index, **body)
    return [
        (b["key"], int(b["doc_count"])) for b in resp["aggregations"]["by"]["buckets"]
    ]


def compute_discourse_coverage(es: Elasticsearch) -> DiscourseCoverage:
    """Fan out one `_count` per framework + by-year + by-chamber aggs."""
    total = _count(es, index="mo-speeches")
    substantive = _count(
        es, index="mo-speeches", query={"term": {"is_substantive": True}}
    )
    coded_any = _count(
        es,
        index="mo-speeches",
        query={"exists": {"field": "enrichments.discourse_producer"}},
    )

    rows: list[FrameworkCoverage] = []
    for fw in DISCOURSE_FRAMEWORKS:
        # The "coded" probe field differs per framework. Hawkins / DQI /
        # V-Party all carry `framework_version` (the canonical
        # "this rubric ran on this speech" signal). Voice doesn't —
        # its block is `{dominant_voice, voices_seen, classifications}`
        # with no version field; use `dominant_voice` as its presence
        # probe instead (always emitted when voice runs).
        coded_field = (
            f"enrichments.discourse.{fw}.dominant_voice"
            if fw == "voice"
            else f"enrichments.discourse.{fw}.framework_version"
        )
        coded = _count(
            es,
            index="mo-speeches",
            query={"exists": {"field": coded_field}},
        )
        if fw == "dqi":
            with_markers: int | None = None
        elif fw == "voice":
            # Voice's marker-equivalent: at least one classification.
            with_markers = _count(
                es,
                index="mo-speeches",
                query={
                    "exists": {"field": f"enrichments.discourse.{fw}.classifications"}
                },
            )
        else:
            with_markers = _count(
                es,
                index="mo-speeches",
                query={
                    "range": {f"enrichments.discourse.{fw}.marker_count": {"gt": 0}}
                },
            )
        rows.append(
            FrameworkCoverage(framework=fw, coded=coded, with_markers=with_markers)
        )

    by_year_raw = _terms_agg(
        es,
        index="mo-speeches",
        field="year",
        query={"exists": {"field": "enrichments.discourse_producer"}},
        size=50,
    )
    by_year = tuple(sorted(((int(y), c) for y, c in by_year_raw), key=lambda t: t[0]))

    by_chamber_raw = _terms_agg(
        es,
        index="mo-speeches",
        field="chamber",
        query={"exists": {"field": "enrichments.discourse_producer"}},
        size=10,
    )
    by_chamber = tuple(sorted((str(c), n) for c, n in by_chamber_raw))

    return DiscourseCoverage(
        total_speeches=total,
        substantive=substantive,
        coded_any=coded_any,
        frameworks=tuple(rows),
        by_year=by_year,
        by_chamber=by_chamber,
    )


def compute_embedding_coverage(es: Elasticsearch) -> EmbeddingCoverage:
    """One `_count` per embeddable grain for total + embedded."""
    grains: list[GrainEmbedding] = []
    for g in EMBEDDABLE_GRAINS:
        total = _count(es, index=g)
        embedded = _count(
            es,
            index=g,
            query={"exists": {"field": "enrichments.embedding"}},
        )
        grains.append(GrainEmbedding(grain=g, total=total, embedded=embedded))
    return EmbeddingCoverage(grains=tuple(grains))


def compute_coverage(es: Elasticsearch) -> CoverageReport:
    """Build the full report. Single entry point for the CLI."""
    return CoverageReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        discourse=compute_discourse_coverage(es),
        embedding=compute_embedding_coverage(es),
        notes=(
            "discourse.voice runs only when Hawkins emits ≥1 populism marker — "
            "its coded count equals the Hawkins markers-positive count.",
            "discourse.dqi emits per-axis levels (no `marker_count`); the "
            "with-markers column is omitted as N/A.",
            "Substantive speeches are speeches with text_length ≥ 100 chars and a "
            "canonical speaker — the population the analyzer codes from.",
            "Embedding coverage uses an `exists` probe on `enrichments.embedding`; "
            "the `mo-documents` / `mo-votes` / `mo-persons` grains carry no "
            "embeddable text and are omitted from the table.",
        ),
    )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _pct(numer: int, denom: int) -> str:
    if denom <= 0:
        return "  n/a"
    return f"{numer / denom * 100:5.1f}%"


def _fmt_int(n: int) -> str:
    return f"{n:>11,}"


def _table(rows: list[list[str]], *, headers: list[str]) -> str:
    """Render a plain-text table with right-padded columns.

    Auto-widths each column to the longest cell. Uses ASCII pipes so
    the output is copy-pasteable as markdown.
    """
    cols = list(zip(*([headers, *rows])))
    widths = [max(len(cell) for cell in col) for col in cols]
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    line = lambda r: "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths)) + " |"  # noqa: E731
    out = [sep, line(headers), sep]
    out.extend(line(r) for r in rows)
    out.append(sep)
    return "\n".join(out)


def format_text(report: CoverageReport) -> str:
    """Render the report as text tables (markdown-compatible)."""
    d = report.discourse
    e = report.embedding

    out: list[str] = []
    out.append(f"Coverage report — generated {report.generated_at}")
    out.append("")
    out.append("== Discourse LLM coverage on mo-speeches ==")
    out.append("")
    headline_rows = [
        ["Total speeches", _fmt_int(d.total_speeches), "      —"],
        [
            "Substantive (text ≥ 100, canonical)",
            _fmt_int(d.substantive),
            _pct(d.substantive, d.total_speeches),
        ],
        [
            "Coded with any framework",
            _fmt_int(d.coded_any),
            _pct(d.coded_any, d.substantive),
        ],
    ]
    out.append(
        _table(
            headline_rows,
            headers=["Population", "Count", "% of parent"],
        )
    )
    out.append("")
    out.append("Per framework:")
    fw_rows: list[list[str]] = []
    for fw in d.frameworks:
        wm = "    n/a" if fw.with_markers is None else _fmt_int(fw.with_markers)
        wm_pct = (
            "      —"
            if fw.with_markers is None or fw.coded == 0
            else _pct(fw.with_markers, fw.coded)
        )
        fw_rows.append([fw.framework, _fmt_int(fw.coded), wm, wm_pct])
    out.append(
        _table(
            fw_rows,
            headers=["Framework", "Coded", "With ≥1 marker", "% of coded"],
        )
    )

    if d.by_year:
        out.append("")
        out.append("By year:")
        year_rows = [[str(y), _fmt_int(n)] for y, n in d.by_year]
        out.append(_table(year_rows, headers=["Year", "Coded"]))

    if d.by_chamber:
        out.append("")
        out.append("By chamber:")
        chamber_rows = [[c, _fmt_int(n)] for c, n in d.by_chamber]
        out.append(_table(chamber_rows, headers=["Chamber", "Coded"]))

    out.append("")
    out.append("== Embedding coverage across grains ==")
    out.append("")
    emb_rows = [
        [g.grain, _fmt_int(g.total), _fmt_int(g.embedded), _pct(g.embedded, g.total)]
        for g in e.grains
    ]
    out.append(_table(emb_rows, headers=["Grain", "Total", "Embedded", "% embedded"]))

    if report.notes:
        out.append("")
        out.append("Notes:")
        for n in report.notes:
            out.append(f"  - {n}")

    return "\n".join(out)


def format_json(report: CoverageReport) -> dict[str, Any]:
    """Render the report as a JSON-serialisable dict."""
    d = report.discourse
    e = report.embedding
    return {
        "generated_at": report.generated_at,
        "discourse": {
            "total_speeches": d.total_speeches,
            "substantive": d.substantive,
            "coded_any": d.coded_any,
            "coded_pct_of_substantive": (
                d.coded_any / d.substantive if d.substantive else 0.0
            ),
            "frameworks": [
                {
                    "framework": fw.framework,
                    "coded": fw.coded,
                    "with_markers": fw.with_markers,
                    "with_markers_pct_of_coded": (
                        None
                        if fw.with_markers is None or fw.coded == 0
                        else fw.with_markers / fw.coded
                    ),
                }
                for fw in d.frameworks
            ],
            "by_year": [{"year": y, "coded": n} for y, n in d.by_year],
            "by_chamber": [{"chamber": c, "coded": n} for c, n in d.by_chamber],
        },
        "embedding": {
            "grains": [
                {
                    "grain": g.grain,
                    "total": g.total,
                    "embedded": g.embedded,
                    "embedded_pct": g.pct,
                }
                for g in e.grains
            ],
        },
        "notes": list(report.notes),
    }


__all__ = [
    "DISCOURSE_FRAMEWORKS",
    "EMBEDDABLE_GRAINS",
    "CoverageReport",
    "DiscourseCoverage",
    "EmbeddingCoverage",
    "FrameworkCoverage",
    "GrainEmbedding",
    "compute_coverage",
    "compute_discourse_coverage",
    "compute_embedding_coverage",
    "format_json",
    "format_text",
]
