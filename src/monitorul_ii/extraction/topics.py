"""Closed-set primary topic vocabulary.

15 canonical primary topics aligned with the standing committees of the
Romanian parliament (per `docs/extraction-schema.md` § 8 line 242). Title-
scoped detection only — body-scoped scoring would over-fire (a transport
debate mentioning `spital` in passing would tag Sănătate; the title IS the
topic).

Multi-match returns the full list; no-match returns `[]` (empty list is the
honest discovery-loop signal — defaulting to `"Other"` would dilute the
signal AND require adding "Other" to the canonical set, which is *not*
included by design).

Versioning: bumping `TOPICS_VERSION` invalidates plenary_stenogram +
plenary_joint_session + committee_synthesis sidecars (and qr too — qr lists
topics in `extractor_versions` even though it doesn't use them; trivial
re-extract cost per Q11 conservative-by-design contract).

Secondary topics (`secondary: [...]`) are filled by an LLM pass — out of
scope for v0.1; consumers see `secondary: []` until that work lands.
"""

from __future__ import annotations

import re

TOPICS_VERSION = "0.1.0"


PRIMARY_TOPICS: tuple[str, ...] = (
    "Educație",
    "Sănătate",
    "Apărare",
    "Economie",
    "Transporturi",
    "Afaceri europene",
    "Justiție",
    "Muncă",
    "Agricultură",
    "Mediu",
    "Cultură",
    "Buget-finanțe",
    "Administrație",
    "Politică externă",
    "Drepturile omului",
)


# Per-topic patterns: keyword pack + committee-name pack. Committee names
# from titles are the strongest single signal (per § 8). Patterns are
# minimal to start (1-3 per topic) — expanded as the discovery loop
# surfaces gaps. All patterns are case-insensitive and accept diacritic
# vs. ASCII equivalence (`ț`/`t`, `ă`/`a`, `ș`/`s`, `î`/`i`, `â`/`a`).
_TOPIC_RULES: dict[str, list[re.Pattern[str]]] = {
    "Educație": [
        re.compile(
            r"\b(?:educa[țt]i(?:e|onal[ăa]?)|(?:în|in)v[ăa][țt][ăa]m[âaî]nt(?:ul|ului)?|"
            r"[șs]coli(?:lor|i)?|universit[ăa][țt]i|elevi(?:lor)?|"
            r"studen[țt]i(?:lor)?|profesori(?:lor)?)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+pentru\s+(?:în|in)v[ăa][țt][ăa]m[âaî]nt", re.IGNORECASE
        ),
    ],
    "Sănătate": [
        re.compile(
            r"\b(?:s[ăa]n[ăa]tat(?:e|ii)|spital(?:e|ele|ului|elor)?|"
            r"medic(?:al[ăa]?|amente?|i(?:lor)?)|farmac(?:ie|euti)|"
            r"asisten[țt][ăa]\s+medical[ăa])\b",
            re.IGNORECASE,
        ),
        re.compile(r"\bComisia\s+pentru\s+s[ăa]n[ăa]tate\b", re.IGNORECASE),
    ],
    "Apărare": [
        re.compile(
            r"\b(?:ap[ăa]rare(?:a|ii)?|armat[ăa]|militar(?:[ăa]|i|e|elor)?|"
            r"NATO|sigura?n[țt][ăa]\s+na[țt]ional[ăa])\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+pentru\s+ap[ăa]rare(?:,\s+ordine\s+public[ăa])?",
            re.IGNORECASE,
        ),
        re.compile(r"\bMinisterul\s+Ap[ăa]r[ăa]rii\s+Na[țt]ionale\b", re.IGNORECASE),
    ],
    "Economie": [
        re.compile(
            r"\b(?:economie(?:i)?|economic[ăa]?|industri(?:e|ei|al[ăa])|"
            r"comer[țt](?:ul|ului)?|antreprenor|IMM|întreprinderi(?:lor)?)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+pentru\s+(?:industrii|economie|politic[ăa]\s+economic[ăa])\b",
            re.IGNORECASE,
        ),
    ],
    "Transporturi": [
        re.compile(
            r"\b(?:transport(?:uri(?:le|lor)?|ul|ului)?|"
            r"drumuri(?:le|lor)?|autostr[ăa]zi(?:le|lor)?|"
            r"feroviar[ăa]?|aerian[ăa]?|navigabil|c[ăa]i\s+ferate)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+pentru\s+transporturi(?:\s+[șs]i\s+infrastructur[ăa])?",
            re.IGNORECASE,
        ),
    ],
    "Afaceri europene": [
        re.compile(
            r"\b(?:afaceri(?:lor)?\s+europene|Uniunea\s+European[ăa]|UE|"
            r"(?:Comisi(?:a|ei|ile|ilor)|Consiliul|Parlamentul)\s+Europen[ăaeio]+|"
            r"directiv[ăa]\s+european[ăa])\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+(?:pentru\s+)?afaceri(?:lor)?\s+europene\b", re.IGNORECASE
        ),
        re.compile(
            r"\bComunic[ăa]ri[ie]?\s+(?:a\s+)?Comisiei\s+Europen", re.IGNORECASE
        ),
    ],
    "Justiție": [
        re.compile(
            r"\b(?:justi[țt]ie(?:i)?|judec[ăa]tor(?:i|ilor)?|procurori(?:lor)?|"
            r"penal[ăa]?|infrac[țt]iuni(?:lor)?|magistrat(?:i|ilor|ur[ăa])?)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+juridic[ăa](?:,\s+de\s+disciplin[ăa]\s+[șs]i\s+imunit[ăa][țt]i)?",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+pentru\s+(?:legisla[țt]ie|drepturile\s+omului)\b",
            re.IGNORECASE,
        ),
    ],
    "Muncă": [
        re.compile(
            r"\b(?:munc(?:[ăa]|ii|ito(?:r|ri))|salaria[țt]i(?:lor)?|"
            r"sindicat(?:e|elor)?|pensi(?:e|i|ilor)|[șs]omaj(?:ul|ului)?|"
            r"asisten[țt][ăa]\s+social[ăa])\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+pentru\s+munc[ăa](?:\s+[șs]i\s+protec[țt]ie\s+social[ăa])?",
            re.IGNORECASE,
        ),
    ],
    "Agricultură": [
        re.compile(
            r"\b(?:agricultur(?:[ăa]|ii)|fermier(?:i|ilor)?|crescatori(?:lor)?|"
            r"culturi(?:lor)?\s+agricole|zootehni[ce]|p[ăa]duri(?:lor)?\s+(?:[șs]i\s+)?dezvoltare)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+pentru\s+agricultur[ăa](?:,\s+silvicultur[ăa])?",
            re.IGNORECASE,
        ),
    ],
    "Mediu": [
        re.compile(
            r"\b(?:mediul(?:ui)?\s+înconjur[ăa]tor|protec[țt]ia\s+mediului|"
            r"polu[ăa]r(?:e|ii)|p[ăa]duri(?:lor)?|defri[șs][ăa]r(?:e|i)|"
            r"climatic[ăa]|biodiversitate)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+pentru\s+mediu(?:\s+[șs]i\s+echilibru\s+ecologic)?",
            re.IGNORECASE,
        ),
    ],
    "Cultură": [
        re.compile(
            r"\b(?:cultur(?:[ăa]|ii|al[ăa]?)|patrimoniu(?:lui)?|"
            r"art(?:[ăa]|ist(?:ic[ăa]?|i))|monumente(?:lor)?\s+istorice)\b",
            re.IGNORECASE,
        ),
        re.compile(r"\bComisia\s+pentru\s+cultur[ăa](?:,\s+arte)?", re.IGNORECASE),
    ],
    "Buget-finanțe": [
        re.compile(
            r"\b(?:buget(?:ul|ului|are)?|finan[țt][ăa]ri?|fiscal[ăa]?|"
            r"impozit(?:e|are)|TVA|taxa(?:elor)?|deficit(?:ul|ului)?|"
            r"datorie\s+public[ăa]|trezorerie)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+pentru\s+buget(?:,\s+finan[țt]e\s+[șs]i\s+b[ăa]nci)?",
            re.IGNORECASE,
        ),
        re.compile(r"\bCodul\s+fiscal\b", re.IGNORECASE),
    ],
    "Administrație": [
        re.compile(
            r"\b(?:administra[țt]ie\s+public[ăa]?|autorit[ăa][țt]i\s+locale|"
            r"prim[ăa]ri(?:e|ile|ilor)|consilii\s+locale|consilii\s+jude[țt]ene)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+pentru\s+administra[țt]ie\s+public[ăa]"
            r"(?:,\s+amenajarea\s+teritoriului)?",
            re.IGNORECASE,
        ),
    ],
    "Politică externă": [
        re.compile(
            r"\b(?:politic[ăa]\s+extern[ăa]|diploma[țt]i(?:e|lor|ic[ăa])|"
            r"ambasad(?:[ăa]|ei|elor)|tratat(?:e|ul|elor)\s+interna[țt]ional|"
            r"rela[țt]ii\s+(?:bilaterale|interna[țt]ionale))\b",
            re.IGNORECASE,
        ),
        re.compile(r"\bComisia\s+pentru\s+politic[ăa]\s+extern[ăa]", re.IGNORECASE),
    ],
    "Drepturile omului": [
        re.compile(
            r"\b(?:drepturile?\s+omului|libert[ăa][țt]i\s+fundamentale|"
            r"egalitatea?\s+de\s+[șs]anse|nediscriminare|minorit[ăa][țt]i)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bComisia\s+pentru\s+drepturile\s+omului"
            r"(?:,\s+culte(?:\s+[șs]i\s+problemele\s+minorit[ăa][țt]ilor)?)?",
            re.IGNORECASE,
        ),
    ],
}


def detect_primary_topics(agenda_title: str) -> list[str]:
    """Return the list of primary topics matching the agenda title.

    Multi-match returns multiple; no match returns []. Order follows the
    canonical `PRIMARY_TOPICS` tuple (deterministic).
    """
    if not agenda_title:
        return []
    return [
        topic
        for topic in PRIMARY_TOPICS
        if any(p.search(agenda_title) for p in _TOPIC_RULES[topic])
    ]


def make_topics(agenda_title: str) -> dict[str, list[str]]:
    """Build the canonical `topics` dict for an agenda item.

    `secondary` is empty in v0.1; the LLM pass that fills it lands later.
    """
    return {
        "primary": detect_primary_topics(agenda_title),
        "secondary": [],
    }


__all__ = [
    "TOPICS_VERSION",
    "PRIMARY_TOPICS",
    "detect_primary_topics",
    "make_topics",
]
