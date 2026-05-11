"""Tests for src/monitorul_ii/pilot.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monitorul_ii.pilot import (
    BenchmarkReport,
    ModelAgreement,
    compute_benchmark,
    compute_model_agreement,
    format_json,
    format_text,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _speech(
    model_key: str,
    model_label: str,
    *,
    hawkins_score: int | None,
    dqi_level: int | None,
) -> dict:
    """Minimal speech result dict."""
    hawkins_output = (
        {"score": hawkins_score, "score_unit": "ordinal_0_2"}
        if hawkins_score is not None
        else None
    )
    dqi_output = (
        {"level_of_justification": dqi_level, "score_unit": "ordinal_0_2"}
        if dqi_level is not None
        else None
    )
    return {
        "speech_id": "mo://2024/II/1#agenda-1#act-1",
        "model_key": model_key,
        "model_label": model_label,
        "results": {
            "hawkins": {"output": hawkins_output, "error": None},
            "dqi": {"output": dqi_output, "error": None},
        },
    }


def _write_speech(directory: Path, fname: str, speech: dict) -> Path:
    path = directory / fname
    path.write_text(json.dumps(speech), encoding="utf-8")
    return path


def _make_results_dir(
    tmp_path: Path,
    *,
    gold_speeches: list[dict],
    candidate_speeches: list[dict],
    candidate_key: str = "cand",
    gold_key: str = "opus",
    summary: dict | None = None,
) -> Path:
    results = tmp_path / "results"
    gold_dir = results / gold_key
    cand_dir = results / candidate_key
    gold_dir.mkdir(parents=True)
    cand_dir.mkdir(parents=True)

    for i, sp in enumerate(gold_speeches):
        _write_speech(gold_dir, f"speech_{i}.json", sp)
    for i, sp in enumerate(candidate_speeches):
        _write_speech(cand_dir, f"speech_{i}.json", sp)

    if summary is not None:
        summary_path = results / f"{candidate_key}_summary.json"
        summary_path.write_text(json.dumps(summary), encoding="utf-8")

    return results


# ---------------------------------------------------------------------------
# compute_model_agreement
# ---------------------------------------------------------------------------


class TestComputeModelAgreement:
    def test_perfect_hawkins_agreement(self, tmp_path: Path) -> None:
        results = _make_results_dir(
            tmp_path,
            gold_speeches=[
                _speech("opus", "claude-opus-4.7", hawkins_score=1, dqi_level=2),
                _speech("opus", "claude-opus-4.7", hawkins_score=0, dqi_level=0),
            ],
            candidate_speeches=[
                _speech("cand", "candidate-model", hawkins_score=1, dqi_level=2),
                _speech("cand", "candidate-model", hawkins_score=0, dqi_level=0),
            ],
        )
        row = compute_model_agreement(results / "opus", results / "cand")
        assert row.hawkins_match_rate == 1.0
        assert row.hawkins_mean_delta == 0.0
        assert row.dqi_match_rate == 1.0
        assert row.dqi_mean_delta == 0.0
        assert row.n_paired == 2

    def test_partial_disagreement(self, tmp_path: Path) -> None:
        results = _make_results_dir(
            tmp_path,
            gold_speeches=[
                _speech("opus", "claude-opus-4.7", hawkins_score=0, dqi_level=1),
                _speech("opus", "claude-opus-4.7", hawkins_score=1, dqi_level=2),
            ],
            candidate_speeches=[
                _speech(
                    "cand", "cand", hawkins_score=1, dqi_level=1
                ),  # H disagree, DQI agree
                _speech("cand", "cand", hawkins_score=1, dqi_level=2),  # both agree
            ],
        )
        row = compute_model_agreement(results / "opus", results / "cand")
        assert row.hawkins_matches == 1
        assert row.hawkins_total == 2
        assert row.hawkins_match_rate == 0.5
        assert row.hawkins_mean_delta == 0.5
        assert row.dqi_matches == 2
        assert row.dqi_match_rate == 1.0

    def test_no_shared_speeches(self, tmp_path: Path) -> None:
        results = tmp_path / "results"
        gold_dir = results / "opus"
        cand_dir = results / "cand"
        gold_dir.mkdir(parents=True)
        cand_dir.mkdir(parents=True)
        _write_speech(
            gold_dir, "a.json", _speech("opus", "opus", hawkins_score=1, dqi_level=0)
        )
        _write_speech(
            cand_dir, "b.json", _speech("cand", "cand", hawkins_score=1, dqi_level=0)
        )

        row = compute_model_agreement(gold_dir, cand_dir)
        assert row.n_paired == 0
        assert row.hawkins_match_rate is None
        assert row.dqi_match_rate is None

    def test_null_hawkins_output_skipped(self, tmp_path: Path) -> None:
        results = _make_results_dir(
            tmp_path,
            gold_speeches=[_speech("opus", "opus", hawkins_score=None, dqi_level=1)],
            candidate_speeches=[_speech("cand", "cand", hawkins_score=1, dqi_level=1)],
        )
        row = compute_model_agreement(results / "opus", results / "cand")
        assert row.hawkins_total == 0
        assert row.hawkins_match_rate is None
        assert row.dqi_total == 1

    def test_list_dqi_output_gracefully_skipped(self, tmp_path: Path) -> None:
        """When dqi.output is a list (malformed model response), skip the pair."""
        results = tmp_path / "results"
        gold_dir = results / "opus"
        cand_dir = results / "cand"
        gold_dir.mkdir(parents=True)
        cand_dir.mkdir(parents=True)

        gold_sp = _speech("opus", "opus", hawkins_score=1, dqi_level=2)
        cand_sp = _speech("cand", "cand", hawkins_score=1, dqi_level=0)
        # Simulate malformed list output
        cand_sp["results"]["dqi"]["output"] = [{"level_of_justification": 0}]

        _write_speech(gold_dir, "s.json", gold_sp)
        _write_speech(cand_dir, "s.json", cand_sp)

        row = compute_model_agreement(gold_dir, cand_dir)
        assert row.hawkins_total == 1
        assert row.dqi_total == 0  # list output → skipped

    def test_summary_stats_loaded(self, tmp_path: Path) -> None:
        summary = {
            "n_speeches": 5,
            "totals": {
                "cost_usd": 1.25,
                "latency_ms": 50000,
                "calls": 20,
            },
            "errors": {"schema_invalid": 2},
            "fragments_not_found_by_kind": {"dqi": 3},
        }
        results = _make_results_dir(
            tmp_path,
            gold_speeches=[],
            candidate_speeches=[],
            summary=summary,
        )
        row = compute_model_agreement(
            results / "opus", results / "cand", summary=summary
        )
        assert row.cost_usd == 1.25
        assert row.latency_ms == 50000
        assert row.calls == 20
        assert row.errors == 2
        assert row.fragments_missed == 3
        assert row.n_total == 5

    def test_model_label_extracted_from_speech(self, tmp_path: Path) -> None:
        results = _make_results_dir(
            tmp_path,
            gold_speeches=[
                _speech("opus", "claude-opus-4.7", hawkins_score=0, dqi_level=0)
            ],
            candidate_speeches=[
                _speech("cand", "gemini-3.1-flash-lite", hawkins_score=0, dqi_level=0)
            ],
        )
        row = compute_model_agreement(results / "opus", results / "cand")
        assert row.model_label == "gemini-3.1-flash-lite"


# ---------------------------------------------------------------------------
# compute_benchmark
# ---------------------------------------------------------------------------


class TestComputeBenchmark:
    def _make_two_model_results(self, tmp_path: Path) -> Path:
        results = tmp_path / "results"
        for model, h_score in [("opus", 1), ("alpha", 1), ("beta", 0)]:
            d = results / model
            d.mkdir(parents=True)
            _write_speech(
                d,
                "speech_0.json",
                _speech(model, model, hawkins_score=h_score, dqi_level=1),
            )
        return results

    def test_returns_report_with_all_candidates(self, tmp_path: Path) -> None:
        results = self._make_two_model_results(tmp_path)
        report = compute_benchmark(results, gold="opus")
        keys = {m.model_key for m in report.models}
        assert "alpha" in keys
        assert "beta" in keys
        assert "opus" not in keys

    def test_ranking_by_hawkins_agreement(self, tmp_path: Path) -> None:
        results = self._make_two_model_results(tmp_path)
        report = compute_benchmark(results, gold="opus")
        # alpha agrees (H=1 matches gold H=1); beta disagrees (H=0 vs gold H=1)
        assert report.models[0].model_key == "alpha"
        assert report.models[1].model_key == "beta"

    def test_gold_not_found_raises(self, tmp_path: Path) -> None:
        results = tmp_path / "results"
        results.mkdir()
        with pytest.raises(FileNotFoundError, match="Gold model directory not found"):
            compute_benchmark(results, gold="nonexistent")

    def test_gold_n_speeches(self, tmp_path: Path) -> None:
        results = self._make_two_model_results(tmp_path)
        report = compute_benchmark(results, gold="opus")
        assert report.gold_n_speeches == 1
        assert report.gold_model == "opus"

    def test_empty_candidate_directory(self, tmp_path: Path) -> None:
        results = tmp_path / "results"
        (results / "opus").mkdir(parents=True)
        _write_speech(
            results / "opus",
            "s.json",
            _speech("opus", "opus", hawkins_score=0, dqi_level=0),
        )
        (results / "empty_cand").mkdir()
        report = compute_benchmark(results, gold="opus")
        assert len(report.models) == 1
        assert report.models[0].n_paired == 0
        assert report.models[0].hawkins_match_rate is None


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


class TestFormatText:
    def _synthetic_report(self) -> BenchmarkReport:
        m = ModelAgreement(
            model_key="flash-lite",
            model_label="gemini-3.1-flash-lite",
            n_paired=10,
            n_total=10,
            hawkins_matches=9,
            hawkins_total=10,
            hawkins_mean_delta=0.1,
            dqi_matches=9,
            dqi_total=10,
            dqi_mean_delta=0.1,
            cost_usd=0.05,
            latency_ms=92822,
            calls=23,
            errors=0,
            fragments_missed=3,
        )
        return BenchmarkReport(
            generated_at="2026-01-01T00:00:00+00:00",
            gold_model="opus",
            gold_n_speeches=10,
            models=(m,),
        )

    def test_header_present(self) -> None:
        text = format_text(self._synthetic_report())
        assert "Pilot benchmark report" in text
        assert "Gold model: opus" in text

    def test_model_label_in_table(self) -> None:
        text = format_text(self._synthetic_report())
        assert "gemini-3.1-flash-lite" in text

    def test_match_rate_shown(self) -> None:
        text = format_text(self._synthetic_report())
        assert "90.0%" in text

    def test_cost_shown(self) -> None:
        text = format_text(self._synthetic_report())
        assert "$0.0050" in text

    def test_no_match_rate_when_none(self) -> None:
        m = ModelAgreement(
            model_key="x",
            model_label="x",
            n_paired=0,
            n_total=0,
            hawkins_matches=0,
            hawkins_total=0,
            hawkins_mean_delta=None,
            dqi_matches=0,
            dqi_total=0,
            dqi_mean_delta=None,
            cost_usd=0.0,
            latency_ms=0,
            calls=0,
            errors=0,
            fragments_missed=0,
        )
        report = BenchmarkReport(
            generated_at="2026-01-01T00:00:00+00:00",
            gold_model="opus",
            gold_n_speeches=10,
            models=(m,),
        )
        text = format_text(report)
        assert "n/a" in text


class TestFormatJson:
    def _simple_report(self) -> BenchmarkReport:
        m = ModelAgreement(
            model_key="test",
            model_label="test-model",
            n_paired=5,
            n_total=5,
            hawkins_matches=4,
            hawkins_total=5,
            hawkins_mean_delta=0.2,
            dqi_matches=5,
            dqi_total=5,
            dqi_mean_delta=0.0,
            cost_usd=0.10,
            latency_ms=30000,
            calls=10,
            errors=1,
            fragments_missed=0,
        )
        return BenchmarkReport(
            generated_at="2026-01-01T00:00:00+00:00",
            gold_model="opus",
            gold_n_speeches=5,
            models=(m,),
        )

    def test_json_structure(self) -> None:
        d = format_json(self._simple_report())
        assert d["gold_model"] == "opus"
        assert d["gold_n_speeches"] == 5
        assert len(d["models"]) == 1

    def test_json_model_fields(self) -> None:
        d = format_json(self._simple_report())
        m = d["models"][0]
        assert m["rank"] == 1
        assert m["model_key"] == "test"
        assert m["hawkins"]["match_rate"] == pytest.approx(0.8)
        assert m["hawkins"]["mean_abs_delta"] == pytest.approx(0.2)
        assert m["dqi"]["match_rate"] == pytest.approx(1.0)
        assert m["cost_usd_per_speech"] == pytest.approx(0.02)
        assert m["errors"] == 1

    def test_json_serialisable(self) -> None:
        d = format_json(self._simple_report())
        # Should not raise
        json.dumps(d)


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestPilotCli:
    def test_missing_results_dir(self, tmp_path: Path) -> None:
        from monitorul_ii.cli import _build_parser

        p = _build_parser()
        args = p.parse_args(
            ["pilot-results", "--results-dir", str(tmp_path / "noexist")]
        )
        from monitorul_ii.cli import cmd_pilot

        rc = cmd_pilot(args)
        assert rc == 2

    def test_missing_gold_dir(self, tmp_path: Path) -> None:
        (tmp_path / "results").mkdir()
        from monitorul_ii.cli import _build_parser, cmd_pilot

        p = _build_parser()
        args = p.parse_args(
            [
                "pilot-results",
                "--results-dir",
                str(tmp_path / "results"),
                "--gold",
                "badmodel",
            ]
        )
        rc = cmd_pilot(args)
        assert rc == 2

    def test_successful_run(self, tmp_path: Path, capsys) -> None:
        results = tmp_path / "results"
        gold_dir = results / "opus"
        cand_dir = results / "test_model"
        gold_dir.mkdir(parents=True)
        cand_dir.mkdir(parents=True)
        for d, key in [(gold_dir, "opus"), (cand_dir, "test_model")]:
            _write_speech(d, "s.json", _speech(key, key, hawkins_score=1, dqi_level=2))

        from monitorul_ii.cli import _build_parser, cmd_pilot

        p = _build_parser()
        args = p.parse_args(["pilot-results", "--results-dir", str(results)])
        rc = cmd_pilot(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Pilot benchmark report" in out
        assert "test_model" in out

    def test_json_flag(self, tmp_path: Path, capsys) -> None:
        results = tmp_path / "results"
        gold_dir = results / "opus"
        cand_dir = results / "cand"
        gold_dir.mkdir(parents=True)
        cand_dir.mkdir(parents=True)
        for d, key in [(gold_dir, "opus"), (cand_dir, "cand")]:
            _write_speech(d, "s.json", _speech(key, key, hawkins_score=0, dqi_level=1))

        from monitorul_ii.cli import _build_parser, cmd_pilot

        p = _build_parser()
        args = p.parse_args(["pilot-results", "--results-dir", str(results), "--json"])
        rc = cmd_pilot(args)
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "gold_model" in data
        assert "models" in data
