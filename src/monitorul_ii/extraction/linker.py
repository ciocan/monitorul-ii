"""Cross-document linker pass.

Populates back-link fields that need awareness of OTHER sidecars, beyond
what the per-type extractors can fill at single-doc extract time.

v0.1 — report→session pass. Fills
  `report_facsimile.body.report.received_at.received_in_document` — the
  `mo://YYYY/PART/ISSUE` document_id of the joint-session (or single-
  chamber) stenogram that received the report.

v0.2 — vote-pair pass. Fills
  `agenda_items[].activities[].defers_to` (forward link) on votes whose
  outcome is `deferred`, and `agenda_items[].activities[].resolves` (back
  link) on resolving votes in subsequent docs. The forward link points
  at the document_id of the future stenogram with the resolving vote;
  the back-link is an array because a single resolving vote can resolve
  multiple prior deferrals (the chair often batches several deferred
  bills into one final-vote round).

Workflow: extract first (writes `received_in_document: null`,
`defers_to: null`, `resolves: []` for plenary sidecars), then run the
linker over the same directory. The linker walks `*.extraction.json`
twice — once for the report→session pass, once for the vote-pair pass —
and updates each sidecar in place. Atomic write via `.part` rename;
pre-write schema validation; idempotent (skip when already linked).

# Vote-pair matching algorithm (v0.2.0)

The hard problem is "did vote X in doc N resolve in vote Y of doc M?".
The matching key is derived per-vote from the parent agenda item, in
this order of preference:

  1. **Bill cite** — first entry in `agenda_items[].primary_references[]`
     of `type=bill`. Format: `f"bill:{number}/{year}"`. PL-x / L is the
     canonical bill identifier in Romanian parliamentary procedure; the
     same legislative initiative keeps the same cite from first reading
     through final vote, so two docs discussing the same bill always
     share the same PL-x.
  2. **Motion title hash** — for motion-class votes (when the agenda
     item's primary_references include a `motion` ref WITH a quoted
     title at least 12 chars long), use `f"motion:{motion_kind}:{title-hash}"`.
     Bare `Moțiunea simplă` without a quoted title is too generic and
     stays unlinked.

There is intentionally **no fallback** to law / oug / og /
parliamentary_resolution / chamber_resolution cites, nor to agenda
title hashes. Two iterations of v0.2.0 widened the key family to those
types and surfaced massive false-positive pair populations on the
5,551-doc corpus:

- agenda-title-hash collided generic procedural items (`Ședința`,
  `Aprobarea ordinii de zi`, `Informare cu privire la inițiativele
  legislative`) whose titles repeat verbatim every session.
- `law:N/Y` keys collided unrelated bills that all amend the same
  underlying law, plus recurring procedural notes — `law:47/1992`
  (Constitutional Court procedural law) collided every "Notă pentru
  exercitarea de către senatori a dreptului de sesizare a Curții
  Constituționale" across decades; `law:286/2009` (Criminal Code) and
  `law:95/2006` (Health Code) collided independent amendment debates.
- `oug:N/Y` cites are similarly shared by multiple bills approving
  the same OUG.

Bill-only keying is the precision floor. The linker's job is to be
precise, not aggressive — votes that don't carry a `bill` ref stay
unlinked. The cost is a coverage gap on procedural-item cross-doc
deferrals, but those are rare in practice while the false-positive
rate of looser keys was unacceptable.

**Window**: a deferred vote in doc N at session date D only matches
candidate resolvers in docs M with D < session_date(M) ≤ D + 60 days.
Most parliamentary deferrals resolve within 1–2 weeks; beyond 60 days
the same key is more likely a different debate cycle.

**Earliest-resolver-wins**: candidate resolvers are sorted by session
date ascending; the first one with `outcome ≠ deferred` (i.e., a vote
that actually resolved one way or another) locks the pair. Once a
deferred vote is matched, it doesn't re-match against later candidates.

**Multi-deferral chain**: if vote A in doc N1 defers to B in N2, and B
itself defers (still got `outcome=deferred` in N2) to C in N3, the
chain is `A.defers_to=N2, B.defers_to=N3, C.resolves=[N1, N2]` (the
back-link reverses for transitivity — every prior deferral that
ultimately landed on C). This means `resolves` is computed in a second
pass after the per-vote forward links are stable.

**Idempotency.** Same contract as v0.1: skip when both ends are already
populated; `force=True` re-links populated entries.

**Versioning.** The linker version is **not** part of `extractor_versions`
(Q11 conservative-by-design — exact-match semantics would force
extractor re-runs whenever the linker bumped). Linker output lives
entirely inside body content; if a future extract re-run clobbers it
(extractor version bumped → re-extract), the user re-runs `link` to
recover. Linking is fast (a single dict lookup per vote).

# Known issues / caveats (v0.2.0 production smoke)

Read before iterating on the linker — these are NOT linker bugs but
they shape what the output means.

1. **Agenda-extractor misattribution drives ~40% spot-check false
   positives.** The deferring agenda's title sometimes doesn't match
   the bill cite in its `primary_references[]` — `agenda.py`'s
   collection occasionally absorbs a bill mention from a neighbouring
   item when the boundary detection misfires. The linker correctly
   pairs by key; the false positive is upstream. Fix priority is on
   `agenda.py`, not on the linker's match key.
2. **Multi-chain ratio is ~73%** (11 of 15 forward links). Chains run
   3-4 levels deep on average; some may be truncated by the 60-day
   window. Worth instrumenting per-chain depth in a follow-up.
3. **Same-agenda multi-amendment votes inflate forward-link counts.**
   Multiple deferred amendment votes in one agenda each get their own
   forward link, even though they collectively represent one cross-doc
   relation. Consider de-duping per `(deferring_doc_id, agenda_index)`
   if the headline metric matters.
4. **Idempotency was unit-tested but not corpus-smoked.** A future
   regression should run `monitorul-ii link` twice in a row on the
   corpus and assert all-skip on the second pass.

Downstream consumers querying `defers_to` / `resolves` should treat the
values as **candidate links, not verified** — see
`docs/architecture.md` § "Known issues — read before iterating".
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from monitorul_ii.extraction.schema import SchemaError, validate

LINKER_VERSION = "0.2.0"


LinkStatus = Literal["linked", "skip", "error"]


@dataclass(frozen=True)
class LinkResult:
    sidecar_path: Path
    status: LinkStatus
    target_document_id: str | None = None
    reason: str | None = None
    sidecar: dict[str, Any] | None = None  # populated for status=="linked"


@dataclass(frozen=True)
class VoteLinkResult:
    """One row per stenogram sidecar processed by the vote-pair pass."""

    sidecar_path: Path
    status: LinkStatus
    pairs_written: int = 0  # forward links populated this run
    backlinks_written: int = 0  # `resolves` entries appended this run
    reason: str | None = None


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_atomic(path: Path, payload: str) -> None:
    """Atomic write via `.part` rename — same contract as the dispatcher."""
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


# Document types that act as "receiving sessions" for report_facsimile.
# Joint sessions are the dominant case (every R-suffix doc observed in the
# corpus is received in joint session); single-chamber stenograms are
# included for completeness because the schema's `received_at.session_kind`
# allows `camera` / `senat` for future-proofing.
_RECEIVING_SESSION_TYPES = ("plenary_joint_session", "plenary_stenogram")


# Document types that carry vote activities — the vote-pair pass walks
# these and skips qr / committee / report sidecars.
_VOTING_DOC_TYPES = ("plenary_stenogram", "plenary_joint_session")


def build_session_index(sidecars: Iterable[Path]) -> dict[str, str]:
    """Index of `session_date` → `document_id` for receiving-session sidecars.

    Walks the iterable of sidecar paths once, reads each, and indexes any
    `plenary_joint_session` / `plenary_stenogram` sidecar by its
    `metadata.session_date` (falling back to `metadata.published` when
    session_date is null). When multiple sidecars share a date,
    `plenary_joint_session` wins over `plenary_stenogram` (joint sessions
    receive reports more often); within the same type, first-seen wins.
    """
    by_date: dict[str, tuple[int, str]] = {}
    # priority: lower number wins. joint=0 beats stenogram=1.
    type_priority = {"plenary_joint_session": 0, "plenary_stenogram": 1}
    for path in sidecars:
        sc = _read_sidecar(path)
        if sc is None:
            continue
        doc_type = sc.get("document_type")
        if doc_type not in _RECEIVING_SESSION_TYPES:
            continue
        meta = sc.get("metadata") or {}
        date_s = meta.get("session_date") or meta.get("published")
        if not isinstance(date_s, str) or not date_s:
            continue
        doc_id = sc.get("document_id")
        if not isinstance(doc_id, str) or not doc_id:
            continue
        prio = type_priority[doc_type]
        existing = by_date.get(date_s)
        if existing is None or prio < existing[0]:
            by_date[date_s] = (prio, doc_id)
    return {date_s: doc_id for date_s, (_, doc_id) in by_date.items()}


def _get_received_at(sidecar: dict[str, Any]) -> dict[str, Any] | None:
    """Return the report_facsimile `received_at` block, or None if absent."""
    body = sidecar.get("body")
    if not isinstance(body, dict):
        return None
    report = body.get("report")
    if not isinstance(report, dict):
        return None
    received_at = report.get("received_at")
    if not isinstance(received_at, dict):
        return None
    return received_at


def link_report(
    sidecar_path: Path,
    *,
    session_index: dict[str, str],
    force: bool = False,
    write: bool = True,
) -> LinkResult:
    """Link a single report_facsimile sidecar to its receiving stenogram.

    Reads the sidecar, looks up `received_at.session_date` in the index,
    writes `received_in_document` back. Pre-write schema validation
    guards against shape regressions; an invalid post-link sidecar is
    rejected and the file is NOT touched.

    Returns a LinkResult with the new document_id (status=linked) or a
    skip reason (status=skip / error). `write=False` runs the full link
    logic but doesn't touch disk — useful in tests.
    """
    sc = _read_sidecar(sidecar_path)
    if sc is None:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="error",
            reason="failed to read sidecar JSON",
        )
    if sc.get("document_type") != "report_facsimile":
        return LinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="not a report_facsimile sidecar",
        )
    received_at = _get_received_at(sc)
    if received_at is None:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="error",
            reason="missing body.report.received_at block",
        )
    existing = received_at.get("received_in_document")
    if existing and not force:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="already linked",
            target_document_id=existing,
        )
    session_date = received_at.get("session_date")
    if not isinstance(session_date, str) or not session_date:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="no session_date in report",
        )
    target = session_index.get(session_date)
    if target is None:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason=f"no receiving session indexed for {session_date}",
        )
    # Don't link a sidecar to itself (defensive: shouldn't happen because
    # report_facsimile is excluded from the index, but guard anyway).
    if target == sc.get("document_id"):
        return LinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="self-link prevented",
        )
    received_at["received_in_document"] = target
    try:
        validate(sc)
    except SchemaError as exc:
        return LinkResult(
            sidecar_path=sidecar_path,
            status="error",
            reason=f"schema validation failed after link: {exc.errors[0]}",
        )
    if write:
        _write_atomic(
            sidecar_path,
            json.dumps(sc, indent=2, ensure_ascii=False),
        )
    return LinkResult(
        sidecar_path=sidecar_path,
        status="linked",
        target_document_id=target,
        sidecar=sc,
    )


def link_all(
    sidecars: Iterable[Path],
    *,
    force: bool = False,
    write: bool = True,
) -> Iterator[LinkResult]:
    """Walk a list of sidecars, build the session index, link each report.

    Two passes over the list: pass 1 builds the index from joint /
    single-chamber stenograms; pass 2 yields a LinkResult per
    report_facsimile sidecar. Non-report sidecars are silently skipped
    (no result yielded — the caller filters on its own input).
    """
    sidecar_list = list(sidecars)
    session_index = build_session_index(sidecar_list)
    for path in sidecar_list:
        sc = _read_sidecar(path)
        if sc is None:
            continue
        if sc.get("document_type") != "report_facsimile":
            continue
        yield link_report(
            path,
            session_index=session_index,
            force=force,
            write=write,
        )


# -- vote-pair pass (v0.2.0) ------------------------------------------------


# Reference types that produce a stable cite key for cross-doc vote pairing.
#
# Only `bill` qualifies. PL-x N/Y / L N/Y is the canonical bill identifier
# in Romanian parliamentary procedure: the same legislative initiative
# carries the same cite from first reading through final vote, so two docs
# discussing the same bill always share the same PL-x. The other ref types
# (`law`, `oug`, `og`, `parliamentary_resolution`, `chamber_resolution`)
# fail this test in production: a 5,551-doc smoke spot-check found that
# `law:47/1992` (the Constitutional Court procedural law, cited every
# time the Senate considers a CCR referral) collided dozens of unrelated
# CCR-referral votes into one key; `law:286/2009` (the Criminal Code,
# cited by every bill amending it) collided unrelated criminal-code
# amendments. These are NOT cross-doc deferrals — they're independent
# items that incidentally share a cite. Bill-only is the precision floor.
_STABLE_REF_TYPES = frozenset({"bill"})


_WHITESPACE_RE = re.compile(r"\s+")


def _agenda_title_key(title: str) -> str:
    """Normalised hash of an agenda title — fallback match key."""
    norm = _WHITESPACE_RE.sub(" ", (title or "").lower()).strip()[:100]
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
    return f"title:{digest}"


def _vote_match_key(agenda_item: dict[str, Any]) -> str | None:
    """Derive a stable match key from an agenda item.

    Returns None when the item carries no stable signal — those votes are
    unmatchable and stay unlinked. We deliberately do NOT fall back to an
    agenda-title hash because agenda titles are dominated by generic
    procedural phrases (`Ședința`, `Aprobarea ordinii de zi`, ...) that
    repeat in every session and produce thousands of false-positive
    same-key matches across unrelated debates. Bill / law / OUG / OG /
    parliamentary_resolution / chamber_resolution citations are stable
    across re-discussions of the same item, and motion titles are
    distinctive enough; everything else stays unlinked. The cost is a
    coverage gap on truly cross-doc-deferred procedural items, but those
    are rare in practice and the linker's job is to be precise, not
    aggressive.
    """
    refs = agenda_item.get("primary_references") or []
    # Bill cite — first `type=bill` ref wins. The prefix (PL-x vs L) is
    # part of the key because PL-x N/Y (Camera-originated) and L N/Y
    # (Senate-originated) are different bills — they share number-spaces
    # only by coincidence, so a flat "bill:N/Y" key would cross-collide
    # them.
    for r in refs:
        if r.get("type") == "bill":
            prefix = r.get("prefix") or "?"
            number = r.get("number")
            year = r.get("year")
            if number is not None and year is not None:
                return f"bill:{prefix}:{number}/{year}"
    # Motion title — only for motion-class refs with an embedded quoted
    # title; bare `Moțiunea simplă` without a title is too generic.
    for r in refs:
        if r.get("type") == "motion":
            kind = r.get("motion_kind") or "unknown"
            title = (r.get("title") or "").strip()
            if title and len(title) >= 12:
                norm = _WHITESPACE_RE.sub(" ", title.lower())[:100]
                digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
                return f"motion:{kind}:{digest}"
    return None


def _parse_iso_date(s: str | None) -> date | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _sidecar_session_date(sidecar: dict[str, Any]) -> date | None:
    meta = sidecar.get("metadata") or {}
    return _parse_iso_date(meta.get("session_date") or meta.get("published"))


@dataclass(frozen=True)
class _VoteEntry:
    document_id: str
    session_date: date
    sidecar_path: Path
    agenda_index: int
    activity_index: int
    match_key: str
    outcome: str
    motion_type: str | None


def build_vote_index(
    sidecars: Iterable[Path],
) -> dict[str, list[_VoteEntry]]:
    """Index every plenary vote by match key, sorted by session date.

    Walks the iterable of sidecars, extracts every `vote` activity from
    `plenary_stenogram` / `plenary_joint_session` sidecars, computes its
    match key, and groups by key. Each group is sorted by session_date
    ascending so the earliest-resolver-wins lookup is a linear scan.
    """
    by_key: dict[str, list[_VoteEntry]] = {}
    for path in sidecars:
        sc = _read_sidecar(path)
        if sc is None:
            continue
        if sc.get("document_type") not in _VOTING_DOC_TYPES:
            continue
        doc_id = sc.get("document_id")
        if not isinstance(doc_id, str):
            continue
        session_date = _sidecar_session_date(sc)
        if session_date is None:
            continue
        body = sc.get("body") or {}
        for agenda_idx, item in enumerate(body.get("agenda_items") or []):
            key = _vote_match_key(item)
            if key is None:
                continue
            for act_idx, act in enumerate(item.get("activities") or []):
                if act.get("type") != "vote":
                    continue
                outcome = act.get("outcome")
                if not isinstance(outcome, str):
                    continue
                entry = _VoteEntry(
                    document_id=doc_id,
                    session_date=session_date,
                    sidecar_path=path,
                    agenda_index=agenda_idx,
                    activity_index=act_idx,
                    match_key=key,
                    outcome=outcome,
                    motion_type=act.get("motion_type"),
                )
                by_key.setdefault(key, []).append(entry)
    for entries in by_key.values():
        entries.sort(key=lambda e: (e.session_date, e.document_id))
    return by_key


# Window for cross-doc deferral pairing. Most resolutions land within
# 1-2 weeks; beyond 60 days the same key is more likely a separate
# debate cycle (re-introduced bills, recurring committee reports).
DEFERRAL_WINDOW_DAYS = 60


def _find_resolver(
    deferral: _VoteEntry,
    candidates: list[_VoteEntry],
) -> _VoteEntry | None:
    """Pick the earliest non-deferred vote within the window.

    Candidates are pre-sorted by session_date. Skip the deferral itself
    (same doc + same agenda + same activity index); only consider future
    votes within `DEFERRAL_WINDOW_DAYS`. The first such vote that's not
    itself `outcome=deferred` resolves the chain.

    Returns None if no resolver fits — the deferred vote stays unlinked.
    A still-deferred vote in the next session DOES count as a forward
    target (chain support); the chain terminates at the first non-
    deferred vote (handled by build_pairs's two-pass walk).
    """
    cutoff = deferral.session_date + timedelta(days=DEFERRAL_WINDOW_DAYS)
    for cand in candidates:
        if cand.session_date <= deferral.session_date:
            continue
        if cand.session_date > cutoff:
            return None
        # Same-doc: shouldn't happen (build_vote_index never emits self
        # entries since deferred ones get processed separately) but
        # guard anyway.
        if (
            cand.document_id == deferral.document_id
            and cand.agenda_index == deferral.agenda_index
            and cand.activity_index == deferral.activity_index
        ):
            continue
        return cand
    return None


def _build_pairs(
    vote_index: dict[str, list[_VoteEntry]],
) -> tuple[
    dict[tuple[str, int, int], str],
    dict[tuple[str, int, int], list[str]],
]:
    """Compute forward + back links from the vote index.

    Returns:
      forward_links: {(document_id, agenda_index, activity_index) →
                      target_document_id}
        — keyed by the deferring vote; value is the next doc in the
          chain (immediate successor, even if that successor is itself
          still deferred).
      back_links: {(document_id, agenda_index, activity_index) →
                   [origin_document_ids ...]}
        — keyed by the resolving vote; value is every prior deferred
          vote that ultimately reached this resolver (chain reversal).
    """
    forward: dict[tuple[str, int, int], str] = {}
    back: dict[tuple[str, int, int], list[str]] = {}
    for key, entries in vote_index.items():
        for i, e in enumerate(entries):
            if e.outcome != "deferred":
                continue
            # Pick the immediate next entry in the chain (earliest follow-up
            # within the window — could itself be deferred OR a resolver).
            tail = entries[i + 1 :]
            cutoff = e.session_date + timedelta(days=DEFERRAL_WINDOW_DAYS)
            successor: _VoteEntry | None = None
            for cand in tail:
                if cand.session_date <= e.session_date:
                    continue
                if cand.session_date > cutoff:
                    break
                successor = cand
                break
            if successor is None:
                continue
            forward[(e.document_id, e.agenda_index, e.activity_index)] = (
                successor.document_id
            )
        # Now compute back-links by walking each chain to its terminal
        # non-deferred vote and accumulating origins.
        for i, e in enumerate(entries):
            if e.outcome != "deferred":
                continue
            chain_origins: list[_VoteEntry] = [e]
            walker_idx = i
            while True:
                walker = entries[walker_idx]
                fwd_key = (
                    walker.document_id,
                    walker.agenda_index,
                    walker.activity_index,
                )
                if fwd_key not in forward:
                    break
                next_doc_id = forward[fwd_key]
                next_entry: _VoteEntry | None = None
                next_idx = -1
                for j in range(walker_idx + 1, len(entries)):
                    if entries[j].document_id == next_doc_id:
                        next_entry = entries[j]
                        next_idx = j
                        break
                if next_entry is None:
                    break
                if next_entry.outcome != "deferred":
                    # Resolver found — record the back-link
                    bk = (
                        next_entry.document_id,
                        next_entry.agenda_index,
                        next_entry.activity_index,
                    )
                    accum = back.setdefault(bk, [])
                    for origin in chain_origins:
                        if origin.document_id not in accum:
                            accum.append(origin.document_id)
                    break
                # Still deferred — extend chain and walk one more step
                chain_origins.append(next_entry)
                walker_idx = next_idx
    return forward, back


def link_vote(
    sidecar_path: Path,
    *,
    forward_links: dict[tuple[str, int, int], str],
    back_links: dict[tuple[str, int, int], list[str]],
    force: bool = False,
    write: bool = True,
) -> VoteLinkResult:
    """Apply forward + back links to one stenogram sidecar's votes.

    Reads the sidecar, walks each agenda item's activities, populates
    `defers_to` (forward) on deferred votes and appends `resolves` (back)
    entries on resolving votes. Pre-write schema validation; atomic
    write via `.part` rename. Idempotent: skips votes whose forward-link
    already matches (and back-link already contains the expected origin
    set) — `force=True` overwrites.

    Returns a VoteLinkResult counting how many forward + back links were
    actually written this run.
    """
    sc = _read_sidecar(sidecar_path)
    if sc is None:
        return VoteLinkResult(
            sidecar_path=sidecar_path,
            status="error",
            reason="failed to read sidecar JSON",
        )
    if sc.get("document_type") not in _VOTING_DOC_TYPES:
        return VoteLinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="not a plenary sidecar",
        )
    doc_id = sc.get("document_id")
    if not isinstance(doc_id, str):
        return VoteLinkResult(
            sidecar_path=sidecar_path,
            status="error",
            reason="missing document_id",
        )
    body = sc.get("body") or {}
    items = body.get("agenda_items") or []
    pairs_written = 0
    backlinks_written = 0
    for ag_idx, item in enumerate(items):
        for act_idx, act in enumerate(item.get("activities") or []):
            if act.get("type") != "vote":
                continue
            key = (doc_id, ag_idx, act_idx)
            # Forward link: only on deferred votes.
            target = forward_links.get(key)
            if target is not None:
                existing = act.get("defers_to")
                if existing != target and (existing in (None, "") or force):
                    act["defers_to"] = target
                    pairs_written += 1
            # Back link: only on resolvers (any non-deferred vote that
            # received chains from prior deferrals).
            origins = back_links.get(key)
            if origins:
                cur = act.get("resolves") or []
                if not isinstance(cur, list):
                    cur = []
                added = False
                if force:
                    # Replace wholesale (idempotent + de-dup).
                    new_list = list(dict.fromkeys(origins))
                    if new_list != list(cur):
                        act["resolves"] = new_list
                        added = True
                else:
                    # Merge — append any origin not already present.
                    new_list = list(cur)
                    for origin in origins:
                        if origin not in new_list:
                            new_list.append(origin)
                            added = True
                    if added:
                        act["resolves"] = new_list
                if added:
                    backlinks_written += 1
    if pairs_written == 0 and backlinks_written == 0:
        return VoteLinkResult(
            sidecar_path=sidecar_path,
            status="skip",
            reason="no vote-pair updates",
        )
    try:
        validate(sc)
    except SchemaError as exc:
        return VoteLinkResult(
            sidecar_path=sidecar_path,
            status="error",
            reason=f"schema validation failed after link: {exc.errors[0]}",
        )
    if write:
        _write_atomic(
            sidecar_path,
            json.dumps(sc, indent=2, ensure_ascii=False),
        )
    return VoteLinkResult(
        sidecar_path=sidecar_path,
        status="linked",
        pairs_written=pairs_written,
        backlinks_written=backlinks_written,
    )


def link_all_votes(
    sidecars: Iterable[Path],
    *,
    force: bool = False,
    write: bool = True,
) -> Iterator[VoteLinkResult]:
    """Walk a list of sidecars, build the vote index, write pair updates.

    Three passes over the list: pass 1 builds the per-key vote index;
    pass 2 derives forward + back links from the index; pass 3 yields
    one VoteLinkResult per touched plenary sidecar (sidecars with no
    updates are skipped silently — no result yielded). Non-plenary
    sidecars are filtered out cheaply by document_type.
    """
    sidecar_list = list(sidecars)
    vote_index = build_vote_index(sidecar_list)
    forward_links, back_links = _build_pairs(vote_index)
    # Build the set of paths that actually need an update (to avoid
    # parsing every plenary sidecar when only a few got updates).
    touched: set[Path] = set()
    for key in forward_links.keys() | back_links.keys():
        # Look up the path via the index (every entry remembers its path)
        for entries in vote_index.values():
            for e in entries:
                if (e.document_id, e.agenda_index, e.activity_index) == key:
                    touched.add(e.sidecar_path)
    for path in sidecar_list:
        if path not in touched:
            continue
        yield link_vote(
            path,
            forward_links=forward_links,
            back_links=back_links,
            force=force,
            write=write,
        )


__all__ = [
    "DEFERRAL_WINDOW_DAYS",
    "LINKER_VERSION",
    "LinkResult",
    "VoteLinkResult",
    "build_session_index",
    "build_vote_index",
    "link_all",
    "link_all_votes",
    "link_report",
    "link_vote",
]
