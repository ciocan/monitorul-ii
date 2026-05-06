"""Corpus-wide smoke tests over post-sweep extraction.json sidecars.

Reads every sidecar under pdfs/, validates against the runtime schema, and
emits an aggregate health report covering:

  - schema validation (errors / rejections)
  - schema_version distribution
  - document_type distribution
  - extractor_versions distribution (per component)
  - coverage histogram + per-doc-type means / medians / floors
  - per-doc-type record-count distributions (votes / agenda items / interpellations / committees / questions)
  - reference-type distribution (over both primary_references and references_mentioned)
  - linker fill rates: defers_to / resolves[] / received_in_document / xref resolved_to
  - backfill fill rates: ministry_normalized, addressed_to_normalized, issuing_body_normalized, proposed_by

Designed to be run after `extract --force` + `link --force` + `backfill --force`.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from monitorul_ii.extraction.schema import SchemaError, validate


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def walk_refs(node, sink: list[dict]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("primary_references", "references_mentioned") and isinstance(
                v, list
            ):
                sink.extend(v)
            else:
                walk_refs(v, sink)
    elif isinstance(node, list):
        for item in node:
            walk_refs(item, sink)


def walk_votes(node, sink: list[dict]) -> None:
    if isinstance(node, dict):
        if node.get("type") == "vote":
            sink.append(node)
        for v in node.values():
            walk_votes(v, sink)
    elif isinstance(node, list):
        for item in node:
            walk_votes(item, sink)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "pdfs")
    paths = sorted(root.glob("*.extraction.json"))
    print(f"sidecars: {len(paths)}")

    schema_errors: list[str] = []
    schema_versions: Counter[str] = Counter()
    doc_types: Counter[str] = Counter()
    component_versions: dict[str, Counter[str]] = defaultdict(Counter)

    coverage_by_type: dict[str, list[float]] = defaultdict(list)
    record_count_by_type: dict[str, list[int]] = defaultdict(list)

    ref_types: Counter[str] = Counter()
    unknown_hints: Counter[str] = Counter()
    unknowns_total = 0
    unknowns_resolved = 0

    votes_total = 0
    votes_with_proposed_by = 0
    votes_proposed_by_guvern = 0
    votes_with_defers_to = 0
    votes_with_resolves = 0

    interp_total = 0
    interp_with_question_text = 0
    interp_with_response = 0
    interp_with_addressee = 0
    interp_with_addressee_normalized = 0

    qr_questions_total = 0
    qr_with_addressee = 0
    qr_with_ministry_normalized = 0

    report_total = 0
    report_with_issuing = 0
    report_with_issuing_normalized = 0
    report_with_received_in_document = 0

    committee_total = 0
    committee_with_roster = 0
    committee_with_joint_with = 0
    committee_with_agenda = 0

    agenda_items_total = 0
    agenda_categories: Counter[str] = Counter()
    agenda_outcomes: Counter[str] = Counter()

    error_files: list[str] = []
    smallest_cov: list[tuple[float, str]] = []

    for p in paths:
        try:
            with p.open() as f:
                sc = json.load(f)
        except Exception as e:
            error_files.append(f"{p.name}: parse {e}")
            continue

        try:
            validate(sc)
        except SchemaError as e:
            schema_errors.append(f"{p.name}: {e}")

        schema_versions[sc.get("schema_version", "<missing>")] += 1
        dt = sc.get("document_type", "<missing>")
        doc_types[dt] += 1
        for k, v in sc.get("extraction", {}).get("extractor_versions", {}).items():
            component_versions[k][v] += 1

        cov = sc.get("coverage", {}).get("claimed_pct")
        if isinstance(cov, (int, float)):
            coverage_by_type[dt].append(cov)
            smallest_cov.append((cov, p.name))

        body = sc.get("body", {})

        # ref-type distribution
        refs: list[dict] = []
        walk_refs(body, refs)
        for r in refs:
            t = r.get("type", "<missing>")
            ref_types[t] += 1
            if t == "unknown":
                unknowns_total += 1
                hint = r.get("hint")
                if hint:
                    unknown_hints[hint] += 1
                if r.get("resolved_to"):
                    unknowns_resolved += 1

        # vote stats
        if dt in ("plenary_stenogram", "plenary_joint_session"):
            votes: list[dict] = []
            walk_votes(body, votes)
            votes_total += len(votes)
            for v in votes:
                pb = v.get("proposed_by")
                if pb:
                    votes_with_proposed_by += 1
                    if isinstance(pb, dict) and pb.get("role") == "Guvern":
                        votes_proposed_by_guvern += 1
                if v.get("defers_to"):
                    votes_with_defers_to += 1
                if v.get("resolves"):
                    votes_with_resolves += 1

            interps = body.get("interpellations", []) or []
            interp_total += len(interps)
            for ip in interps:
                if ip.get("question_text"):
                    interp_with_question_text += 1
                if (
                    ip.get("response", {}).get("text")
                    if isinstance(ip.get("response"), dict)
                    else False
                ):
                    interp_with_response += 1
                if ip.get("addressed_to"):
                    interp_with_addressee += 1
                if ip.get("addressed_to_normalized"):
                    interp_with_addressee_normalized += 1

            ag = body.get("agenda_items", []) or []
            agenda_items_total += len(ag)
            record_count_by_type[dt].append(len(ag))
            for a in ag:
                agenda_categories[a.get("category", "<missing>")] += 1
                oc = a.get("outcome")
                if oc:
                    agenda_outcomes[oc] += 1

        elif dt == "question_register":
            qs = body.get("questions", []) or []
            qr_questions_total += len(qs)
            record_count_by_type[dt].append(len(qs))
            for q in qs:
                addr = q.get("addressee") or {}
                if addr.get("raw") or addr.get("name") or addr.get("ministry"):
                    qr_with_addressee += 1
                if addr.get("ministry_normalized"):
                    qr_with_ministry_normalized += 1

        elif dt == "report_facsimile":
            report_total += 1
            rep = body.get("report") or {}
            if rep.get("issuing_body"):
                report_with_issuing += 1
            if rep.get("issuing_body_normalized"):
                report_with_issuing_normalized += 1
            recv = (rep.get("received_at") or {}) if isinstance(rep, dict) else {}
            if recv.get("received_in_document"):
                report_with_received_in_document += 1

        elif dt == "committee_synthesis":
            committee_total += 1
            committees = body.get("committees", []) or []
            record_count_by_type[dt].append(len(committees))
            for c in committees:
                meetings = c.get("meetings") or []
                if any(m.get("roster") for m in meetings):
                    committee_with_roster += 1
                if any(m.get("joint_with") for m in meetings):
                    committee_with_joint_with += 1
                if any(m.get("agenda") for m in meetings):
                    committee_with_agenda += 1

    smallest_cov.sort()

    # ---- print
    print(
        f"\n=== schema validation: errors={len(schema_errors)}, parse-errors={len(error_files)}"
    )
    for line in schema_errors[:10]:
        print(f"  ! {line}")
    if len(schema_errors) > 10:
        print(f"  ... +{len(schema_errors) - 10} more")
    for line in error_files[:5]:
        print(f"  ? {line}")

    print("\n=== schema_version")
    for k, v in schema_versions.most_common():
        print(f"  {k}: {v}")

    print("\n=== document_type")
    for k, v in doc_types.most_common():
        print(f"  {k}: {v}")

    print("\n=== extractor_versions (per component)")
    for comp, ctr in sorted(component_versions.items()):
        line = "  ".join(f"{vv}={cc}" for vv, cc in ctr.most_common())
        print(f"  {comp}: {line}")

    print("\n=== coverage by doc_type")
    print(
        f"  {'type':25s}  {'n':>5s}  {'mean':>7s}  {'p50':>7s}  {'p25':>7s}  {'min':>7s}"
    )
    for dt, vals in sorted(coverage_by_type.items()):
        if not vals:
            continue
        m = statistics.fmean(vals)
        p50 = statistics.median(vals)
        p25 = percentile(vals, 0.25)
        mn = min(vals)
        print(
            f"  {dt:25s}  {len(vals):5d}  {m:7.4f}  {p50:7.4f}  {p25:7.4f}  {mn:7.4f}"
        )

    cov_below_targets = {
        "<0.50": 0,
        "0.50–0.80": 0,
        "0.80–0.90": 0,
        "0.90–0.95": 0,
        "0.95–0.99": 0,
        "≥0.99": 0,
    }
    for vals in coverage_by_type.values():
        for v in vals:
            if v < 0.50:
                cov_below_targets["<0.50"] += 1
            elif v < 0.80:
                cov_below_targets["0.50–0.80"] += 1
            elif v < 0.90:
                cov_below_targets["0.80–0.90"] += 1
            elif v < 0.95:
                cov_below_targets["0.90–0.95"] += 1
            elif v < 0.99:
                cov_below_targets["0.95–0.99"] += 1
            else:
                cov_below_targets["≥0.99"] += 1
    print("\n=== coverage histogram")
    for k, v in cov_below_targets.items():
        print(f"  {k}: {v}")

    print("\n=== bottom-15 coverage")
    for c, name in smallest_cov[:15]:
        print(f"  {c:.4f}  {name}")

    print("\n=== reference types (across all docs)")
    total_refs = sum(ref_types.values())
    for k, v in ref_types.most_common():
        pct = 100 * v / total_refs if total_refs else 0
        print(f"  {k:30s}  {v:7d}  ({pct:.2f}%)")

    print(
        f"\n=== unknown refs: total={unknowns_total}, resolved_to={unknowns_resolved} "
        f"({100 * unknowns_resolved / max(unknowns_total, 1):.1f}%)"
    )
    for k, v in unknown_hints.most_common():
        print(f"  hint={k:15s}  {v}")

    print("\n=== votes & vote linker")
    print(f"  votes total: {votes_total}")
    print(
        f"  votes with proposed_by: {votes_with_proposed_by} "
        f"({100 * votes_with_proposed_by / max(votes_total, 1):.2f}%)"
    )
    print(
        f"  votes proposed_by Guvern: {votes_proposed_by_guvern} "
        f"({100 * votes_proposed_by_guvern / max(votes_total, 1):.2f}%)"
    )
    print(f"  votes with defers_to: {votes_with_defers_to}")
    print(f"  votes with resolves[]: {votes_with_resolves}")

    print("\n=== interpellations")
    print(f"  total: {interp_total}")
    print(
        f"  with question_text: {interp_with_question_text} "
        f"({100 * interp_with_question_text / max(interp_total, 1):.1f}%)"
    )
    print(
        f"  with response: {interp_with_response} "
        f"({100 * interp_with_response / max(interp_total, 1):.1f}%)"
    )
    print(
        f"  with addressed_to: {interp_with_addressee} "
        f"({100 * interp_with_addressee / max(interp_total, 1):.1f}%)"
    )
    print(
        f"  with addressed_to_normalized: {interp_with_addressee_normalized} "
        f"({100 * interp_with_addressee_normalized / max(interp_total, 1):.1f}%)"
    )

    print("\n=== question_register")
    print(f"  questions total: {qr_questions_total}")
    print(
        f"  with addressee: {qr_with_addressee} "
        f"({100 * qr_with_addressee / max(qr_questions_total, 1):.1f}%)"
    )
    print(
        f"  with ministry_normalized: {qr_with_ministry_normalized} "
        f"({100 * qr_with_ministry_normalized / max(qr_questions_total, 1):.1f}%)"
    )

    print("\n=== report_facsimile")
    print(f"  total docs: {report_total}")
    print(
        f"  with issuing_body: {report_with_issuing} "
        f"({100 * report_with_issuing / max(report_total, 1):.1f}%)"
    )
    print(
        f"  with issuing_body_normalized: {report_with_issuing_normalized} "
        f"({100 * report_with_issuing_normalized / max(report_total, 1):.1f}%)"
    )
    print(
        f"  with received_in_document: {report_with_received_in_document} "
        f"({100 * report_with_received_in_document / max(report_total, 1):.1f}%)"
    )

    print("\n=== committee_synthesis")
    print(f"  total docs: {committee_total}")
    print(f"  committees with roster: {committee_with_roster}")
    print(f"  committees with joint_with: {committee_with_joint_with}")
    print(f"  committees with agenda_items: {committee_with_agenda}")

    print("\n=== agenda categories (plenary)")
    print(f"  agenda items total: {agenda_items_total}")
    for k, v in agenda_categories.most_common(15):
        print(f"  {k:35s}  {v}")
    print("\n=== agenda outcomes (plenary)")
    for k, v in agenda_outcomes.most_common(15):
        print(f"  {k:30s}  {v}")

    print(
        f"\n=== exit OK (errors={len(schema_errors)}, parse-errors={len(error_files)})"
    )
    return 0 if not schema_errors and not error_files else 1


if __name__ == "__main__":
    sys.exit(main())
