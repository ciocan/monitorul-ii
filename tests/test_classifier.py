from __future__ import annotations

from pathlib import Path

import pytest

from monitorul_ii.classifier import (
    ClassifyResult,
    classify,
    classify_file,
    collect_mds,
    parse_issue_suffix,
)


# --- parse_issue_suffix ----------------------------------------------------


def test_parse_issue_suffix_plain():
    assert parse_issue_suffix("2026-04-29_MO-PII-47-2026.md") == ""


def test_parse_issue_suffix_committee():
    assert parse_issue_suffix("2025-05-27_MO-PII-3c-2025.md") == "c"


def test_parse_issue_suffix_report():
    assert parse_issue_suffix("2014-01-21_MO-PII-3R-2014.md") == "R"


def test_parse_issue_suffix_bis():
    assert parse_issue_suffix("2026-04-29_MO-PII-358Bis-2026.md") == "Bis"


def test_parse_issue_suffix_works_on_pdf_too():
    """Parser accepts both .md and .pdf so callers can use the same helper."""
    assert parse_issue_suffix("2025-05-27_MO-PII-3c-2025.pdf") == "c"


@pytest.mark.parametrize(
    "name",
    [
        "garbage.md",
        "garbage.txt",
        "2026-04-29_MO-PII-47-2026.MD",  # case-sensitive on extension
        "2026-04-29_MO-P!!-47-2026.md",  # non-roman part
    ],
)
def test_parse_issue_suffix_rejects_invalid(name):
    assert parse_issue_suffix(name) == ""


# --- classify (pure-function rules) -----------------------------------------


def test_classify_committee_by_suffix_alone():
    """`c` suffix is decisive even without any body marker."""
    r = classify("body without any markers", issue_suffix="c")
    assert r.top_type == "committee_synthesis"
    assert r.top_score == 1.0
    assert "issue_suffix=c" in r.matched_signals


def test_classify_report_by_suffix_alone():
    r = classify("body without any markers", issue_suffix="R")
    assert r.top_type == "report_facsimile"
    assert r.top_score == 1.0
    assert "issue_suffix=R" in r.matched_signals


def test_classify_plenary_by_stenograma_marker():
    body = "PARLAMENTUL ROMÂNIEI\nDEZBATERI PARLAMENTARE\n(STENOGRAMA)\n"
    r = classify(body, issue_suffix="")
    assert r.top_type == "plenary_stenogram"
    assert "body=STENOGRAMA" in r.matched_signals


def test_classify_plenary_dezbateri_alone_is_weaker():
    """DEZBATERI PARLAMENTARE alone is a weaker signal — the question_register
    docs share that header, so the classifier shouldn't lock in plenary."""
    r = classify("DEZBATERI PARLAMENTARE", issue_suffix="")
    # Still wins for plenary (nothing else matched), but at the lower 0.65 score.
    assert r.top_type == "plenary_stenogram"
    assert r.top_score == 0.65


def test_classify_question_register_overrides_dezbateri():
    body = "DEZBATERI PARLAMENTARE\n\nLISTA ÎNTREBĂRILOR ADRESATE DE DEPUTAȚI\n"
    r = classify(body, issue_suffix="")
    assert r.top_type == "question_register"
    # plenary_stenogram still gets its 0.65 from DEZBATERI — useful audit trail.
    assert r.all_scores["plenary_stenogram"] == 0.65
    assert r.all_scores["question_register"] == 0.95


def test_classify_lista_with_stenograma_falls_through_to_plenary():
    """If a stenogram embeds the LISTA marker (rare), the rule explicitly
    requires *no* (STENOGRAMA) for question_register to fire."""
    body = "(STENOGRAMA)\nLISTA ÎNTREBĂRILOR ADRESATE\n"
    r = classify(body, issue_suffix="")
    assert r.top_type == "plenary_stenogram"
    assert r.all_scores["question_register"] == 0.0


def test_classify_joint_session_marker():
    body = (
        "PARLAMENTUL ROMÂNIEI\n"
        "ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI SENATULUI\n"
        "(STENOGRAMA)\n"
    )
    r = classify(body, issue_suffix="")
    assert r.top_type == "plenary_joint_session"
    assert r.all_scores["plenary_stenogram"] == 0.85  # co-evidence preserved


def test_classify_committee_by_sinteza_body_marker():
    """When the `c` suffix is missing upstream but the body announces itself
    as a committee synthesis, the body marker carries the classification."""
    body = "Anul 185 (XXIV) — Nr. 19/C\nSINTEZA LUCRĂRILOR COMISIILOR\n"
    r = classify(body, issue_suffix="")
    assert r.top_type == "committee_synthesis"
    assert "body=SINTEZA_COMISIILOR" in r.matched_signals


def test_classify_report_by_rapoarte_marker_without_suffix():
    body = "(RAPOARTE DE ACTIVITATE)\n"
    r = classify(body, issue_suffix="")
    assert r.top_type == "report_facsimile"
    assert r.top_score == 0.9


def test_classify_other_when_no_marker_matches():
    r = classify("just some unrelated body text", issue_suffix="")
    assert r.top_type == "other"
    assert r.top_score == 0.0
    assert r.matched_signals == []


def test_classify_diacritic_tolerance_for_legacy_glyph():
    """Older PDFs decode `Ț` as `Þ`; the regex allows the corruption."""
    body = "ȘEDINȚE COMUNE ALE CAMEREI DEPUTAÞILOR ȘI SENATULUI"
    r = classify(body, issue_suffix="")
    assert r.top_type == "plenary_joint_session"


# --- ClassifyResult.is_ambiguous ------------------------------------------


def test_is_ambiguous_when_real_close_call():
    """Two unrelated types within the threshold → ambiguous."""
    r = ClassifyResult(
        top_type="plenary_stenogram",
        top_score=0.85,
        second_type="committee_synthesis",
        second_score=0.80,
        all_scores={},
        matched_signals=[],
    )
    assert r.is_ambiguous(threshold=0.2) is True


def test_is_ambiguous_compatible_runner_up_suppressed():
    """plenary_joint_session ⊃ plenary_stenogram — the runner-up is
    co-evidence, not an alternative classification, so don't flag."""
    r = ClassifyResult(
        top_type="plenary_joint_session",
        top_score=0.95,
        second_type="plenary_stenogram",
        second_score=0.85,
        all_scores={},
        matched_signals=[],
    )
    assert r.is_ambiguous(threshold=0.2) is False


def test_is_ambiguous_report_with_joint_session_runner_up_suppressed():
    """`R`-suffix reports get received in joint sessions; the joint marker
    isn't a competing classification."""
    r = ClassifyResult(
        top_type="report_facsimile",
        top_score=1.0,
        second_type="plenary_joint_session",
        second_score=0.95,
        all_scores={},
        matched_signals=[],
    )
    assert r.is_ambiguous(threshold=0.2) is False


def test_is_ambiguous_other_returns_false():
    """`other` is its own outlier signal; not 'ambiguous'."""
    r = ClassifyResult(
        top_type="other",
        top_score=0.0,
        second_type="other",
        second_score=0.0,
        all_scores={},
        matched_signals=[],
    )
    assert r.is_ambiguous() is False


# --- classify_file ----------------------------------------------------------


def test_classify_file_reads_window_and_classifies(tmp_path: Path):
    md = tmp_path / "2025-05-27_MO-PII-3c-2025.md"
    md.write_text("anything — suffix decides\n")
    r = classify_file(md)
    assert r.top_type == "committee_synthesis"
    assert r.top_score == 1.0


def test_classify_file_handles_corrupted_utf8(tmp_path: Path):
    """errors='replace' so a corrupted older PDF doesn't blow up the sweep."""
    md = tmp_path / "2014-01-21_MO-PII-3R-2014.md"
    # raw bytes containing an invalid UTF-8 sequence
    md.write_bytes(b"(RAPOARTE DE ACTIVITATE)\n\xff\xfe garbage \xc3\x28 here\n")
    r = classify_file(md)
    assert r.top_type == "report_facsimile"


# --- collect_mds ------------------------------------------------------------


def test_collect_mds_filters_to_md(tmp_path: Path):
    md = tmp_path / "a.md"
    md.write_text("")
    pdf = tmp_path / "b.pdf"
    pdf.write_text("")
    assert collect_mds([md, pdf]) == [md]


def test_collect_mds_globs_directory_non_recursive(tmp_path: Path):
    (tmp_path / "a.md").write_text("")
    (tmp_path / "b.md").write_text("")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("")
    out = collect_mds([tmp_path])
    assert sorted(p.name for p in out) == ["a.md", "b.md"]


def test_collect_mds_dedupes_by_resolved_path(tmp_path: Path):
    md = tmp_path / "a.md"
    md.write_text("")
    out = collect_mds([md, tmp_path])  # explicit + dir glob
    assert len(out) == 1


def test_collect_mds_reverse_flips_order(tmp_path: Path):
    for n in ("2026-04-01.md", "2026-04-02.md", "2026-04-03.md"):
        (tmp_path / n).write_text("")
    out = [p.name for p in collect_mds([tmp_path], reverse=True)]
    assert out == ["2026-04-03.md", "2026-04-02.md", "2026-04-01.md"]


# --- CLI parser wiring ------------------------------------------------------


def test_classify_parser_defaults():
    from monitorul_ii.cli import _build_parser

    args = _build_parser().parse_args(["classify", "pdfs/"])
    assert args.outliers is False
    assert args.ambiguity_threshold == pytest.approx(0.2)
    assert args.reverse is False


def test_classify_parser_outliers_flag():
    from monitorul_ii.cli import _build_parser

    args = _build_parser().parse_args(["classify", "pdfs/", "--outliers"])
    assert args.outliers is True


def test_classify_parser_threshold_override():
    from monitorul_ii.cli import _build_parser

    args = _build_parser().parse_args(
        ["classify", "pdfs/", "--ambiguity-threshold", "0.05"]
    )
    assert args.ambiguity_threshold == pytest.approx(0.05)


# --- cmd_classify end-to-end ------------------------------------------------


def test_cmd_classify_emits_jsonl(tmp_path: Path, capsys):
    """End-to-end: a tmp dir with one MD per type, run cmd_classify, parse stdout."""
    import json
    from argparse import Namespace

    from monitorul_ii.cli import cmd_classify

    (tmp_path / "2025-05-27_MO-PII-3c-2025.md").write_text("any body")
    (tmp_path / "2026-04-29_MO-PII-47-2026.md").write_text("(STENOGRAMA)")
    (tmp_path / "garbage.md").write_text("nothing matches here")

    args = Namespace(
        paths=[tmp_path],
        outliers=False,
        ambiguity_threshold=0.2,
        reverse=False,
    )
    rc = cmd_classify(args)
    assert rc == 0
    out = capsys.readouterr()
    rows = [json.loads(line) for line in out.out.splitlines() if line.strip()]
    types = {Path(r["file"]).name: r["top_type"] for r in rows}
    assert types["2025-05-27_MO-PII-3c-2025.md"] == "committee_synthesis"
    assert types["2026-04-29_MO-PII-47-2026.md"] == "plenary_stenogram"
    assert types["garbage.md"] == "other"


def test_cmd_classify_outliers_filters_to_other_only(tmp_path: Path, capsys):
    """--outliers should drop confidently-classified rows."""
    import json
    from argparse import Namespace

    from monitorul_ii.cli import cmd_classify

    (tmp_path / "2026-04-29_MO-PII-47-2026.md").write_text("(STENOGRAMA)")
    (tmp_path / "garbage.md").write_text("nothing matches here")

    args = Namespace(
        paths=[tmp_path],
        outliers=True,
        ambiguity_threshold=0.2,
        reverse=False,
    )
    cmd_classify(args)
    out = capsys.readouterr()
    rows = [json.loads(line) for line in out.out.splitlines() if line.strip()]
    assert len(rows) == 1
    assert Path(rows[0]["file"]).name == "garbage.md"
    assert rows[0]["top_type"] == "other"


def test_cmd_classify_handles_no_mds(tmp_path: Path, capsys):
    from argparse import Namespace

    from monitorul_ii.cli import cmd_classify

    args = Namespace(
        paths=[tmp_path],
        outliers=False,
        ambiguity_threshold=0.2,
        reverse=False,
    )
    rc = cmd_classify(args)
    assert rc == 0
    err = capsys.readouterr().err
    assert "no MDs found" in err
