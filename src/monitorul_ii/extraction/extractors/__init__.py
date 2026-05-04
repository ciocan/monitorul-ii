"""Per-document-type extractors.

`EXTRACTORS` maps a `DocumentType` literal (from `monitorul_ii.classifier`)
to the extractor function for that type. Types absent from the map are
skipped at the dispatcher with a `not-yet-implemented` reason — they don't
get a stub-extracted `body=other` sidecar (see Q1 from the design grilling).

Add a new extractor by:
  1. Authoring `extractors/<type>.py` with `extract(ctx) -> tuple[BodyDict, list[Claim]]`
     and an `EXTRACTOR_VERSION = "x.y.z"` constant.
  2. Registering it here.
  3. Tightening the corresponding $defs/<TypeBody> in extraction_schema.json
     from `additionalProperties: true` to the strict shape.
  4. Bumping schema_version.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from monitorul_ii.classifier import DocumentType
from monitorul_ii.extraction.extractors import (
    committee_synthesis,
    plenary,
    plenary_joint_session,
    question_register,
    report_facsimile,
)

if TYPE_CHECKING:
    from monitorul_ii.extraction.pipeline import ExtractContext

ExtractorFn = Callable[
    ["ExtractContext"],
    tuple[dict, list],  # (body_dict, claims)
]


EXTRACTORS: dict[DocumentType, ExtractorFn] = {
    "question_register": question_register.extract,
    "plenary_stenogram": plenary.extract,
    "plenary_joint_session": plenary_joint_session.extract,
    "committee_synthesis": committee_synthesis.extract,
    "report_facsimile": report_facsimile.extract,
}


EXTRACTOR_VERSIONS: dict[DocumentType, str] = {
    "question_register": question_register.EXTRACTOR_VERSION,
    "plenary_stenogram": plenary.EXTRACTOR_VERSION,
    "plenary_joint_session": plenary_joint_session.EXTRACTOR_VERSION,
    "committee_synthesis": committee_synthesis.EXTRACTOR_VERSION,
    "report_facsimile": report_facsimile.EXTRACTOR_VERSION,
}
