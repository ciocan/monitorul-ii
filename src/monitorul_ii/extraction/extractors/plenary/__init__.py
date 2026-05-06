"""Plenary stenogram extractor — composes session/agenda/activities/votes/
interpellations sub-extractors into the body shape required by
`PlenaryStenogramBody` ($defs/plenary).

The actual `extract` orchestrator function lives at module level here so the
canonical pattern matches `extractors/question_register.py` (one `extract`
per per-type extractor).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from monitorul_ii.extraction.coverage import Claim
from monitorul_ii.extraction.extractors.plenary import (
    agenda,
    boilerplate,
    interpellations,
    session,
)

if TYPE_CHECKING:
    from monitorul_ii.extraction.pipeline import ExtractContext

EXTRACTOR_VERSION = "0.2.9"
EXTRACTOR_LABEL = f"regex@plenary_stenogram@{EXTRACTOR_VERSION}"


def _collect_chair_names(session_dict: dict[str, Any]) -> set[str]:
    """Union of chair[] + secretaries[] (+ chair_segments[*].chair) names."""
    names: set[str] = set()
    for c in session_dict.get("chair") or []:
        n = (c or {}).get("name")
        if n:
            names.add(n)
    for s in session_dict.get("secretaries") or []:
        n = (s or {}).get("name")
        if n:
            names.add(n)
    for seg in session_dict.get("chair_segments") or []:
        c = (seg or {}).get("chair") or {}
        n = c.get("name")
        if n:
            names.add(n)
    return names


def extract(ctx: "ExtractContext") -> tuple[dict[str, Any], list[Claim]]:
    """Run the plenary_stenogram pipeline → (body_dict, claims).

    Sub-extractor responsibility split:
      - session.extract_session: pre-first-speaker span (opened_at,
        chair_segments, attendance, format, ...) + body-suffix span
        (closed_at, outcome, special_procedure)
      - agenda.extract_agenda: SUMAR-driven agenda_items[] population +
        per-item activity walking via activities.extract_activities
      - interpellations.extract_interpellations: chair-transition-bounded
        sibling array

    Returned `claims` includes record claims for every emitted record
    plus boilerplate claims for plenary-specific MO-page content (SUMAR
    table, STENOGRAMA marker, etc.). Shared MO boilerplate is added by
    the dispatcher, not here.
    """
    body = ctx.body_text
    interp_block = interpellations.find_interpellation_block(body)
    if interp_block is not None:
        agenda_end = interp_block[0]
    else:
        agenda_end = len(body)

    session_dict, session_claims = session.extract_session(body, ctx)
    agenda_items, agenda_claims = agenda.extract_agenda(body, agenda_end, ctx)
    if interp_block is not None:
        chair_names = _collect_chair_names(session_dict)
        interp_list, interp_claims = interpellations.extract_interpellations(
            body, interp_block, ctx, chair_names=chair_names
        )
    else:
        interp_list, interp_claims = [], []

    plenary_bp_claims = boilerplate.claim_plenary_boilerplate(body)

    body_dict: dict[str, Any] = {
        "session": session_dict,
        "agenda_items": agenda_items,
        "interpellations": interp_list,
    }
    return body_dict, session_claims + agenda_claims + interp_claims + plenary_bp_claims


__all__ = ["EXTRACTOR_VERSION", "EXTRACTOR_LABEL", "extract"]
