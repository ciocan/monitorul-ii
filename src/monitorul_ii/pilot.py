"""Pilot benchmark analysis — compare LLM candidates against an opus gold standard.

Reads from a ``data/pilot_results/``-shaped directory:

* ``<results_dir>/<model_key>/`` — per-speech JSON files (one per tested speech).
* ``<results_dir>/<model_key>_summary.json`` — aggregated operational stats.

The gold model (default ``opus``) acts as the labelling reference. For each
candidate model, every speech file present in **both** the gold and candidate
directories is paired and scored on two agreement axes:

* **Hawkins** — ``results.hawkins.output.score`` (ordinal 0-2).
* **DQI** — ``results.dqi.output.level_of_justification`` (ordinal 0-2).

Agreement metrics per axis: exact-match rate + mean absolute delta (MAD).

Candidates are ranked by: Hawkins match-rate ↓ → DQI match-rate ↓ → cost ↑
(i.e. highest agreement first, cheapest as a tiebreaker for equal agreement).

Output is a frozen dataclass graph. ``format_text`` / ``format_json`` are pure
projections — no I/O, no mutation — so tests can exercise formatters without
hitting the filesystem.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_hawkins_score(d: dict[str, Any]) -> int | None:
    r = (d.get("results") or {}).get("hawkins")
    if not r:
        return None
    out = r.get("output")
    if not isinstance(out, dict):
        return None
    v = out.get("score")
    return int(v) if v is not None else None


def _get_dqi_level(d: dict[str, Any]) -> int | None:
    r = (d.get("results") or {}).get("dqi")
    if not r:
        return None
    out = r.get("output")
    if not isinstance(out, dict):
        return None
    v = out.get("level_of_justification")
    return int(v) if v is not None else None


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelAgreement:
    """Agreement row for one candidate model vs. the gold model."""

    model_key: str
    """Directory name / internal key (e.g. ``gemini-3.1-flash-lite``)."""

    model_label: str
    """Verbose model label from the speech files (e.g. ``gemini-3.1-flash-lite``)."""

    n_paired: int
    """Speeches present in both gold and candidate — the comparison universe."""

    n_total: int
    """Speeches in the candidate's summary (may differ from n_paired when
    gold has fewer files than candidate)."""

    hawkins_matches: int
    hawkins_total: int
    hawkins_mean_delta: float | None
    """Mean |gold_score - candidate_score| over paired speeches; None when
    no valid pairs exist."""

    dqi_matches: int
    dqi_total: int
    dqi_mean_delta: float | None

    cost_usd: float
    latency_ms: int
    calls: int
    errors: int
    fragments_missed: int

    @property
    def hawkins_match_rate(self) -> float | None:
        if self.hawkins_total == 0:
            return None
        return self.hawkins_matches / self.hawkins_total

    @property
    def dqi_match_rate(self) -> float | None:
        if self.dqi_total == 0:
            return None
        return self.dqi_matches / self.dqi_total

    @property
    def cost_usd_per_speech(self) -> float | None:
        if self.n_total == 0:
            return None
        return self.cost_usd / self.n_total

    @property
    def latency_ms_per_speech(self) -> float | None:
        if self.n_total == 0:
            return None
        return self.latency_ms / self.n_total


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Top-level benchmark report — returned by ``compute_benchmark``."""

    generated_at: str
    gold_model: str
    gold_n_speeches: int
    models: tuple[ModelAgreement, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def _load_summary(results_dir: Path, model_key: str) -> dict[str, Any]:
    path = results_dir / f"{model_key}_summary.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def compute_model_agreement(
    gold_dir: Path,
    candidate_dir: Path,
    *,
    summary: dict[str, Any] | None = None,
) -> ModelAgreement:
    """Pair speeches, compute agreement, wrap operational stats."""
    model_key = candidate_dir.name
    model_label = model_key

    gold_files: dict[str, Path] = {f.name: f for f in sorted(gold_dir.glob("*.json"))}
    cand_files: dict[str, Path] = {
        f.name: f for f in sorted(candidate_dir.glob("*.json"))
    }
    shared = sorted(gold_files.keys() & cand_files.keys())

    h_matches = h_total = h_delta_sum = 0
    d_matches = d_total = d_delta_sum = 0

    for fname in shared:
        gold_data = json.loads(gold_files[fname].read_text(encoding="utf-8"))
        cand_data = json.loads(cand_files[fname].read_text(encoding="utf-8"))

        if model_label == model_key and (cand_data.get("model_label")):
            model_label = cand_data["model_label"]

        g_h = _get_hawkins_score(gold_data)
        c_h = _get_hawkins_score(cand_data)
        if g_h is not None and c_h is not None:
            h_total += 1
            h_matches += int(g_h == c_h)
            h_delta_sum += abs(g_h - c_h)

        g_d = _get_dqi_level(gold_data)
        c_d = _get_dqi_level(cand_data)
        if g_d is not None and c_d is not None:
            d_total += 1
            d_matches += int(g_d == c_d)
            d_delta_sum += abs(g_d - c_d)

    totals = (summary or {}).get("totals", {})
    n_total = (summary or {}).get("n_speeches", len(cand_files))
    errors_sum = sum((summary or {}).get("errors", {}).values())
    frags = sum((summary or {}).get("fragments_not_found_by_kind", {}).values())

    return ModelAgreement(
        model_key=model_key,
        model_label=model_label,
        n_paired=len(shared),
        n_total=int(n_total),
        hawkins_matches=h_matches,
        hawkins_total=h_total,
        hawkins_mean_delta=h_delta_sum / h_total if h_total else None,
        dqi_matches=d_matches,
        dqi_total=d_total,
        dqi_mean_delta=d_delta_sum / d_total if d_total else None,
        cost_usd=float(totals.get("cost_usd") or 0.0),
        latency_ms=int(totals.get("latency_ms") or 0),
        calls=int(totals.get("calls") or 0),
        errors=errors_sum,
        fragments_missed=frags,
    )


def _rank_key(m: ModelAgreement) -> tuple[float, float, float]:
    """Sort key: higher agreement first, then cheaper per-speech."""
    h = m.hawkins_match_rate if m.hawkins_match_rate is not None else -1.0
    d = m.dqi_match_rate if m.dqi_match_rate is not None else -1.0
    cost = m.cost_usd_per_speech if m.cost_usd_per_speech is not None else 0.0
    return (-h, -d, cost)


def compute_benchmark(
    results_dir: Path,
    *,
    gold: str = "opus",
) -> BenchmarkReport:
    """Load all model directories and compute agreement vs. the gold model.

    Models without any speeches overlapping the gold set are included with
    ``n_paired=0`` and null agreement metrics so the report is complete.
    """
    gold_dir = results_dir / gold
    if not gold_dir.is_dir():
        raise FileNotFoundError(f"Gold model directory not found: {gold_dir}")

    gold_files = list(gold_dir.glob("*.json"))
    gold_n = len(gold_files)

    rows: list[ModelAgreement] = []
    for child in sorted(results_dir.iterdir()):
        if not child.is_dir() or child.name == gold:
            continue
        summary = _load_summary(results_dir, child.name)
        row = compute_model_agreement(gold_dir, child, summary=summary)
        rows.append(row)

    rows.sort(key=_rank_key)

    return BenchmarkReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        gold_model=gold,
        gold_n_speeches=gold_n,
        models=tuple(rows),
        notes=(
            "Hawkins score is ordinal 0-2 (0=none, 1=moderate, 2=strong). "
            "DQI level_of_justification is ordinal 0-2 (0=no, 1=inferior, 2=qualified). "
            "Match rate = exact agreement / paired speeches. "
            "MAD = mean |gold - candidate| over valid pairs.",
            "n_paired = speeches present in both gold and candidate; "
            "n_total = speeches in candidate summary.",
            "cost_usd and latency_ms are totals from the model summary file "
            "(normalised per speech in the /sp columns). "
            "Models with n_paired=0 have no overlap with the gold set.",
        ),
    )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _pct(rate: float | None) -> str:
    if rate is None:
        return "  n/a"
    return f"{rate * 100:5.1f}%"


def _mad(v: float | None) -> str:
    if v is None:
        return "  n/a"
    return f"{v:.2f}"


def _table(rows: list[list[str]], *, headers: list[str]) -> str:
    cols = list(zip(*([headers, *rows])))
    widths = [max(len(cell) for cell in col) for col in cols]
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    line = lambda r: "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths)) + " |"  # noqa: E731
    out = [sep, line(headers), sep]
    out.extend(line(r) for r in rows)
    out.append(sep)
    return "\n".join(out)


def format_text(report: BenchmarkReport) -> str:
    """Render the report as text tables (markdown-compatible)."""
    out: list[str] = []
    out.append(f"Pilot benchmark report — generated {report.generated_at}")
    out.append(f"Gold model: {report.gold_model}  ({report.gold_n_speeches} speeches)")
    out.append("")
    out.append("== Model ranking vs. opus gold ==")
    out.append("")

    rows: list[list[str]] = []
    for rank, m in enumerate(report.models, 1):
        cost_sp = (
            f"${m.cost_usd_per_speech:.4f}"
            if m.cost_usd_per_speech is not None
            else "n/a"
        )
        lat_sp = (
            f"{m.latency_ms_per_speech / 1000:.1f}s"
            if m.latency_ms_per_speech is not None
            else "n/a"
        )
        rows.append(
            [
                str(rank),
                m.model_label,
                str(m.n_paired),
                _pct(m.hawkins_match_rate),
                _mad(m.hawkins_mean_delta),
                _pct(m.dqi_match_rate),
                _mad(m.dqi_mean_delta),
                cost_sp,
                lat_sp,
                str(m.errors),
                str(m.fragments_missed),
            ]
        )

    out.append(
        _table(
            rows,
            headers=[
                "#",
                "Model",
                "n",
                "H-match",
                "H-MAD",
                "DQI-match",
                "DQI-MAD",
                "cost/sp",
                "lat/sp",
                "errors",
                "frags",
            ],
        )
    )

    if report.notes:
        out.append("")
        out.append("Notes:")
        for n in report.notes:
            out.append(f"  - {n}")

    return "\n".join(out)


def format_json(report: BenchmarkReport) -> dict[str, Any]:
    """Render the report as a JSON-serialisable dict."""
    return {
        "generated_at": report.generated_at,
        "gold_model": report.gold_model,
        "gold_n_speeches": report.gold_n_speeches,
        "models": [
            {
                "rank": rank,
                "model_key": m.model_key,
                "model_label": m.model_label,
                "n_paired": m.n_paired,
                "n_total": m.n_total,
                "hawkins": {
                    "match_rate": m.hawkins_match_rate,
                    "matches": m.hawkins_matches,
                    "total": m.hawkins_total,
                    "mean_abs_delta": m.hawkins_mean_delta,
                },
                "dqi": {
                    "match_rate": m.dqi_match_rate,
                    "matches": m.dqi_matches,
                    "total": m.dqi_total,
                    "mean_abs_delta": m.dqi_mean_delta,
                },
                "cost_usd": m.cost_usd,
                "cost_usd_per_speech": m.cost_usd_per_speech,
                "latency_ms": m.latency_ms,
                "latency_ms_per_speech": m.latency_ms_per_speech,
                "calls": m.calls,
                "errors": m.errors,
                "fragments_missed": m.fragments_missed,
            }
            for rank, m in enumerate(report.models, 1)
        ],
        "notes": list(report.notes),
    }


__all__ = [
    "BenchmarkReport",
    "ModelAgreement",
    "compute_benchmark",
    "compute_model_agreement",
    "format_json",
    "format_text",
]
