"""Tests for `src/monitorul_ii/elasticsearch/coverage.py`."""

from __future__ import annotations

import json
from typing import Any

import pytest

from monitorul_ii.elasticsearch import coverage as cov


# ---------------------------------------------------------------------------
# Fake ES client
# ---------------------------------------------------------------------------


class FakeES:
    """Minimal Elasticsearch stand-in for the coverage module.

    Routes `count` / `search` based on `(index, query)` keys an explicit
    fixture sets up. Any unanticipated query path raises so tests fail
    loudly when the module's call shape drifts.
    """

    def __init__(
        self,
        *,
        counts: dict[tuple[str, str], int],
        aggs: dict[tuple[str, str], list[tuple[Any, int]]] | None = None,
    ) -> None:
        self.counts = counts
        self.aggs = aggs or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @staticmethod
    def _key(query: dict[str, Any] | None) -> str:
        if query is None:
            return "*"
        # Stable hashable key. Sort keys so the test fixture is insensitive
        # to dict ordering in the production code.
        return json.dumps(query, sort_keys=True, ensure_ascii=False)

    def count(
        self, *, index: str, query: dict[str, Any] | None = None, **_: Any
    ) -> dict[str, Any]:
        key = (index, self._key(query))
        self.calls.append(("count", {"index": index, "query": query}))
        if key not in self.counts:
            raise KeyError(f"no fixture for count {key}")
        return {"count": self.counts[key]}

    def search(
        self,
        *,
        index: str,
        size: int = 10,
        query: dict[str, Any] | None = None,
        aggs: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            ("search", {"index": index, "size": size, "query": query, "aggs": aggs})
        )
        if not aggs:
            raise AssertionError("coverage module always passes aggs to search")
        key = (index, self._key(query))
        buckets = self.aggs.get(key, [])
        return {
            "hits": {"hits": []},
            "aggregations": {
                "by": {"buckets": [{"key": k, "doc_count": c} for k, c in buckets]}
            },
        }


# ---------------------------------------------------------------------------
# Dataclass and formatter tests (no ES dependency)
# ---------------------------------------------------------------------------


def _sample_report() -> cov.CoverageReport:
    return cov.CoverageReport(
        generated_at="2026-05-10T12:00:00+00:00",
        discourse=cov.DiscourseCoverage(
            total_speeches=818_405,
            substantive=535_526,
            coded_any=108_648,
            frameworks=(
                cov.FrameworkCoverage(
                    framework="hawkins", coded=108_648, with_markers=15_605
                ),
                cov.FrameworkCoverage(
                    framework="voice", coded=15_605, with_markers=15_605
                ),
                cov.FrameworkCoverage(
                    framework="dqi", coded=108_648, with_markers=None
                ),
                cov.FrameworkCoverage(
                    framework="vparty", coded=108_648, with_markers=1_861
                ),
            ),
            by_year=((2024, 10_984), (2025, 13_841)),
            by_chamber=(("Camera Deputaților", 70_000), ("Senatul", 38_648)),
        ),
        embedding=cov.EmbeddingCoverage(
            grains=(
                cov.GrainEmbedding(
                    grain="mo-speeches", total=535_526, embedded=535_526
                ),
                cov.GrainEmbedding(
                    grain="mo-agenda-items", total=10_000, embedded=8_000
                ),
            ),
        ),
    )


def test_grain_embedding_pct_zero_safe() -> None:
    g = cov.GrainEmbedding(grain="x", total=0, embedded=0)
    assert g.pct == 0.0


def test_grain_embedding_pct() -> None:
    g = cov.GrainEmbedding(grain="x", total=200, embedded=50)
    assert g.pct == 0.25


def test_format_text_renders_all_sections() -> None:
    text = cov.format_text(_sample_report())
    # Headline
    assert "Total speeches" in text
    assert "818,405" in text
    assert "535,526" in text
    assert "108,648" in text
    # Frameworks
    for fw in ("hawkins", "voice", "dqi", "vparty"):
        assert fw in text
    # N/A column for DQI
    assert "n/a" in text
    # By year / by chamber
    assert "2024" in text and "2025" in text
    assert "Camera Deputaților" in text and "Senatul" in text
    # Embedding section
    assert "Embedding coverage" in text
    assert "mo-speeches" in text and "mo-agenda-items" in text


def test_format_json_shape() -> None:
    payload = cov.format_json(_sample_report())
    assert payload["discourse"]["coded_any"] == 108_648
    assert payload["discourse"]["coded_pct_of_substantive"] == pytest.approx(
        108_648 / 535_526
    )
    fw_names = [f["framework"] for f in payload["discourse"]["frameworks"]]
    assert fw_names == list(cov.DISCOURSE_FRAMEWORKS)
    # DQI carries None for with_markers
    dqi = next(f for f in payload["discourse"]["frameworks"] if f["framework"] == "dqi")
    assert dqi["with_markers"] is None
    assert dqi["with_markers_pct_of_coded"] is None
    # Hawkins carries a numeric ratio
    hk = next(
        f for f in payload["discourse"]["frameworks"] if f["framework"] == "hawkins"
    )
    assert hk["with_markers_pct_of_coded"] == pytest.approx(15_605 / 108_648)
    # By-year / by-chamber preserved
    assert payload["discourse"]["by_year"] == [
        {"year": 2024, "coded": 10_984},
        {"year": 2025, "coded": 13_841},
    ]
    # Embedding grains
    grains = payload["embedding"]["grains"]
    assert {g["grain"] for g in grains} == {"mo-speeches", "mo-agenda-items"}


def test_format_json_is_json_serialisable() -> None:
    # Round-trip through json.dumps to catch any non-serialisable types.
    payload = cov.format_json(_sample_report())
    assert json.loads(json.dumps(payload, ensure_ascii=False))


def test_format_text_handles_empty_by_year_and_chamber() -> None:
    # Synthetic empty-corpus report should still render without exploding.
    rep = cov.CoverageReport(
        generated_at="2026-01-01T00:00:00+00:00",
        discourse=cov.DiscourseCoverage(
            total_speeches=0,
            substantive=0,
            coded_any=0,
            frameworks=tuple(
                cov.FrameworkCoverage(
                    framework=fw, coded=0, with_markers=0 if fw != "dqi" else None
                )
                for fw in cov.DISCOURSE_FRAMEWORKS
            ),
        ),
        embedding=cov.EmbeddingCoverage(grains=()),
    )
    text = cov.format_text(rep)
    # No optional sections rendered when the data is missing
    assert "By year" not in text
    assert "By chamber" not in text
    # No divide-by-zero noise in %
    assert "n/a" in text


# ---------------------------------------------------------------------------
# compute_* against the fake ES client
# ---------------------------------------------------------------------------


def test_compute_discourse_coverage_fans_out_correctly() -> None:
    qkey_substantive = json.dumps(
        {"term": {"is_substantive": True}}, sort_keys=True, ensure_ascii=False
    )
    qkey_coded_any = json.dumps(
        {"exists": {"field": "enrichments.discourse_producer"}},
        sort_keys=True,
        ensure_ascii=False,
    )

    def fw_exists_key(fw: str) -> str:
        # Voice probe field differs (it has no `framework_version`).
        field_name = (
            f"enrichments.discourse.{fw}.dominant_voice"
            if fw == "voice"
            else f"enrichments.discourse.{fw}.framework_version"
        )
        return json.dumps(
            {"exists": {"field": field_name}},
            sort_keys=True,
            ensure_ascii=False,
        )

    def marker_key(fw: str) -> str:
        return json.dumps(
            {"range": {f"enrichments.discourse.{fw}.marker_count": {"gt": 0}}},
            sort_keys=True,
            ensure_ascii=False,
        )

    voice_class_key = json.dumps(
        {"exists": {"field": "enrichments.discourse.voice.classifications"}},
        sort_keys=True,
        ensure_ascii=False,
    )

    counts = {
        ("mo-speeches", "*"): 100,
        ("mo-speeches", qkey_substantive): 80,
        ("mo-speeches", qkey_coded_any): 60,
        ("mo-speeches", fw_exists_key("hawkins")): 60,
        ("mo-speeches", marker_key("hawkins")): 12,
        ("mo-speeches", fw_exists_key("voice")): 12,
        ("mo-speeches", voice_class_key): 12,
        ("mo-speeches", fw_exists_key("dqi")): 60,
        ("mo-speeches", fw_exists_key("vparty")): 60,
        ("mo-speeches", marker_key("vparty")): 3,
    }
    aggs = {
        ("mo-speeches", qkey_coded_any): [(2024, 35), (2025, 25)],
    }

    es = FakeES(counts=counts, aggs=aggs)
    d = cov.compute_discourse_coverage(es)

    assert d.total_speeches == 100
    assert d.substantive == 80
    assert d.coded_any == 60
    assert {f.framework for f in d.frameworks} == set(cov.DISCOURSE_FRAMEWORKS)
    hk = next(f for f in d.frameworks if f.framework == "hawkins")
    assert hk.coded == 60 and hk.with_markers == 12
    voice = next(f for f in d.frameworks if f.framework == "voice")
    assert voice.coded == 12 and voice.with_markers == 12
    dqi = next(f for f in d.frameworks if f.framework == "dqi")
    assert dqi.coded == 60 and dqi.with_markers is None  # N/A signal
    vparty = next(f for f in d.frameworks if f.framework == "vparty")
    assert vparty.with_markers == 3
    assert d.by_year == ((2024, 35), (2025, 25))


def test_compute_embedding_coverage_iterates_grains() -> None:
    embed_key = json.dumps(
        {"exists": {"field": "enrichments.embedding"}},
        sort_keys=True,
        ensure_ascii=False,
    )
    counts: dict[tuple[str, str], int] = {}
    for g in cov.EMBEDDABLE_GRAINS:
        counts[(g, "*")] = 1000
        counts[(g, embed_key)] = 800
    es = FakeES(counts=counts)
    e = cov.compute_embedding_coverage(es)
    assert tuple(g.grain for g in e.grains) == cov.EMBEDDABLE_GRAINS
    for g in e.grains:
        assert g.total == 1000
        assert g.embedded == 800
        assert g.pct == pytest.approx(0.8)


def test_compute_coverage_stamps_timestamp() -> None:
    embed_key = json.dumps(
        {"exists": {"field": "enrichments.embedding"}},
        sort_keys=True,
        ensure_ascii=False,
    )
    qkey_substantive = json.dumps(
        {"term": {"is_substantive": True}}, sort_keys=True, ensure_ascii=False
    )
    qkey_coded_any = json.dumps(
        {"exists": {"field": "enrichments.discourse_producer"}},
        sort_keys=True,
        ensure_ascii=False,
    )

    def fw_exists_key(fw: str) -> str:
        field_name = (
            f"enrichments.discourse.{fw}.dominant_voice"
            if fw == "voice"
            else f"enrichments.discourse.{fw}.framework_version"
        )
        return json.dumps(
            {"exists": {"field": field_name}},
            sort_keys=True,
            ensure_ascii=False,
        )

    def marker_key(fw: str) -> str:
        return json.dumps(
            {"range": {f"enrichments.discourse.{fw}.marker_count": {"gt": 0}}},
            sort_keys=True,
            ensure_ascii=False,
        )

    counts: dict[tuple[str, str], int] = {
        ("mo-speeches", "*"): 0,
        ("mo-speeches", qkey_substantive): 0,
        ("mo-speeches", qkey_coded_any): 0,
        ("mo-speeches", fw_exists_key("hawkins")): 0,
        ("mo-speeches", marker_key("hawkins")): 0,
        ("mo-speeches", fw_exists_key("voice")): 0,
        (
            "mo-speeches",
            json.dumps(
                {"exists": {"field": "enrichments.discourse.voice.classifications"}},
                sort_keys=True,
                ensure_ascii=False,
            ),
        ): 0,
        ("mo-speeches", fw_exists_key("dqi")): 0,
        ("mo-speeches", fw_exists_key("vparty")): 0,
        ("mo-speeches", marker_key("vparty")): 0,
    }
    for g in cov.EMBEDDABLE_GRAINS:
        counts[(g, "*")] = 0
        counts[(g, embed_key)] = 0
    es = FakeES(counts=counts)
    report = cov.compute_coverage(es)
    assert report.generated_at.endswith("+00:00")
    assert report.notes  # the canned operator footnotes are present


# ---------------------------------------------------------------------------
# CLI argparse wiring
# ---------------------------------------------------------------------------


def test_cli_parser_accepts_coverage_subcommand() -> None:
    from monitorul_ii.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["coverage"])
    assert getattr(args, "as_json", None) is False
    args = parser.parse_args(["coverage", "--json"])
    assert args.as_json is True
