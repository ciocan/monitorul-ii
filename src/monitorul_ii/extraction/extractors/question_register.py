"""Extractor for `question_register` documents.

End-of-session lists of unanswered written questions, published as standalone
MO Partea II issues. The body shape is the schema's simplest: one flat list
of questions, each with addressee + questioner + registration metadata.

The MD layout is consistent across the 13-year corpus (verified on
2013-07-11 through 2026-03-25 samples):

    [shared boilerplate banner — handled by claim_shared_boilerplate]

    LISTA

    ÎNTREBĂRILOR ADRESATE DE CĂTRE …                          (qr-specific)

    ## **Domnului <Name>, <role>**                            (addressee)

    ## 1. **<Questioner>, deputat <PARTY>, Circumscripția…**  (question)

    [Obiectul/Subiectul/Întrebare privind …]                  (topic)

    [body text — multi-paragraph]

    _<Questioner>_, deputat <PARTY>                           (signature)

    Nr. <regnum>/<reg-date>                                   (reg block)

    ## 2. …                                                   (next q)

Addressee headers may repeat — every question is preceded by an addressee
header (ranking) directly above it; the addressee can change between
questions or stay the same. Ordinals are global per document (1, 2, 3 …),
not per addressee.

Source-span policy: each question record claims `[addressee_start,
next_question_or_eof)`. Including the addressee header in the question's
span means each addressee header appears in exactly one question's claim
(no double-counting). The qr-specific boilerplate (LISTA word + the long
ÎNTREBĂRILOR ADRESATE… sentence) is emitted as `claimed_by_policy`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from monitorul_ii.extraction.coverage import (
    Claim,
    line_offsets,
    make_boilerplate_claim,
    make_record_claim,
)
from monitorul_ii.extraction.speakers import parse_questioner

if TYPE_CHECKING:
    from monitorul_ii.extraction.pipeline import ExtractContext

EXTRACTOR_VERSION = "0.1.0"
EXTRACTOR_LABEL = f"regex@question_register@{EXTRACTOR_VERSION}"


# Question header — modern: "## 1. **Patricia-Simina-Arina Moș, deputat PNL, …**"
# Older 2012-era variant drops the `##` prefix: "1. **Sorin… senator progresist, …**"
# We require the bold inner content to contain ", deputat" or ", senator" so the
# pattern can't mis-fire on numbered list items in question bodies.
_QUESTION_HEADER_RE = re.compile(
    r"^(?:##\s+)?(?P<ord>\d+)\.\s+\*\*"
    r"(?P<inner>[^\n*]+?,\s*(?:deputat|senator)\b[^\n*]*?)"
    r"\*\*\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Addressee header — three observed forms:
#   1. Personal:        "## **Domnului Bogdan-Gruia Ivan, ministrul energiei**"
#   2. With rank:       "## **Domnului general Răzvan Ionescu, directorul …**"
#   3. Institutional:   "## **Curții de Conturi**" / "## **Băncii Naționale a României**"
# Negative lookahead `(?!\d)` excludes question headers from this match. The
# blacklist filter (`_ADDRESSEE_BLACKLIST_RE`) drops boilerplate lines that
# happen to share the `## **TEXT**` shape (DEZBATERI PARLAMENTARE, etc.).
_ADDRESSEE_HEADER_RE = re.compile(
    r"^##\s+\*\*\s*(?!\d)(?P<inner>[^\n*]+?)\s*\*\*\s*$",
    re.MULTILINE,
)
_ADDRESSEE_PERSONAL_RE = re.compile(
    r"^(?:Domnului|Doamnei|Domnișoarei|Domnișoarelor|Domnilor|Doamnelor)"
    r"\s+(?P<rest>.+)$",
    re.IGNORECASE,
)
# Boilerplate lines that share the `## **TEXT**` shape with addressee headers
# (older docs render banner headings at H2 instead of H1).
_ADDRESSEE_BLACKLIST_RE = re.compile(
    r"^\s*(?:DEZBATERI\s+PARLAMENTARE"
    r"|CAMERA\s+DEPUTA[ȚTÞ]ILOR|CAMERA\s+DEPUTATILOR|SENATUL"
    r"|L\s*I\s*S\s*T\s*A|LISTA"
    r"|PA\s*R\s*T\s*E\s*A[^\n]*?DEZBATERI"
    r"|nr\.[^\n]+|Circumscrip[țt]ia[^\n]*"
    r")\s*$",
    re.IGNORECASE,
)

# qr-specific boilerplate: standalone "LISTA" line. Some older docs spread it
# as `L I S T A` letter-by-letter.
_QR_LISTA_RE = re.compile(
    r"^L\s*I\s*S\s*T\s*A\s*$",
    re.MULTILINE,
)
# Same word inside a `## **L I S T A**` H2 wrapper (2012-era variant).
_QR_LISTA_H2_RE = re.compile(
    r"^##\s+\*\*\s*L\s*I\s*S\s*T\s*A\s*\*\*\s*$",
    re.MULTILINE,
)

# qr-specific boilerplate: the long "ÎNTREBĂRILOR ADRESATE DE CĂTRE …" sentence.
_QR_LIST_HEADER_RE = re.compile(
    r"^[ÎI]NTREB[ĂA]RILOR\s+ADRESATE\s+DE\s+C[ĂA]TRE[^\n]+$",
    re.MULTILINE,
)

# Topic line: variants observed in the corpus.
#   "Obiectul întrebării: …", "Obiectul: …", "Obiect: …"
#   "Subiectul întrebării: …", "Subiectul: …", "Subiect: …"
#   "Întrebare privind …"
_TOPIC_RE = re.compile(
    r"^(?:Obiectul\s+(?:întreb[ăa]rii|interpel[ăa]rii)|Obiectul|Obiect|"
    r"Subiectul\s+(?:întreb[ăa]rii|interpel[ăa]rii)|Subiectul|Subiect)"
    r"\s*:\s*(?P<topic>.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_TOPIC_PRIVIND_RE = re.compile(
    r"^[ÎI]ntrebare\s+privind\s+(?P<topic>.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Convert sometimes joins a topic line with the question's opening
# salutation (PDF→MD doesn't split them). Trim a trailing salutation so the
# `topic` field doesn't carry the next sentence.
_TOPIC_TRIM_RE = re.compile(
    r"\s+(?:Stimat[ăe]\s+(?:domn|doamn)"
    r"|Onorat[ăe]\s+(?:domn|doamn)"
    r"|Domnule\s+(?:ministru|președinte|director|deputat|guvernator|primar|secretar)"
    r"|Doamn[ăa]\s+(?:ministr|președint|director|deput|secretar)).*$",
    re.IGNORECASE | re.DOTALL,
)

# Registration block: "Nr. 2.110A/05.09.2025" — number then optional date.
# Number form: digits with optional thousands separator + trailing letter
# (e.g. "8A", "2.110A", "1.172B", "101A", "344A"). Date form: D.M.YYYY or
# DD.MM.YYYY (zero-padded both ways are accepted).
_REGNUM_RE = re.compile(
    r"\bNr\.\s*(?P<num>\d+(?:\.\d+)*[A-Za-z]?)"
    r"(?:\s*/\s*(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4}))?",
    re.IGNORECASE,
)

# Strip trailing rank prefix from addressee body
# ("Domnului general Răzvan Ionescu, …") so "name" is just the human name.
_RANK_PREFIX_RE = re.compile(
    r"^(?:general(?:-?(?:locotenent|maior|brigad[ăa]|colonel))?|"
    r"colonel|locotenent[- ]colonel|maior|c[ăa]pitan|"
    r"profesor(?:\s+universitar)?|prof\.|"
    r"academician|"
    r"doctor|dr\.|"
    r"inginer|ing\.|"
    r"economist)\s+",
    re.IGNORECASE,
)

# Best-effort: the role text often carries the parent-ministry verbatim;
# match that first, then fall back to deriving "Ministerul X" from the
# "ministrul X" genitive form.
_EXPLICIT_MINISTERUL_RE = re.compile(r"\bMinisterul\s+[^,]+", re.IGNORECASE)
_MINISTRUL_RE = re.compile(
    r"\bministr(?:ul|u|ei|a)\s+(?P<rest>.+?)(?:,|$)",
    re.IGNORECASE,
)
_GENITIVE_LOWERCASE = {
    "și",
    "si",
    "de",
    "la",
    "ale",
    "al",
    "a",
    "pentru",
    "cu",
    "în",
    "in",
    "din",
}


def _title_genitive(text: str) -> str:
    """Title-case Romanian ministry-name genitives, keeping conjunctions/
    prepositions lowercase ("Educației și Cercetării", not "Educației Și
    Cercetării").
    """
    words = text.split()
    out: list[str] = []
    for i, w in enumerate(words):
        if not w:
            continue
        if i > 0 and w.lower() in _GENITIVE_LOWERCASE:
            out.append(w.lower())
        else:
            out.append(w[0].upper() + w[1:] if w else w)
    return " ".join(out)


def _derive_ministry(role: str | None) -> str | None:
    if not role:
        return None
    m = _EXPLICIT_MINISTERUL_RE.search(role)
    if m:
        return m.group(0).strip()
    m = _MINISTRUL_RE.search(role)
    if m:
        rest = m.group("rest").strip()
        if rest:
            return f"Ministerul {_title_genitive(rest)}"
    return None


def _norm_or_none(s: str) -> str | None:
    s = " ".join(s.split())
    return s or None


def _parse_personal_addressee(rest: str) -> dict[str, Any]:
    """Parse the body after `Domnului|Doamnei|...` — strip a leading rank
    word, then split into (name, role) on the first comma.
    """
    body = rest.strip()
    body = _RANK_PREFIX_RE.sub("", body, count=1)
    if "," in body:
        name, role = body.split(",", 1)
        name_n = _norm_or_none(name)
        role_n = _norm_or_none(role)
    else:
        name_n = _norm_or_none(body)
        role_n = None
    return {
        "ministry": _derive_ministry(role_n),
        "ministry_normalized": None,
        "name": name_n,
        "role": role_n,
    }


def _parse_institutional_addressee(inner: str) -> dict[str, Any]:
    """Institutional form ("Curții de Conturi") — name is null; the
    institution text lives in `ministry` verbatim. Nominative-form
    normalisation is deferred to the institutions registry.
    """
    return {
        "ministry": _norm_or_none(inner),
        "ministry_normalized": None,
        "name": None,
        "role": None,
    }


def _parse_addressee(inner: str) -> dict[str, Any]:
    inner = inner.strip()
    m = _ADDRESSEE_PERSONAL_RE.match(inner)
    if m:
        return _parse_personal_addressee(m.group("rest"))
    return _parse_institutional_addressee(inner)


def _is_real_addressee(inner: str) -> bool:
    """Filter out boilerplate lines that happen to match `## **TEXT**` shape."""
    return _ADDRESSEE_BLACKLIST_RE.match(inner) is None


def _empty_addressee() -> dict[str, Any]:
    return {
        "ministry": None,
        "ministry_normalized": None,
        "name": None,
        "role": None,
    }


def _addressee_before(
    pos: int, addressees: list[tuple[int, int, str]]
) -> tuple[int, int, str] | None:
    last: tuple[int, int, str] | None = None
    for ah in addressees:
        if ah[0] >= pos:
            break
        last = ah
    return last


def _claim_qr_boilerplate(body: str, offsets: list[int]) -> list[Claim]:
    out: list[Claim] = []
    for m in _QR_LISTA_RE.finditer(body):
        out.append(
            make_boilerplate_claim(
                (m.start(), m.end()), "question_register.lista_word", offsets
            )
        )
    for m in _QR_LISTA_H2_RE.finditer(body):
        out.append(
            make_boilerplate_claim(
                (m.start(), m.end()),
                "question_register.lista_h2_wrapped",
                offsets,
            )
        )
    for m in _QR_LIST_HEADER_RE.finditer(body):
        out.append(
            make_boilerplate_claim(
                (m.start(), m.end()),
                "question_register.list_header_sentence",
                offsets,
            )
        )
    return out


def _parse_topic(text: str) -> str | None:
    m = _TOPIC_RE.search(text)
    if m:
        topic = _clean_topic(m.group("topic"))
        return topic or None
    m = _TOPIC_PRIVIND_RE.search(text)
    if m:
        topic = _clean_topic(m.group("topic"))
        return topic or None
    return None


def _clean_topic(topic: str) -> str:
    return _TOPIC_TRIM_RE.sub("", topic).strip()


def _parse_regblock(text: str) -> tuple[str | None, str | None]:
    """Return (registration_number, registration_date_iso).

    Last `Nr. …` block in the question text wins — questions occasionally
    include `Nr. crt.` or law citations earlier in the body, but the
    registration block (with date) is conventionally the last "Nr."
    occurrence before the next question header.
    """
    matches = list(_REGNUM_RE.finditer(text))
    if not matches:
        return None, None
    m = matches[-1]
    num = m.group("num")
    day, month, year = m.group("day"), m.group("month"), m.group("year")
    iso_date = None
    if day and month and year:
        try:
            iso_date = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        except ValueError:
            iso_date = None
    return num, iso_date


def _confidence_for(question: dict[str, Any]) -> float:
    """Simple heuristic — present-vs-null fields drive a 0.6 → 0.95 ramp.

    More sophisticated scoring lands when a calibration corpus exists.
    """
    score = 0.6
    if question["topic"]:
        score += 0.15
    if question["registration_number"]:
        score += 0.1
    if question["addressee"]["name"]:
        score += 0.05
    if question["questioner"]["name"]:
        score += 0.05
    return min(0.95, score)


def _frontmatter_chamber(fm: dict[str, Any]) -> str | None:
    v = fm.get("chamber")
    if isinstance(v, str) and v in ("Camera Deputaților", "Senatul"):
        return v
    return None


def _frontmatter_session(fm: dict[str, Any]) -> str | None:
    v = fm.get("session")
    if isinstance(v, str) and v:
        return v
    return None


def extract(
    ctx: "ExtractContext",
) -> tuple[dict[str, Any], list[Claim]]:
    """Extract a question_register MD into (body_dict, claims).

    `body_dict` matches `$defs/QuestionRegisterBody`; `claims` carries
    every span the extractor accounted for (record claims + qr-specific
    boilerplate). Shared MO boilerplate is added by the dispatcher.
    """
    body = ctx.body_text
    offsets = ctx.line_offsets

    qr_boilerplate = _claim_qr_boilerplate(body, offsets)

    addressee_hdrs: list[tuple[int, int, str]] = [
        (m.start(), m.end(), m.group("inner"))
        for m in _ADDRESSEE_HEADER_RE.finditer(body)
        if _is_real_addressee(m.group("inner"))
    ]
    question_hdrs = list(_QUESTION_HEADER_RE.finditer(body))

    questions: list[dict[str, Any]] = []
    record_claims: list[Claim] = []
    used_addressee_starts: set[int] = set()

    for i, qm in enumerate(question_hdrs):
        q_start = qm.start()
        # Span end: just before the next question header, or EOF
        if i + 1 < len(question_hdrs):
            next_q_start = question_hdrs[i + 1].start()
        else:
            next_q_start = len(body)

        ordinal = int(qm.group("ord"))
        questioner_inner = qm.group("inner")
        questioner = parse_questioner(questioner_inner)

        ah = _addressee_before(q_start, addressee_hdrs)
        # Question's source_span starts at the addressee header IFF the
        # addressee belongs to this question (i.e., is between the previous
        # question header and this one). If multiple questions share the
        # same earlier addressee, only the first one claims the addressee
        # header span; subsequent ones start at the question header.
        prev_q_end = question_hdrs[i - 1].end() if i > 0 else 0
        if (
            ah is not None
            and ah[0] >= prev_q_end
            and ah[0] not in used_addressee_starts
        ):
            span_start = ah[0]
            used_addressee_starts.add(ah[0])
            addressee = _parse_addressee(ah[2])
        elif ah is not None:
            span_start = q_start
            addressee = _parse_addressee(ah[2])
        else:
            span_start = q_start
            addressee = _empty_addressee()

        # Question text: everything between the question header end and the
        # next question / EOF. Trim leading/trailing whitespace; this is
        # the body the user actually wrote (topic + body + signature + reg
        # block).
        q_body_raw = body[qm.end() : next_q_start]
        q_body = q_body_raw.strip() or None

        topic = _parse_topic(q_body_raw) if q_body_raw else None
        regnum, regdate = _parse_regblock(q_body_raw)

        record = {
            "ordinal": ordinal,
            "addressee": addressee,
            "questioner": questioner,
            "registration_number": regnum,
            "registration_date": regdate,
            "topic": topic,
            "question_text": q_body,
            "source_span": ctx.make_source_span((span_start, next_q_start)),
            "extraction": {
                "extractor": EXTRACTOR_LABEL,
                "confidence": 0.0,  # filled below
                "source_span": ctx.make_source_span((span_start, next_q_start)),
            },
        }
        record["extraction"]["confidence"] = round(_confidence_for(record), 4)
        questions.append(record)
        record_claims.append(make_record_claim((span_start, next_q_start), offsets))

    body_dict: dict[str, Any] = {
        "session_label": _frontmatter_session(ctx.frontmatter),
        "chamber": _frontmatter_chamber(ctx.frontmatter),
        "questions": questions,
    }

    return body_dict, record_claims + qr_boilerplate


__all__ = ["EXTRACTOR_VERSION", "EXTRACTOR_LABEL", "extract"]


# Re-export `line_offsets` for tests that exercise the boilerplate helper
# without spinning up a full ExtractContext.
_line_offsets = line_offsets
