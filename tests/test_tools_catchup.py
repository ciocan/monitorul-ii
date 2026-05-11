"""Tests for tools.catchup — the end-to-end pipeline catch-up runner."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from tools.catchup import (
    ANALYZE_MAX_WORDS,
    STAGE_INPUT_PATTERN,
    STAGES,
    STAGE_NAMES,
    Runner,
    StageResult,
    _detect_date_range,
    _es_verify_certs,
    _filter_stages,
    _http_probe,
    _summarise_cmd,
    build_report,
    main,
    print_summary,
)


# ---- _detect_date_range --------------------------------------------------


def test_detect_date_range_no_db_returns_today_today(tmp_path: Path):
    """No DB on disk → both endpoints fall back to today (a 0-day no-op
    is harmless and matches the contract)."""
    today = date.today()
    f, u = _detect_date_range(tmp_path / "missing.db")
    assert f == today
    assert u == today


def test_detect_date_range_empty_db_returns_today_today(tmp_path: Path):
    """A DB with no `days` rows → same fallback."""
    db = tmp_path / "empty.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE days (date TEXT, status TEXT)")
    today = date.today()
    f, u = _detect_date_range(db)
    assert f == today
    assert u == today


def test_detect_date_range_resumes_day_after_latest_ok(tmp_path: Path):
    """The most recent `status='ok'` date sets `from = day_after`."""
    db = tmp_path / "audit.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE days (date TEXT, status TEXT)")
        con.execute("INSERT INTO days VALUES ('2026-04-10', 'ok')")
        con.execute("INSERT INTO days VALUES ('2026-04-15', 'ok')")
        con.execute("INSERT INTO days VALUES ('2026-04-20', 'failed')")  # not 'ok'
    f, _u = _detect_date_range(db)
    assert f == date(2026, 4, 16)


def test_detect_date_range_until_is_today(tmp_path: Path):
    db = tmp_path / "audit.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE days (date TEXT, status TEXT)")
        con.execute("INSERT INTO days VALUES ('2026-04-15', 'ok')")
    _f, u = _detect_date_range(db)
    assert u == date.today()


# ---- _filter_stages ------------------------------------------------------


def test_filter_stages_default_returns_all():
    assert [s["name"] for s in _filter_stages([], [])] == list(STAGE_NAMES)


def test_filter_stages_skip_drops_named_stages():
    out = _filter_stages(["embed", "index"], [])
    assert "embed" not in [s["name"] for s in out]
    assert "index" not in [s["name"] for s in out]
    assert "fetch" in [s["name"] for s in out]


def test_filter_stages_only_overrides_skip():
    """--only is a strict allowlist; --skip is ignored when --only is set."""
    out = _filter_stages(["fetch"], ["index", "embed"])
    assert {s["name"] for s in out} == {"index", "embed"}


def test_filter_stages_unknown_skip_raises():
    with pytest.raises(ValueError, match="unknown stage"):
        _filter_stages(["bogus"], [])


def test_filter_stages_unknown_only_raises():
    with pytest.raises(ValueError, match="unknown stage"):
        _filter_stages([], ["bogus"])


# ---- _http_probe ---------------------------------------------------------


def test_http_probe_returns_false_for_unreachable():
    """A bogus host returns (False, error message). The probe must not
    raise — callers depend on the (ok, msg) tuple shape."""
    ok, msg = _http_probe("http://127.0.0.1:1/nope", timeout=0.5)
    assert ok is False
    assert isinstance(msg, str) and msg


# ---- _es_verify_certs ----------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),
        ("", True),
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("OFF", False),
        # Anything unexpected is interpreted as the safe default (True)
        # rather than raising — the catchup is best-effort visibility.
        ("garbage", True),
    ],
)
def test_es_verify_certs_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("ES_VERIFY_CERTS", raising=False)
    else:
        monkeypatch.setenv("ES_VERIFY_CERTS", value)
    assert _es_verify_certs() is expected


# ---- build_report --------------------------------------------------------


def test_build_report_aggregates_stage_results():
    results = [
        StageResult(
            name="fetch",
            status="ok",
            duration_s=10.0,
            post_check={"new_pdfs": 5},
        ),
        StageResult(
            name="convert",
            status="ok",
            duration_s=22.5,
            post_check={"new_mds": 5},
        ),
        StageResult(
            name="extract",
            status="fail",
            duration_s=3.0,
            reason="subprocess exit 1",
        ),
        StageResult(
            name="link",
            status="skipped",
            duration_s=0.0,
            reason="pre-check failed",
        ),
    ]
    report = build_report(
        date_from=date(2026, 4, 15),
        date_until=date(2026, 5, 8),
        results=results,
        cli_args={"workers": 8, "include_cleanup": False},
    )
    assert report["date_range"] == {"from": "2026-04-15", "until": "2026-05-08"}
    assert report["summary"]["stages_ok"] == 2
    assert report["summary"]["stages_failed"] == 1
    assert report["summary"]["stages_skipped"] == 1
    assert report["summary"]["total_duration_s"] == 35.5
    assert len(report["stages"]) == 4
    assert report["stages"][0]["post_check"]["new_pdfs"] == 5
    assert report["stages"][2]["reason"] == "subprocess exit 1"


def test_build_report_includes_es_metrics_from_index_stage():
    results = [
        StageResult(
            name="index",
            status="ok",
            duration_s=42.0,
            post_check={
                "ok": True,
                "es_doc_count_in_range": 12,
                "es_latest_published": "2026-05-08",
                "es_latest_document_id": "mo://2026/II/0123",
            },
        ),
    ]
    report = build_report(
        date_from=date(2026, 4, 15),
        date_until=date(2026, 5, 8),
        results=results,
        cli_args={},
    )
    assert report["summary"]["es_doc_count_in_range"] == 12
    assert report["summary"]["es_latest_published"] == "2026-05-08"
    assert report["summary"]["es_latest_document_id"] == "mo://2026/II/0123"


def test_build_report_skips_es_metrics_when_index_failed():
    results = [
        StageResult(name="index", status="fail", duration_s=1.0, reason="boom"),
    ]
    report = build_report(
        date_from=date(2026, 1, 1),
        date_until=date(2026, 1, 1),
        results=results,
        cli_args={},
    )
    assert "es_doc_count_in_range" not in report["summary"]


def test_build_report_passes_through_preflight():
    """Pre-flight section is round-tripped verbatim into the report."""
    pre = {
        "working_dir": "/x",
        "embed": {"ok": True, "url": "http://embed", "probe": "HTTP 200"},
        "es": {"ok": False, "reason": "ES_URL or ES_API_KEY missing"},
    }
    report = build_report(
        date_from=date(2026, 5, 8),
        date_until=date(2026, 5, 8),
        results=[],
        cli_args={},
        preflight=pre,
    )
    assert report["preflight"] == pre


def test_build_report_preflight_defaults_to_empty_dict():
    """Callers that don't pass preflight (older callers / tests) get {}."""
    report = build_report(
        date_from=date(2026, 5, 8),
        date_until=date(2026, 5, 8),
        results=[],
        cli_args={},
    )
    assert report["preflight"] == {}


# ---- Runner pre-checks (unit) --------------------------------------------


def _runner(tmp_path: Path, **overrides) -> Runner:
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    db = tmp_path / "monitorul.db"
    defaults = dict(
        date_from=date(2026, 4, 15),
        date_until=date(2026, 5, 8),
        workers=2,
        db_path=db,
        pdfs_dir=pdfs,
        embed_url="http://127.0.0.1:1",
        es_url="http://127.0.0.1:1",
        es_api_key="dummy",
        include_cleanup=False,
        include_mapping_bump=False,
        analyze_provider="openrouter",
        continue_on_error=False,
    )
    defaults.update(overrides)
    return Runner(**defaults)


def test_runner_pre_convert_requires_pdf_in_range(tmp_path: Path):
    r = _runner(tmp_path)
    pre = r._pre_convert()
    assert pre["ok"] is False
    assert pre["pdfs_in_range"] == 0

    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.pdf").write_bytes(b"%PDF-1.4")
    pre = r._pre_convert()
    assert pre["ok"] is True
    assert pre["pdfs_in_range"] == 1


def test_runner_pre_extract_requires_md_in_range(tmp_path: Path):
    r = _runner(tmp_path)
    assert r._pre_extract() == {"ok": False, "mds_in_range": 0}

    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.md").write_text("body")
    pre = r._pre_extract()
    assert pre["ok"] is True
    assert pre["mds_in_range"] == 1


def test_runner_pre_link_backfill_require_sidecars_in_range(tmp_path: Path):
    r = _runner(tmp_path)
    assert r._pre_link()["ok"] is False
    assert r._pre_backfill()["ok"] is False

    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.extraction.json").write_text("{}")
    assert r._pre_link()["ok"] is True
    assert r._pre_backfill()["ok"] is True


def test_runner_pre_checks_ignore_out_of_range(tmp_path: Path):
    """Files outside [from, until] don't count toward pre-check OK."""
    r = _runner(tmp_path)
    (r.pdfs_dir / "2025-12-01_MO-PII-1-2025.md").write_text("body")  # before range
    (r.pdfs_dir / "2027-01-01_MO-PII-1-2027.md").write_text("body")  # after range
    assert r._pre_extract()["ok"] is False
    assert r._pre_extract()["mds_in_range"] == 0


def test_runner_pre_index_returns_false_without_es_creds(tmp_path: Path):
    r = _runner(tmp_path, es_url=None, es_api_key=None)
    # In-range sidecar required first; without it the early-return wins.
    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.extraction.json").write_text("{}")
    pre = r._pre_index()
    assert pre["ok"] is False
    assert "missing" in (pre.get("reason") or "")


def test_runner_pre_index_skips_when_no_sidecars_in_range(tmp_path: Path):
    """Even with valid ES creds, no sidecars in range → skip the stage."""
    r = _runner(tmp_path, es_url="http://x", es_api_key="k")
    pre = r._pre_index()
    assert pre["ok"] is False
    assert pre["sidecars_in_range"] == 0


# ---- date-range scoping (the user-facing fix) ----------------------------


def test_files_in_range_filters_by_date_prefix(tmp_path: Path):
    r = _runner(tmp_path)
    in_range1 = r.pdfs_dir / "2026-04-20_MO-PII-1-2026.pdf"
    in_range2 = r.pdfs_dir / "2026-05-01_MO-PII-2-2026.pdf"
    before = r.pdfs_dir / "2025-12-31_MO-PII-99-2025.pdf"
    after = r.pdfs_dir / "2027-01-01_MO-PII-1-2027.pdf"
    nondate = r.pdfs_dir / "no-date-prefix.pdf"
    for p in (in_range1, in_range2, before, after, nondate):
        p.write_bytes(b"%PDF-1.4")
    paths = r._files_in_range("*.pdf")
    assert paths == [in_range1, in_range2]


def test_stage_args_passes_only_in_range_files_to_convert(tmp_path: Path):
    r = _runner(tmp_path)
    in_range = r.pdfs_dir / "2026-04-20_MO-PII-1-2026.pdf"
    out_of_range = r.pdfs_dir / "2025-01-01_MO-PII-1-2025.pdf"
    in_range.write_bytes(b"%PDF-1.4")
    out_of_range.write_bytes(b"%PDF-1.4")
    args = r._stage_args("convert")
    # The first arg(s) are the file paths; -j N is appended at the end.
    assert str(in_range) in args
    assert str(out_of_range) not in args
    assert "-j" in args
    assert args[args.index("-j") + 1] == str(r.workers)


def test_stage_args_passes_only_in_range_sidecars_to_index(tmp_path: Path):
    r = _runner(tmp_path)
    in_range = r.pdfs_dir / "2026-04-20_MO-PII-1-2026.extraction.json"
    out_of_range = r.pdfs_dir / "2010-01-01_MO-PII-1-2010.extraction.json"
    in_range.write_text("{}")
    out_of_range.write_text("{}")
    args = r._stage_args("index")
    assert str(in_range) in args
    assert str(out_of_range) not in args


def test_stage_args_returns_empty_path_list_when_no_inputs(tmp_path: Path):
    """When no files match the date range, _stage_args yields the flag
    suffix only — but the per-stage pre-check should have already
    skipped the stage so the subprocess never runs. We verify the args
    shape so a future bug doesn't sneak through."""
    r = _runner(tmp_path)
    # convert: just `-j N` (no path positional) → CLI would error if run,
    # which is why _pre_convert returns ok=False up front.
    assert r._stage_args("convert") == ["-j", str(r.workers)]
    assert r._stage_args("link") == []


def test_stage_args_fetch_passes_date_range(tmp_path: Path):
    r = _runner(tmp_path)
    args = r._stage_args("fetch")
    assert args == ["2026-04-15", "--until", "2026-05-08"]


def test_stage_input_pattern_table_covers_all_non_fetch_stages():
    non_fetch = [n for n in STAGE_NAMES if n != "fetch"]
    assert set(non_fetch) == set(STAGE_INPUT_PATTERN.keys())


# ---- _summarise_cmd ------------------------------------------------------


def test_summarise_cmd_short_passes_through():
    cmd = ["uv", "run", "monitorul-ii", "fetch", "2026-04-15", "--until", "2026-05-08"]
    assert (
        _summarise_cmd(cmd) == "uv run monitorul-ii fetch 2026-04-15 --until 2026-05-08"
    )


def test_summarise_cmd_elides_long_path_lists():
    cmd = (
        ["uv", "run", "monitorul-ii", "convert"]
        + [f"/p/{i}.pdf" for i in range(50)]
        + ["-j", "8"]
    )
    out = _summarise_cmd(cmd, max_paths=3)
    # First 3 paths shown
    assert "/p/0.pdf" in out
    assert "/p/1.pdf" in out
    assert "/p/2.pdf" in out
    # 47 others elided
    assert "(+47 more)" in out
    # Flags retained
    assert "-j 8" in out
    # Path #4 not shown
    assert "/p/3.pdf" not in out


def test_summarise_cmd_keeps_all_paths_under_threshold():
    cmd = ["uv", "run", "monitorul-ii", "convert", "/p/1.pdf", "/p/2.pdf", "-j", "8"]
    out = _summarise_cmd(cmd, max_paths=3)
    assert "(+" not in out
    assert "/p/1.pdf" in out and "/p/2.pdf" in out


def test_runner_post_fetch_reports_new_pdf_delta(tmp_path: Path):
    r = _runner(tmp_path)
    # Drop a file AFTER the runner snapshotted the count, so the post
    # check sees the delta.
    assert r._pre_pdf_count == 0
    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.pdf").write_bytes(b"%PDF-1.4")
    post = r._post_fetch()
    assert post["new_pdfs"] == 1
    assert post["total_pdfs"] == 1


def test_runner_post_convert_flags_unpaired_pdfs(tmp_path: Path):
    r = _runner(tmp_path)
    paired = r.pdfs_dir / "2026-04-20_MO-PII-1-2026.pdf"
    paired.write_bytes(b"%PDF-1.4")
    paired.with_suffix(".md").write_text("# md")
    unpaired = r.pdfs_dir / "2026-04-21_MO-PII-2-2026.pdf"
    unpaired.write_bytes(b"%PDF-1.4")
    post = r._post_convert()
    assert post["unpaired_count"] == 1
    assert "2026-04-21_MO-PII-2-2026.pdf" in post["unpaired_pdfs_in_range"]


def test_runner_post_extract_counts_rejected(tmp_path: Path):
    r = _runner(tmp_path)
    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.rejected.json").write_text("{}")
    post = r._post_extract()
    assert post["rejected_in_range"] == 1


def test_runner_post_backfill_counts_speakers(tmp_path: Path):
    r = _runner(tmp_path)
    sidecar = {
        "body": {
            "session": {
                "chair": [
                    {
                        "raw": "Domnul Florin Iordache",
                        "name": "Florin Iordache",
                        "title": None,
                        "role": None,
                        "party_group": None,
                        "person_id": "iordache-florin",
                    },
                    {
                        "raw": "Doamna Inexistentă Persoană",
                        "name": "Inexistentă Persoană",
                        "title": None,
                        "role": None,
                        "party_group": None,
                        "person_id": None,  # unresolved
                    },
                ],
            }
        }
    }
    p = r.pdfs_dir / "2026-04-20_MO-PII-1-2026.extraction.json"
    p.write_text(json.dumps(sidecar))
    post = r._post_backfill()
    assert post["speakers_with_person_id"] == 1
    assert post["speakers_unresolved"] == 1
    assert post["resolution_rate"] == 0.5


# ---- print_summary smoke -------------------------------------------------


def test_print_summary_smoke():
    """Just verify the renderer doesn't throw on a representative report."""
    import io

    report = {
        "run_id": "20260508T120000",
        "date_range": {"from": "2026-04-15", "until": "2026-05-08"},
        "summary": {
            "stages_ok": 2,
            "stages_failed": 0,
            "stages_skipped": 0,
            "total_duration_s": 12.5,
            "es_doc_count_in_range": 7,
            "es_latest_published": "2026-05-08",
        },
        "stages": [
            {
                "name": "fetch",
                "status": "ok",
                "duration_s": 2.5,
                "post_check": {"new_pdfs": 7},
            },
            {
                "name": "convert",
                "status": "ok",
                "duration_s": 10.0,
                "post_check": {"new_mds": 7},
            },
        ],
    }
    buf = io.StringIO()
    print_summary(report, stream=buf)
    out = buf.getvalue()
    assert "CATCH-UP REPORT" in out
    assert "2026-04-15" in out
    assert "fetch" in out
    assert "new_pdfs=7" in out


# ---- main() — already-caught-up gate -------------------------------------


def test_main_auto_detect_already_caught_up_returns_zero(
    tmp_path: Path, capsys, monkeypatch
):
    """When auto-detect produces a from > until range (DB latest_ok = today),
    main() should print the friendly message and exit 0 — not the
    cryptic --from > --until error which is reserved for explicit-args
    misuse.
    """
    db = tmp_path / "monitorul.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE days (date TEXT, status TEXT)")
        con.execute("INSERT INTO days VALUES (?, 'ok')", (date.today().isoformat(),))

    # Strip ES + embed env so the test doesn't try to probe real services.
    for key in ("ES_URL", "ES_API_KEY", "EMBED_URL"):
        monkeypatch.delenv(key, raising=False)

    rc = main(["--db", str(db), "--pdfs", str(tmp_path / "pdfs")])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Already caught up" in captured.err


def test_main_explicit_invalid_range_returns_two(tmp_path: Path, capsys):
    """When the user passes --from > --until explicitly, that's a usage
    error and main() exits 2 — they wanted a real run with bad inputs."""
    db = tmp_path / "monitorul.db"
    rc = main(
        [
            "--db",
            str(db),
            "--pdfs",
            str(tmp_path / "pdfs"),
            "--from",
            "2026-05-09",
            "--until",
            "2026-05-08",
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "--from" in captured.err and "--until" in captured.err


# ---- STAGES / STAGE_NAMES integrity --------------------------------------


def test_stages_have_required_keys():
    for s in STAGES:
        assert {"name", "subcommand", "pre", "post"} <= set(s.keys())


def test_stage_names_unique():
    assert len(set(STAGE_NAMES)) == len(STAGE_NAMES)


def test_stage_pre_post_methods_exist_on_runner(tmp_path: Path):
    """Every stage's `pre` / `post` keys must point at real methods on Runner."""
    r = _runner(tmp_path)
    for s in STAGES:
        assert callable(getattr(r, s["pre"], None)), f"missing {s['pre']}"
        assert callable(getattr(r, s["post"], None)), f"missing {s['post']}"


# ---- --include-mapping-bump ---------------------------------------------


def test_parser_accepts_include_mapping_bump():
    """The flag is plumbed through argparse and defaults to off."""
    from tools.catchup import _build_parser

    p = _build_parser()
    assert p.parse_args([]).include_mapping_bump is False
    assert p.parse_args(["--include-mapping-bump"]).include_mapping_bump is True


def test_runner_mapping_bump_pre_runs_es_init(tmp_path: Path, monkeypatch):
    """The pre-step shells out to `monitorul-ii es-init --update-mappings`
    and reports the StageResult so the report shows the bump ran.
    """
    import tools.catchup as catchup

    r = _runner(tmp_path, include_mapping_bump=True)
    captured: list[list[str]] = []

    class FakeProc:
        def __init__(self, returncode: int = 0) -> None:
            self.returncode = returncode

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return FakeProc(0)

    monkeypatch.setattr(catchup.subprocess, "run", fake_run)
    result = r._run_mapping_bump_pre()
    assert result.name == "mapping-bump-pre"
    assert result.status == "ok"
    assert captured == [["uv", "run", "monitorul-ii", "es-init", "--update-mappings"]]


def test_runner_mapping_bump_pre_skips_without_es_creds(tmp_path: Path, monkeypatch):
    """Same gate as the index stage: missing ES creds → skipped, not failed."""
    import tools.catchup as catchup

    r = _runner(tmp_path, include_mapping_bump=True, es_url=None, es_api_key=None)
    called: list[bool] = []

    def fake_run(cmd, **kwargs):
        called.append(True)
        raise AssertionError("subprocess should not run when ES creds missing")

    monkeypatch.setattr(catchup.subprocess, "run", fake_run)
    result = r._run_mapping_bump_pre()
    assert result.name == "mapping-bump-pre"
    assert result.status == "skipped"
    assert "ES_URL" in (result.reason or "")
    assert called == []


def test_runner_mapping_bump_pre_marks_fail_on_nonzero_exit(
    tmp_path: Path, monkeypatch
):
    import tools.catchup as catchup

    r = _runner(tmp_path, include_mapping_bump=True)

    class FakeProc:
        returncode = 7

    monkeypatch.setattr(catchup.subprocess, "run", lambda *a, **kw: FakeProc())
    result = r._run_mapping_bump_pre()
    assert result.status == "fail"
    assert "exit 7" in (result.reason or "")


def test_runner_mapping_bump_post_runs_index_force(tmp_path: Path, monkeypatch):
    """The post-step shells out to `monitorul-ii index <pdfs> --force -j N`
    so existing docs reproject through the new denormalizer.
    """
    import tools.catchup as catchup

    r = _runner(tmp_path, include_mapping_bump=True)
    captured: list[list[str]] = []

    class FakeProc:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return FakeProc()

    monkeypatch.setattr(catchup.subprocess, "run", fake_run)
    result = r._run_mapping_bump_post()
    assert result.name == "mapping-bump-post"
    assert result.status == "ok"
    assert captured == [
        [
            "uv",
            "run",
            "monitorul-ii",
            "index",
            str(r.pdfs_dir),
            "--force",
            "-j",
            str(r.workers),
        ]
    ]


def test_runner_mapping_bump_post_skips_without_es_creds(tmp_path: Path, monkeypatch):
    import tools.catchup as catchup

    r = _runner(tmp_path, include_mapping_bump=True, es_url=None, es_api_key=None)

    def fake_run(cmd, **kwargs):
        raise AssertionError("subprocess should not run when ES creds missing")

    monkeypatch.setattr(catchup.subprocess, "run", fake_run)
    result = r._run_mapping_bump_post()
    assert result.status == "skipped"
    assert "ES_URL" in (result.reason or "")


def test_runner_run_invokes_mapping_bump_pre_and_post(tmp_path: Path, monkeypatch):
    """End-to-end: when --include-mapping-bump is on, `Runner.run([])`
    appends a `mapping-bump-pre` StageResult before any pipeline stage
    and a `mapping-bump-post` after, in that order. The empty stages
    list keeps the test focused on the bump bracketing.
    """
    import tools.catchup as catchup

    r = _runner(tmp_path, include_mapping_bump=True)

    class FakeProc:
        returncode = 0

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return FakeProc()

    monkeypatch.setattr(catchup.subprocess, "run", fake_run)
    results = r.run([])
    names = [s.name for s in results]
    assert names == ["mapping-bump-pre", "mapping-bump-post"]
    # Subprocess sequence: es-init then index --force.
    assert captured[0][3] == "es-init"
    assert captured[1][3] == "index"
    assert "--force" in captured[1]


def test_runner_run_skips_post_when_pre_failed_and_no_continue_on_error(
    tmp_path: Path, monkeypatch
):
    """If the mapping bump pre-step fails AND --continue-on-error is off,
    the catch-up aborts immediately — running the index stage against a
    stale mapping would corrupt the new field types.
    """
    import tools.catchup as catchup

    r = _runner(tmp_path, include_mapping_bump=True, continue_on_error=False)

    class FakeProc:
        def __init__(self, code: int) -> None:
            self.returncode = code

    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[3])
        return FakeProc(7 if cmd[3] == "es-init" else 0)

    monkeypatch.setattr(catchup.subprocess, "run", fake_run)
    results = r.run([])
    assert [s.name for s in results] == ["mapping-bump-pre"]
    assert results[0].status == "fail"
    # Critical: index --force did NOT run when pre failed without
    # --continue-on-error.
    assert "index" not in calls


def test_runner_run_runs_post_when_pre_failed_and_continue_on_error(
    tmp_path: Path, monkeypatch
):
    """With --continue-on-error, even a failing pre doesn't block the
    post-step (mirrors the per-stage skip-on-fail semantics)."""
    import tools.catchup as catchup

    r = _runner(tmp_path, include_mapping_bump=True, continue_on_error=True)

    class FakeProc:
        def __init__(self, code: int) -> None:
            self.returncode = code

    def fake_run(cmd, **kwargs):
        return FakeProc(7 if cmd[3] == "es-init" else 0)

    monkeypatch.setattr(catchup.subprocess, "run", fake_run)
    results = r.run([])
    names = [s.name for s in results]
    assert names == ["mapping-bump-pre", "mapping-bump-post"]
    assert results[0].status == "fail"
    assert results[1].status == "ok"


def test_runner_run_no_bump_when_flag_off(tmp_path: Path, monkeypatch):
    """Sanity: without the flag, the pipeline runs as today — no extra
    subprocess invocations."""
    import tools.catchup as catchup

    r = _runner(tmp_path, include_mapping_bump=False)
    called: list[bool] = []

    def fake_run(cmd, **kwargs):
        called.append(True)
        raise AssertionError("subprocess should not run on empty stages w/o bump")

    monkeypatch.setattr(catchup.subprocess, "run", fake_run)
    results = r.run([])
    assert results == []
    assert called == []


# ---- --analyze-provider --------------------------------------------------


def test_parser_accepts_analyze_provider():
    """Both providers accepted; openrouter is the default."""
    from tools.catchup import _build_parser

    p = _build_parser()
    assert p.parse_args([]).analyze_provider == "openrouter"
    assert p.parse_args(["--analyze-provider", "google"]).analyze_provider == "google"
    assert (
        p.parse_args(["--analyze-provider", "openrouter"]).analyze_provider
        == "openrouter"
    )


def test_parser_rejects_unknown_analyze_provider():
    """argparse `choices=` enforces the enum at parse time."""
    from tools.catchup import _build_parser

    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["--analyze-provider", "anthropic"])


def test_runner_rejects_unsupported_analyze_provider(tmp_path: Path):
    """Defence-in-depth at the constructor: caller can't bypass argparse."""
    with pytest.raises(ValueError, match="unsupported analyze_provider"):
        _runner(tmp_path, analyze_provider="invalid")


def test_pre_analyze_checks_openrouter_key_by_default(tmp_path: Path, monkeypatch):
    """Default provider → OPENROUTER_API_KEY is the gate.
    GOOGLE_AI_STUDIO_API_KEY presence is irrelevant."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_AI_STUDIO_API_KEY", "google-key-set")

    r = _runner(tmp_path, analyze_provider="openrouter")
    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.extraction.json").write_text("{}")
    pre = r._pre_analyze()
    assert pre["ok"] is False
    assert pre["api_key_env"] == "OPENROUTER_API_KEY"
    assert pre["api_key_present"] is False
    assert pre["provider"] == "openrouter"

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key-set")
    pre = r._pre_analyze()
    assert pre["ok"] is True
    assert pre["api_key_present"] is True


def test_pre_analyze_checks_google_key_when_provider_google(
    tmp_path: Path, monkeypatch
):
    """provider=google → GOOGLE_AI_STUDIO_API_KEY is the gate.
    OPENROUTER_API_KEY presence is irrelevant."""
    monkeypatch.delenv("GOOGLE_AI_STUDIO_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key-set")

    r = _runner(tmp_path, analyze_provider="google")
    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.extraction.json").write_text("{}")
    pre = r._pre_analyze()
    assert pre["ok"] is False
    assert pre["api_key_env"] == "GOOGLE_AI_STUDIO_API_KEY"
    assert pre["api_key_present"] is False
    assert pre["provider"] == "google"

    monkeypatch.setenv("GOOGLE_AI_STUDIO_API_KEY", "google-key-set")
    pre = r._pre_analyze()
    assert pre["ok"] is True
    assert pre["api_key_present"] is True


def test_pre_analyze_skips_when_no_sidecars(tmp_path: Path, monkeypatch):
    """Provider gate doesn't override the empty-input gate."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "set")
    r = _runner(tmp_path, analyze_provider="openrouter")
    pre = r._pre_analyze()
    assert pre["ok"] is False
    assert pre["sidecars_in_range"] == 0


def test_stage_args_analyze_omits_provider_on_openrouter_default(tmp_path: Path):
    """Don't pollute the dominant-path command line with the redundant
    `--provider openrouter`. The analyze CLI defaults to openrouter
    on its own, so absence is correct."""
    r = _runner(tmp_path, analyze_provider="openrouter")
    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.extraction.json").write_text("{}")
    args = r._stage_args("analyze")
    assert "--provider" not in args
    assert "google" not in args
    # Sanity: the standard `-j N` is still appended.
    assert "-j" in args
    assert args[args.index("--max-words") + 1] == str(ANALYZE_MAX_WORDS)


def test_stage_args_analyze_includes_provider_on_google(tmp_path: Path):
    """provider=google → `--provider google` forwarded so the analyze
    subprocess routes through Google AI Studio instead of OpenRouter."""
    r = _runner(tmp_path, analyze_provider="google", workers=4)
    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.extraction.json").write_text("{}")
    args = r._stage_args("analyze")
    assert args[-2:] == ["--provider", "google"]
    # The -j workers flag is still appended too (just earlier in the args).
    assert "-j" in args
    assert "4" in args
    assert args[args.index("--max-words") + 1] == str(ANALYZE_MAX_WORDS)


def test_stage_args_provider_only_affects_analyze(tmp_path: Path):
    """Other stages must NOT carry the --provider forward (it's an
    analyze-only flag — would crash any other subcommand's argparse)."""
    r = _runner(tmp_path, analyze_provider="google")
    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.pdf").write_bytes(b"%PDF")
    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.md").write_text("body")
    (r.pdfs_dir / "2026-04-20_MO-PII-1-2026.extraction.json").write_text("{}")
    for stage in ("convert", "extract", "link", "backfill", "embed", "index"):
        args = r._stage_args(stage)
        assert "--provider" not in args, f"{stage} should not carry --provider"
        assert "--max-words" not in args, f"{stage} should not carry --max-words"


def test_run_preflight_lists_google_key_when_provider_google(
    tmp_path: Path, monkeypatch, capsys
):
    """Pre-flight env-var section must require GOOGLE_AI_STUDIO_API_KEY
    (not OPENROUTER_API_KEY) when the operator picks Google. Otherwise
    a Google-provider run with no OpenRouter key would loudly warn about
    a missing OPENROUTER_API_KEY that's irrelevant.
    """
    # Strip both keys + ES creds so we only test the analyze gate.
    for key in (
        "OPENROUTER_API_KEY",
        "GOOGLE_AI_STUDIO_API_KEY",
        "ES_URL",
        "ES_API_KEY",
        "EMBED_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    r = _runner(tmp_path, analyze_provider="google")
    pre = r.run_preflight()
    # Required keys include GOOGLE_AI_STUDIO_API_KEY (missing → flagged).
    # The non-selected OPENROUTER_API_KEY moves to optional, not missing.
    assert "GOOGLE_AI_STUDIO_API_KEY" in pre["env"]["missing"]
    assert "OPENROUTER_API_KEY" not in pre["env"]["missing"]


def test_build_report_carries_analyze_provider():
    """The report's cli_args section must round-trip the provider so a
    later post-mortem can see which backend coded the discourse."""
    report = build_report(
        date_from=date(2026, 5, 8),
        date_until=date(2026, 5, 8),
        results=[],
        cli_args={"analyze_provider": "google"},
    )
    assert report["cli_args"]["analyze_provider"] == "google"
