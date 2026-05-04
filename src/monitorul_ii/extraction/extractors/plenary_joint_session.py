"""Plenary joint session extractor — composition over plenary/.

Thin orchestrator: detects `chambers_present` and reuses the plenary sub-
extractors (session/agenda/activities/votes/interpellations).

Per Q12 design: composition over inheritance. The schema treats joint
session as a sibling document_type with a body shape that adds
`chambers_present` to the session envelope; everything else (agenda,
activities, votes, interpellations) is identical to plenary_stenogram.
"""

from __future__ import annotations

import re
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


EXTRACTOR_VERSION = "0.1.1"
EXTRACTOR_LABEL = f"regex@plenary_joint_session@{EXTRACTOR_VERSION}"


_JOINT_HEADER_RE = re.compile(
    r"[ȘS]EDIN[ȚT]E\s+COMUNE\s+ALE\s+CAMEREI\s+DEPUTA[ȚT]ILOR\s+[ȘS]I\s+SENATULUI",
    re.IGNORECASE,
)


def detect_chambers_present(body: str) -> list[str]:
    """Return ['Camera Deputaților', 'Senatul'] when the joint header
    appears; empty list otherwise."""
    if _JOINT_HEADER_RE.search(body):
        return ["Camera Deputaților", "Senatul"]
    return []


def extract(ctx: "ExtractContext") -> tuple[dict[str, Any], list[Claim]]:
    """Same orchestration as plenary_stenogram, with `chambers_present`
    augmentation on the session dict."""
    body = ctx.body_text
    interp_block = interpellations.find_interpellation_block(body)
    if interp_block is not None:
        agenda_end = interp_block[0]
    else:
        agenda_end = len(body)

    session_dict, session_claims = session.extract_session(body, ctx)
    session_dict["chambers_present"] = detect_chambers_present(body)
    agenda_items, agenda_claims = agenda.extract_agenda(body, agenda_end, ctx)
    if interp_block is not None:
        interp_list, interp_claims = interpellations.extract_interpellations(
            body, interp_block, ctx
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


__all__ = [
    "EXTRACTOR_VERSION",
    "EXTRACTOR_LABEL",
    "detect_chambers_present",
    "extract",
]
