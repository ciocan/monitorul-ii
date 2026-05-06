"""Unit tests for the plenary vote detector."""

from __future__ import annotations

from monitorul_ii.extraction.extractors.plenary.votes import (
    detect_motion_type,
    detect_outcome_from_window,
    detect_voting_method,
    detect_votes,
    parse_result_window,
)


# -- motion type ----------------------------------------------------------


def test_motion_type_agenda_approval():
    assert (
        detect_motion_type("Supun votului ordinea de zi pentru ședința")
        == "agenda_approval"
    )


def test_motion_type_system_check():
    """v1.3.0 P3-1: pre-vote hardware tests."""
    assert (
        detect_motion_type("Vă rog să facem o verificare a sistemului de vot")
        == "system_check"
    )


def test_motion_type_final():
    assert detect_motion_type("Supunerea la votul final asupra") == "final"


def test_motion_type_amendment():
    assert detect_motion_type("Supun votului amendamentul depus") == "amendment"


def test_motion_type_item_adoption():
    assert detect_motion_type("Supun votului proiectul de hotărâre") == "item_adoption"


def test_motion_type_default_procedural():
    """No specific pattern → procedural fallback."""
    assert detect_motion_type("text without specific phrase") == "procedural"


# -- voting method --------------------------------------------------------


def test_voting_method_electronic():
    assert detect_voting_method("vot electronic") == "electronic"


def test_voting_method_electronic_remote():
    """v1.4.0: pandemic-era remote voting."""
    assert detect_voting_method("vot online") == "electronic_remote"


def test_voting_method_secret_ballot():
    assert (
        detect_voting_method("adoptat prin vot secret cu buletine de vot")
        == "secret_ballot"
    )


def test_voting_method_no_marker_returns_none():
    """Chair often doesn't restate method on routine votes."""
    assert detect_voting_method("regular vote announcement") is None


# -- result parsing -------------------------------------------------------


def test_parse_result_full_count_line():
    counts = parse_result_window("229 de voturi pentru, 3 voturi împotrivă, 9 abțineri")
    assert counts["for"] == 229
    assert counts["against"] == 3
    assert counts["abstain"] == 9


def test_parse_result_with_not_voting():
    counts = parse_result_window("320 voturi pentru, 5 abțineri, 3 nu votez")
    assert counts["for"] == 320
    assert counts["abstain"] == 5
    assert counts["not_voting"] == 3


def test_parse_result_unanimous_literal():
    counts = parse_result_window("Cu unanimitate de voturi")
    assert counts["for"] == "unanimous"


def test_parse_result_no_match_returns_none():
    assert parse_result_window("no result phrase here") is None


def test_parse_result_for_only():
    counts = parse_result_window("100 voturi pentru, ședința continuă")
    assert counts["for"] == 100
    # Other counts not stated — null
    assert counts["against"] is None


def test_parse_result_dotted_thousands():
    counts = parse_result_window("1.234 voturi pentru")
    # Romanian uses `.` as thousands separator
    assert counts["for"] == 1234


# -- outcome from window --------------------------------------------------


def test_outcome_explicit_approved():
    counts = parse_result_window("100 voturi pentru, 5 împotrivă")
    assert (
        detect_outcome_from_window(
            "Cu majoritate de voturi, proiectul a fost adoptat", counts
        )
        == "approved"
    )


def test_outcome_explicit_rejected():
    counts = parse_result_window("10 voturi pentru, 100 împotrivă")
    assert (
        detect_outcome_from_window("a fost respins cu majoritate", counts) == "rejected"
    )


def test_outcome_numeric_heuristic_rejected():
    counts = {
        "for": 10,
        "against": 100,
        "abstain": 0,
        "not_voting": None,
        "total_voting": None,
    }
    assert detect_outcome_from_window("plain text", counts) == "rejected"


def test_outcome_unanimous_approved():
    counts = {
        "for": "unanimous",
        "against": None,
        "abstain": None,
        "not_voting": None,
        "total_voting": None,
    }
    assert detect_outcome_from_window("plain text", counts) == "approved"


# -- end-to-end vote detection -------------------------------------------


def test_detect_votes_finds_open_and_result():
    span = (
        "Domnul X: Supun votului ordinea de zi.\n"
        "Să înceapă votul!\n"
        "229 de voturi pentru, 3 împotrivă, 9 abțineri. "
        "Cu majoritate de voturi, ordinea de zi a fost aprobată."
    )
    votes = detect_votes(span, span_start_offset=0)
    assert len(votes) >= 1
    s, e, data = votes[0]
    assert data["type"] == "vote"
    assert data["counts"]["for"] == 229
    assert data["outcome"] == "approved"
    assert data["motion_type"] == "agenda_approval"
    assert data["timing"] == "live"


def test_detect_votes_deferral_no_counts():
    span = "Domnul X: Supun votului proiectul. Aceasta rămâne pentru votul final."
    votes = detect_votes(span, span_start_offset=0)
    assert len(votes) == 1
    _, _, data = votes[0]
    assert data["outcome"] == "deferred"
    assert data["timing"] == "deferred"


def test_detect_votes_no_votes():
    span = "Domnul X: Vorbesc despre ceva, fără votare."
    assert detect_votes(span, span_start_offset=0) == []


def test_detect_votes_offsets_global():
    span = "Supun votului ordinea de zi.\nSă înceapă votul!\n100 voturi pentru."
    votes = detect_votes(span, span_start_offset=1000)
    s, e, data = votes[0]
    assert s >= 1000
    assert e > s
