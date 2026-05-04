"""Type detector for converted MDs.

Step 1 of the extraction pipeline (see `docs/extraction-schema.md` Build order).
Classifies each MD into one of six buckets using cheap regex over the filename
suffix and the first ~10 KB of body text. Pure functions; no I/O outside
`classify_file` (which just reads the file).

The detection rules are lifted directly from the schema doc:

  - issue suffix `c`              → committee_synthesis
  - issue suffix `R`              → report_facsimile
  - body has `LISTA ÎNTREBĂRILOR ADRESATE` AND lacks `(STENOGRAMA)`
                                  → question_register
  - body has `ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI SENATULUI`
                                  → plenary_joint_session
  - body has `(RAPOARTE DE ACTIVITATE)`
                                  → report_facsimile (rare; suffix usually wins)
  - body has `(STENOGRAMA)` or `DEZBATERI PARLAMENTARE`
                                  → plenary_stenogram
  - else                          → other
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DocumentType = Literal[
    "plenary_stenogram",
    "plenary_joint_session",
    "committee_synthesis",
    "report_facsimile",
    "question_register",
    "other",
]

TYPED_DOCUMENT_TYPES: tuple[DocumentType, ...] = (
    "plenary_stenogram",
    "plenary_joint_session",
    "committee_synthesis",
    "report_facsimile",
    "question_register",
)

# Read this much of each MD body for header detection. Markers always sit in
# the front matter / first heading block; reading more burns I/O for no win.
HEADER_WINDOW_BYTES = 10_000


# Filename → issue-number suffix.
# Reuses the same shape as `converter._FILENAME_RE`; we accept either .md or .pdf
# so `parse_issue_suffix` works on either side of the convert step.
_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_MO-P(?P<part>[IVXM]+)-"
    r"(?P<num>[0-9A-Za-z]+)-(?P<year>\d{4})\.(?:md|pdf)$"
)
# Split a `<digits><letters>` issue number into its two parts.
_ISSUE_NUMBER_RE = re.compile(r"^(?P<digits>\d+)(?P<suffix>[A-Za-z]*)$")


# Body markers. Diacritic-tolerant (Ț ↔ T ↔ Þ for older PDFs); whitespace-loose
# so multi-line / extra-space layouts still match.
_RE_STENOGRAMA = re.compile(r"\(\s*STENOGRAMA\s*\)", re.IGNORECASE)
_RE_DEZBATERI_PARLAMENTARE = re.compile(r"DEZBATERI\s+PARLAMENTARE", re.IGNORECASE)
_RE_SEDINTE_COMUNE = re.compile(
    r"[ȘS]EDIN[ȚTÞ]E\s+COMUNE\s+ALE\s+CAMEREI\s+DEPUTA[ȚTÞ]ILOR"
    r"\s+[ȘS]I\s+SENATULUI",
    re.IGNORECASE,
)
_RE_RAPOARTE_DE_ACTIVITATE = re.compile(
    r"\(\s*RAPOARTE\s+DE\s+ACTIVITATE\s*\)", re.IGNORECASE
)
_RE_LISTA_INTREBARILOR = re.compile(
    r"LISTA\s+[ÎI]NTREB[ĂA]RILOR\s+ADRESATE", re.IGNORECASE
)
_RE_SINTEZA_COMISIILOR = re.compile(r"SINTEZA\s+LUCR[ĂA]RILOR\s+COMISI", re.IGNORECASE)


# Pairs where the runner-up is structurally implied by the winner — not
# ambiguous in the bad sense. A joint session is a kind of plenary stenogram;
# an `R`-suffix report is typically received in a joint or single-chamber
# session whose markers will fire as evidence. Suppressing these keeps
# `--outliers` focused on docs that need human review.
_COMPATIBLE_RUNNERS_UP: dict[str, frozenset[str]] = {
    "plenary_joint_session": frozenset({"plenary_stenogram"}),
    "report_facsimile": frozenset({"plenary_joint_session", "plenary_stenogram"}),
}


@dataclass(frozen=True)
class ClassifyResult:
    top_type: DocumentType
    top_score: float
    second_type: DocumentType
    second_score: float
    all_scores: dict[str, float] = field(default_factory=dict)
    matched_signals: list[str] = field(default_factory=list)

    def is_ambiguous(self, threshold: float = 0.2) -> bool:
        """True when top and runner-up are within `threshold` — worth eyeballing.

        `other` is excluded (handled separately by the outliers filter).
        Structurally-implied runners-up (joint_session ⊃ stenogram, etc.) are
        also excluded via `_COMPATIBLE_RUNNERS_UP` — those are co-evidence,
        not classification doubt.
        """
        if self.top_score == 0.0:
            return False
        if self.second_type in _COMPATIBLE_RUNNERS_UP.get(self.top_type, frozenset()):
            return False
        return (self.top_score - self.second_score) < threshold


def parse_issue_suffix(filename: str) -> str:
    """Extract trailing letters from the issue number in an MO filename.

    Examples:
        "2026-04-29_MO-PII-47-2026.md"   → ""
        "2026-04-29_MO-PII-12c-2026.md"  → "c"
        "2014-01-21_MO-PII-3R-2014.md"   → "R"
        "2026-04-29_MO-PII-358Bis-2026.md" → "Bis"

    Returns "" for filenames that don't match the canonical shape.
    """
    m = _FILENAME_RE.match(filename)
    if not m:
        return ""
    sm = _ISSUE_NUMBER_RE.match(m.group("num"))
    return sm.group("suffix") if sm else ""


def classify(text: str, issue_suffix: str = "") -> ClassifyResult:
    """Classify a document by the first ~10 KB of body text + the issue suffix.

    Pure function. `text` should be the front of the MD body (the typical
    `HEADER_WINDOW_BYTES` slice). `issue_suffix` is the trailing letters of the
    issue number (`c`, `R`, `Bis`, `""`).
    """
    scores: dict[str, float] = dict.fromkeys(TYPED_DOCUMENT_TYPES, 0.0)
    signals: list[str] = []

    # Filename-suffix evidence (decisive when present).
    if issue_suffix.lower() == "c":
        scores["committee_synthesis"] = 1.0
        signals.append("issue_suffix=c")
    elif issue_suffix.upper() == "R":
        scores["report_facsimile"] = 1.0
        signals.append("issue_suffix=R")

    # Body-marker evidence (always evaluated so ambiguity surfaces).
    has_stenograma = bool(_RE_STENOGRAMA.search(text))
    has_dezbateri = bool(_RE_DEZBATERI_PARLAMENTARE.search(text))
    has_sedinte_comune = bool(_RE_SEDINTE_COMUNE.search(text))
    has_rapoarte = bool(_RE_RAPOARTE_DE_ACTIVITATE.search(text))
    has_lista = bool(_RE_LISTA_INTREBARILOR.search(text))
    has_sinteza = bool(_RE_SINTEZA_COMISIILOR.search(text))

    if has_lista and not has_stenograma:
        scores["question_register"] = max(scores["question_register"], 0.95)
        signals.append("body=LISTA_INTREBARILOR")

    if has_sedinte_comune:
        scores["plenary_joint_session"] = max(scores["plenary_joint_session"], 0.95)
        signals.append("body=SEDINTE_COMUNE")

    if has_rapoarte:
        scores["report_facsimile"] = max(scores["report_facsimile"], 0.9)
        signals.append("body=RAPOARTE_DE_ACTIVITATE")

    if has_sinteza:
        # Catches committee syntheses whose filename suffix went missing
        # upstream (e.g., `2017-06-27_MO-PII-19-2017.md` is published as
        # `Nr. 19/C` in body but routed under `19` in the URL).
        scores["committee_synthesis"] = max(scores["committee_synthesis"], 0.9)
        signals.append("body=SINTEZA_COMISIILOR")

    if has_stenograma:
        scores["plenary_stenogram"] = max(scores["plenary_stenogram"], 0.85)
        signals.append("body=STENOGRAMA")
    if has_dezbateri:
        # On its own (no STENOGRAMA, no SEDINTE_COMUNE), DEZBATERI PARLAMENTARE
        # is a weaker signal — the question_register docs share that header.
        scores["plenary_stenogram"] = max(scores["plenary_stenogram"], 0.65)
        signals.append("body=DEZBATERI_PARLAMENTARE")

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_type, top_score = ranked[0]
    second_type, second_score = ranked[1]

    if top_score == 0.0:
        return ClassifyResult(
            top_type="other",
            top_score=0.0,
            second_type="other",
            second_score=0.0,
            all_scores=scores,
            matched_signals=signals,
        )

    return ClassifyResult(
        top_type=top_type,
        top_score=top_score,
        second_type=second_type,
        second_score=second_score,
        all_scores=scores,
        matched_signals=signals,
    )


def classify_file(md_path: Path) -> ClassifyResult:
    """Read the front of `md_path` and run `classify`.

    Reads at most `HEADER_WINDOW_BYTES` bytes (errors='replace' so corrupted
    UTF-8 in older PDFs doesn't blow up the sweep — those land in `other`).
    """
    with md_path.open("r", encoding="utf-8", errors="replace") as f:
        text = f.read(HEADER_WINDOW_BYTES)
    suffix = parse_issue_suffix(md_path.name)
    return classify(text, suffix)


def collect_mds(paths: list[Path], *, reverse: bool = False) -> list[Path]:
    """Resolve a list of file/dir paths into a sorted list of MD files.

    Mirrors `converter.collect_pdfs` but for `*.md`. Directories are non-
    recursive; outputs are deduped by resolved path. With `reverse=True` the
    final list is reversed (newest→oldest given the date-prefixed filenames).
    """
    out: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix.lower() == ".md":
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(p.glob("*.md")))
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        deduped.append(p)
    if reverse:
        deduped.reverse()
    return deduped
