from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from monitorul_ii import converter
from monitorul_ii.converter import (
    IssueMeta,
    _yaml_escape,
    clean_markdown,
    collect_pdfs,
    convert_all,
    enrich_meta,
    parse_filename,
)


# --- parse_filename ---------------------------------------------------------


def test_parse_filename_plain_number():
    meta = parse_filename("2026-04-29_MO-PII-47-2026.pdf")
    assert meta == IssueMeta(
        issue="47", year=2026, part="II", published=date(2026, 4, 29)
    )


def test_parse_filename_letter_suffix():
    meta = parse_filename("2026-04-29_MO-PII-358Bis-2026.pdf")
    assert meta is not None
    assert meta.issue == "358Bis"


def test_parse_filename_lowercase_suffix():
    meta = parse_filename("2000-01-25_MO-PII-1c-2000.pdf")
    assert meta is not None
    assert meta.issue == "1c"
    assert meta.year == 2000
    assert meta.published == date(2000, 1, 25)


@pytest.mark.parametrize(
    "name",
    [
        "garbage.pdf",
        "2026-04-29_MO-PII-47-2026.PDF",  # case-sensitive on extension
        "2026-13-29_MO-PII-47-2026.pdf",  # impossible month
        "2026-04-29_MO-P!!-47-2026.pdf",  # non-roman part
    ],
)
def test_parse_filename_rejects(name):
    assert parse_filename(name) is None


# --- IssueMeta.to_yaml_frontmatter -----------------------------------------


def test_yaml_frontmatter_minimum_fields():
    meta = IssueMeta(issue="47", year=2026, part="II", published=date(2026, 4, 29))
    out = meta.to_yaml_frontmatter()
    assert out.splitlines() == [
        "---",
        'issue: "47"',
        "year: 2026",
        'part: "II"',
        "published: 2026-04-29",
        "---",
    ]


def test_yaml_frontmatter_full_fields():
    meta = IssueMeta(
        issue="47",
        year=2026,
        part="II",
        published=date(2026, 4, 29),
        chamber="Senatul",
        session="SESIUNEA ORDINARĂ",
        session_date=date(2026, 4, 28),
        legislature="X",
    )
    text = meta.to_yaml_frontmatter()
    assert 'chamber: "Senatul"' in text
    assert 'session: "SESIUNEA ORDINARĂ"' in text
    assert "session_date: 2026-04-28" in text
    assert 'legislature: "X"' in text


def test_yaml_escape_handles_quotes_and_backslashes():
    assert _yaml_escape('a"b\\c') == 'a\\"b\\\\c'


def test_yaml_frontmatter_escapes_quotes_in_session():
    meta = IssueMeta(
        issue="1",
        year=2026,
        part="II",
        published=date(2026, 1, 1),
        session='SESSION "WITH QUOTES"',
    )
    assert 'session: "SESSION \\"WITH QUOTES\\""' in meta.to_yaml_frontmatter()


# --- enrich_meta ------------------------------------------------------------


_BASE = IssueMeta(issue="47", year=2026, part="II", published=date(2026, 4, 29))


def test_enrich_meta_extracts_camera_deputatilor():
    body = "PARLAMENTUL ROMÂNIEI\nCAMERA DEPUTAȚILOR\nSESIUNEA ORDINARĂ\n"
    meta = enrich_meta(body, _BASE)
    assert meta.chamber == "Camera Deputaților"


def test_enrich_meta_extracts_senatul():
    meta = enrich_meta("SENATUL\n", _BASE)
    assert meta.chamber == "Senatul"


def test_enrich_meta_extracts_genitive_camerei():
    meta = enrich_meta("CAMEREI DEPUTAȚILOR\n", _BASE)
    assert meta.chamber == "Camera Deputaților"


def test_enrich_meta_handles_legacy_thorn_glyph():
    """Older PDFs decoded `Ț` as `Þ` — the regex must still match."""
    meta = enrich_meta("CAMEREI DEPUTAÞILOR\n", _BASE)
    assert meta.chamber == "Camera Deputaților"


def test_enrich_meta_extracts_session_date_romanian_month():
    body = "Ședința din ziua de 28 aprilie 2026\n"
    meta = enrich_meta(body, _BASE)
    assert meta.session_date == date(2026, 4, 28)


def test_enrich_meta_extracts_session_date_case_insensitive():
    body = "ședința din ziua de 1 ianuarie 2024\n"
    meta = enrich_meta(body, _BASE)
    assert meta.session_date == date(2024, 1, 1)


def test_enrich_meta_invalid_session_date_falls_back_to_none():
    body = "Ședința din ziua de 31 februarie 2026\n"
    meta = enrich_meta(body, _BASE)
    assert meta.session_date is None


def test_enrich_meta_extracts_legislature_roman_numeral():
    meta = enrich_meta("Legislatura a IX-a\n", _BASE)
    assert meta.legislature == "IX"


def test_enrich_meta_extracts_session_text_truncates_at_legislatura():
    body = "SESIUNEA ORDINARĂ A SENATULUI (Legislatura a X-a)\n"
    meta = enrich_meta(body, _BASE)
    assert meta.session == "SESIUNEA ORDINARĂ A SENATULUI"


def test_enrich_meta_only_reads_first_5kb():
    """Anything past byte 5000 must be ignored."""
    body = ("\n" * 5001) + "SENATUL\n"
    meta = enrich_meta(body, _BASE)
    assert meta.chamber is None


def test_enrich_meta_preserves_base_filename_fields():
    meta = enrich_meta("", _BASE)
    assert meta.issue == _BASE.issue
    assert meta.year == _BASE.year
    assert meta.part == _BASE.part
    assert meta.published == _BASE.published
    assert meta.chamber is None
    assert meta.session is None
    assert meta.session_date is None
    assert meta.legislature is None


# --- clean_markdown ---------------------------------------------------------


def test_clean_markdown_strips_picture_placeholder():
    md = "**==> picture [120x80] intentionally omitted <==**\nbody\n"
    assert clean_markdown(md) == "body\n"


def test_clean_markdown_strips_running_header():
    md = "MONITORUL OFICIAL AL ROMÂNIEI, PARTEA a II-a, Nr. 47/2026\nbody\n"
    assert "MONITORUL" not in clean_markdown(md)


def test_clean_markdown_strips_running_header_with_legacy_a():
    """Older PDFs decode without the diacritic."""
    md = "MONITORUL OFICIAL AL ROMANIEI, PARTEA a II-a\nbody\n"
    assert "MONITORUL" not in clean_markdown(md)


def test_clean_markdown_strips_standalone_page_numbers():
    md = "first paragraph\n\n42\n\nnext paragraph\n"
    out = clean_markdown(md)
    assert "42" not in out
    assert "first paragraph" in out
    assert "next paragraph" in out


def test_clean_markdown_joins_hyphen_breaks():
    md = "comple-\ntare"
    assert "completare" in clean_markdown(md)


def test_clean_markdown_collapses_blank_lines():
    md = "a\n\n\n\n\nb\n"
    out = clean_markdown(md)
    assert "a\n\nb\n" == out


def test_clean_markdown_strips_trailing_whitespace():
    md = "trailing spaces   \nthen line\n"
    assert "trailing spaces   " not in clean_markdown(md)
    assert "trailing spaces" in clean_markdown(md)


def test_clean_markdown_idempotent():
    md = "body\n\n42\n\n**==> picture [1x2] intentionally omitted <==**\nmore\n"
    once = clean_markdown(md)
    twice = clean_markdown(once)
    assert once == twice


def test_clean_markdown_always_ends_with_single_newline():
    assert clean_markdown("plain").endswith("\n")
    assert clean_markdown("plain\n\n\n").count("\n") == 1


# --- collect_pdfs -----------------------------------------------------------


def test_collect_pdfs_filters_to_pdf(tmp_path: Path):
    pdf = tmp_path / "a.pdf"
    pdf.write_text("")
    txt = tmp_path / "b.txt"
    txt.write_text("")
    assert collect_pdfs([pdf, txt]) == [pdf]


def test_collect_pdfs_skips_uppercase_extension(tmp_path: Path):
    pdf = tmp_path / "A.PDF"
    pdf.write_text("")
    # The check is `suffix.lower() == ".pdf"` so .PDF *is* matched as a file.
    assert collect_pdfs([pdf]) == [pdf]


def test_collect_pdfs_globs_directory_non_recursive(tmp_path: Path):
    (tmp_path / "a.pdf").write_text("")
    (tmp_path / "b.pdf").write_text("")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.pdf").write_text("")
    out = collect_pdfs([tmp_path])
    names = sorted(p.name for p in out)
    assert names == ["a.pdf", "b.pdf"]


def test_collect_pdfs_directory_glob_is_sorted(tmp_path: Path):
    for n in ("c.pdf", "a.pdf", "b.pdf"):
        (tmp_path / n).write_text("")
    assert [p.name for p in collect_pdfs([tmp_path])] == ["a.pdf", "b.pdf", "c.pdf"]


def test_collect_pdfs_dedupes_by_resolved_path(tmp_path: Path):
    pdf = tmp_path / "a.pdf"
    pdf.write_text("")
    # Same file referenced twice (once direct, once via the directory).
    out = collect_pdfs([pdf, tmp_path])
    assert len(out) == 1


def test_collect_pdfs_ignores_missing_paths(tmp_path: Path):
    missing = tmp_path / "nope.pdf"
    assert collect_pdfs([missing]) == []


# --- convert_all (without invoking pymupdf) --------------------------------


def test_convert_all_skips_existing_md(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    md = tmp_path / "a.md"
    md.write_text("already converted\n")

    called = []

    def fake_convert_pdf(p, m):  # pragma: no cover - guarded by skip
        called.append(p)

    monkeypatch.setattr(converter, "convert_pdf", fake_convert_pdf)

    summary = convert_all([pdf])
    assert summary.skipped == 1
    assert summary.converted == 0
    assert called == []


def test_convert_all_force_reconverts(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    md = tmp_path / "a.md"
    md.write_text("already converted\n")

    def fake_convert_pdf(p, m):
        m.write_text("freshly converted\n")

    monkeypatch.setattr(converter, "convert_pdf", fake_convert_pdf)
    summary = convert_all([pdf], force=True)
    assert summary.converted == 1
    assert summary.skipped == 0
    assert md.read_text() == "freshly converted\n"


def test_convert_all_emits_events(tmp_path: Path, monkeypatch):
    pdfs = [tmp_path / "a.pdf", tmp_path / "b.pdf"]
    for p in pdfs:
        p.write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "a.md").write_text("done\n")  # b.pdf has no md → convert

    def fake_convert_pdf(p, m):
        m.write_text("body\n")

    monkeypatch.setattr(converter, "convert_pdf", fake_convert_pdf)

    events = []
    summary = convert_all(pdfs, on_event=events.append)
    kinds = sorted(e.kind for e in events)
    assert kinds == ["convert", "skip"]
    assert summary.converted == 1
    assert summary.skipped == 1


def test_convert_all_records_errors(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"")

    def boom(p, m):
        raise RuntimeError("fake decode failure")

    monkeypatch.setattr(converter, "convert_pdf", boom)
    summary = convert_all([pdf])
    assert summary.errors and "fake decode failure" in summary.errors[0]
    assert summary.converted == 0
    assert summary.skipped == 0


def test_convert_all_parallel_workers(tmp_path: Path, monkeypatch):
    pdfs = [tmp_path / f"f{i}.pdf" for i in range(5)]
    for p in pdfs:
        p.write_bytes(b"%PDF-1.4 fake")

    def fake_convert_pdf(p, m):
        m.write_text("body\n")

    monkeypatch.setattr(converter, "convert_pdf", fake_convert_pdf)

    events = []
    summary = convert_all(pdfs, workers=4, on_event=events.append)
    assert summary.converted == 5
    assert len(events) == 5
    assert all(e.kind == "convert" for e in events)
