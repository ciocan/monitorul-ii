"""Unit tests for the plenary session extractor."""

from __future__ import annotations

from datetime import date

from monitorul_ii.extraction.envelope import EnvelopeMeta
from monitorul_ii.extraction.extractors.plenary.session import (
    _detect_attendance,
    _detect_format,
    _detect_outcome,
    detect_special_procedure_from_header,
    extract_session,
    find_sumar_span,
)
from monitorul_ii.extraction.pipeline import ExtractContext


def _ctx(body: str, year: int = 2025) -> ExtractContext:
    from monitorul_ii.extraction.coverage import line_offsets

    meta = EnvelopeMeta(issue="1", year=year, part="II", published=date(year, 1, 1))
    return ExtractContext(
        body_text=body,
        line_offsets=line_offsets(body),
        content_sha="0123456789ab",
        meta=meta,
        frontmatter={
            "issue": "1",
            "year": year,
            "part": "II",
            "published": meta.published,
        },
    )


# -- opened_at / closed_at ---------------------------------------------------


def test_opened_at_with_colon_separator():
    body = "_Ședința a început la ora 16:30._\n## **Domnul X:**\n"
    sess, _ = extract_session(body, _ctx(body))
    assert sess["opened_at"] == "16:30"


def test_opened_at_with_dot_separator():
    """Some MDs render time as 16.19 instead of 16:19."""
    body = "_Ședința a început la ora 16.19._\n## **Domnul X:**\n"
    sess, _ = extract_session(body, _ctx(body))
    assert sess["opened_at"] == "16:19"


def test_opened_at_missing_returns_none():
    body = "## **Domnul X:**\nText\n"
    sess, _ = extract_session(body, _ctx(body))
    assert sess["opened_at"] is None


def test_closed_at_at_end():
    body = "## **Domnul X:**\n_Ședința s-a încheiat la ora 18:45._\n"
    sess, _ = extract_session(body, _ctx(body))
    assert sess["closed_at"] == "18:45"


# -- attendance -------------------------------------------------------------


def test_attendance_camera_only():
    body = (
        "din totalul celor 330 de deputați, până în acest moment, "
        "și-au înregistrat prezența 235."
    )
    att, _ = _detect_attendance(body)
    assert att == {"registered": 235, "total_seats": 330}


def test_attendance_joint_session():
    body = (
        "din totalul de 464 de deputați și senatori, "
        "și-au înregistrat prezența, până în acest moment, 296."
    )
    att, _ = _detect_attendance(body)
    assert att == {"registered": 296, "total_seats": 464}


def test_attendance_missing_returns_nulls():
    body = "no attendance phrase here"
    att, end = _detect_attendance(body)
    assert att == {"registered": None, "total_seats": None}
    assert end is None


# -- format ----------------------------------------------------------------


def test_format_mixed_explicit():
    assert (
        _detect_format(
            "Ședința se desfășoară prin mijloace electronice, în format mixt", 2025
        )
        == "mixed"
    )


def test_format_in_person_pre_2020_default():
    """Pre-2020 docs that lack a format marker default to in_person."""
    assert _detect_format("regular plenary text without markers", 2018) == "in_person"


def test_format_post_2020_no_marker_returns_none():
    assert _detect_format("regular plenary text without markers", 2024) is None


def test_format_online_explicit():
    assert _detect_format("doar online", 2021) == "online"


# -- outcome ---------------------------------------------------------------


def test_outcome_completed_explicit():
    assert _detect_outcome("Declar închisă ședința de astăzi.", None) == "completed"


def test_outcome_suspended_no_quorum():
    assert (
        _detect_outcome("Suspend ședința pentru lipsa cvorumului", None)
        == "suspended_no_quorum"
    )


def test_outcome_completed_default_when_closed_at_present():
    """closed_at set + no closing phrase → completed (implicit)."""
    assert _detect_outcome("plenary text", "18:00") == "completed"


def test_outcome_null_when_nothing_indicates_close():
    assert _detect_outcome("plenary text", None) is None


# -- special_procedure header detection ------------------------------------


def test_special_procedure_solemn_session():
    body = "Ședință solemnă comună consacrată aniversării zilei de 1 Decembrie"
    assert detect_special_procedure_from_header(body) == "sedinta_solemna"


def test_special_procedure_session_opening():
    body = "Deschiderea sesiunii ordinare a Senatului"
    assert detect_special_procedure_from_header(body) == "deschiderea_sesiunii"


def test_special_procedure_legislature_opening():
    body = "Deschiderea legislaturii a XII-a"
    assert detect_special_procedure_from_header(body) == "deschiderea_legislaturii"


def test_special_procedure_none_for_regular_session():
    assert detect_special_procedure_from_header("regular plenary text") is None


# -- SUMAR span -----------------------------------------------------------


def test_find_sumar_span_present():
    body = "header\nSUMAR\n|table content here|\n## **Domnul X:**\n"
    span = find_sumar_span(body)
    assert span is not None
    assert span[0] < span[1]


def test_find_sumar_span_absent():
    body = "no SUMAR here\n## **Domnul X:**\n"
    assert find_sumar_span(body) is None


# -- chair narrative -------------------------------------------------------


def test_extract_session_single_chair():
    body = (
        "_Ședința a început la ora 16:00._\n\n"
        "_Lucrările au fost conduse de domnul deputat Test Person, "
        "vicepreședinte al Camerei Deputaților, asistat de doamna deputat "
        "Helper One, secretar al Camerei Deputaților._\n\n"
        "## **Domnul Test Person:**\n"
        "Declar deschisă ședința și vă anunț că, din totalul de 330 de "
        "deputați, până în acest moment, și-au înregistrat prezența 200."
    )
    sess, _ = extract_session(body, _ctx(body))
    assert len(sess["chair"]) == 1
    assert sess["chair"][0]["name"] == "Test Person"
    assert len(sess["secretaries"]) == 1
    assert sess["secretaries"][0]["name"] == "Helper One"
    assert sess["attendance"]["registered"] == 200
    assert sess["attendance"]["total_seats"] == 330
    assert sess["quorum_met"] is True


def test_extract_session_multi_segment():
    body = (
        "_Lucrările au fost conduse, în prima parte, de domnul deputat "
        "Chair One, președinte al Camerei Deputaților, asistat de doamna "
        "deputat Sec One, secretar al Camerei Deputaților._\n\n"
        "_Ultima parte a ședinței a fost condusă de doamna deputat Chair Two, "
        "vicepreședinte al Camerei Deputaților, asistată de doamna deputat "
        "Sec Two, secretar al Camerei Deputaților._\n\n"
        "## **Domnul Chair One:**\nText.\n"
    )
    sess, _ = extract_session(body, _ctx(body))
    assert len(sess["chair_segments"]) >= 2
    chair_names = {c["name"] for c in sess["chair"]}
    assert "Chair One" in chair_names
    assert "Chair Two" in chair_names


def test_extract_session_no_chair_block():
    body = "## **Domnul X:**\nDirect speech without chair narrative.\n"
    sess, _ = extract_session(body, _ctx(body))
    assert sess["chair"] == []
    assert sess["chair_segments"] == []
    assert sess["secretaries"] == []
