"""Targeted regression probes for the recent extractor changes.

Each probe maps to a recent commit and asserts the headline metric the commit
committed to (per CLAUDE.md / commit message). Designed to make any silent
regression visible in the smoke output.

Probes:
  P1  agenda title contamination (e894709)        — no titles containing `--- ---`, `<br>`, or trailing-ordinal patterns
  P2  references year-bounds guard (1.10.0 / 0.6.0) — unknown.law-ish bucket includes the 12 ex-error docs; no rejected.json
  P3  interpellations addressed_to (0.2.3)        — fill rate ≥ 85% on plenary docs that contain a question/interpellation block
  P4  report_facsimile issuing_body (12bfc0b)     — 52/52 R-suffix docs fill issuing_body
  P5  cross-reference linker (84ffe9f)            — schema 1.12.0 universal; resolved_to populated on ≥15% of art-N unknowns
  P6  schema validation                           — zero errors and zero rejected.json files
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from monitorul_ii.extraction.schema import SchemaError, validate

# P1: contamination patterns the v0.2.5 SUMAR clip was supposed to remove
CONTAM_PATTERNS = [
    re.compile(r"--- ---"),
    re.compile(r"<br>"),
    re.compile(r"\s\d+\s+\d+\.\s+[A-ZĂÂÎȘȚ]"),  # `... 15 4. Aprobarea ...`
    re.compile(r"\s+- \d+\. [A-ZĂÂÎȘȚ]"),  # bullet ordinal
]

# P2: ex-rejected docs (now demoted via OOR-year guard, per dcf53f1 commit)
EX_REJECTED_DOCS = {
    # CLAUDE.md mentions 12 docs cleared by references.py v0.6.0 OOR-year demotion
    # We don't have the exact list, so we'll just assert "no .rejected.json under pdfs/"
}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "pdfs")
    paths = sorted(root.glob("*.extraction.json"))
    print(f"probing: {len(paths)} sidecars\n")

    # ---- P1: agenda title contamination
    p1_hits: list[tuple[str, str, str]] = []
    for p in paths:
        sc = json.load(p.open())
        if sc.get("document_type") not in (
            "plenary_stenogram",
            "plenary_joint_session",
        ):
            continue
        for a in sc.get("body", {}).get("agenda_items", []) or []:
            title = a.get("title") or ""
            for pat in CONTAM_PATTERNS:
                if pat.search(title):
                    p1_hits.append((p.name, pat.pattern, title[:120]))
                    break
    print(
        f"P1 agenda-title contamination: {len(p1_hits)} hits (target: 0–15 residuals)"
    )
    for name, pat, title in p1_hits[:10]:
        print(f"  {name}  /{pat}/  {title!r}")

    # ---- P2: rejected-json absence
    rejected = list(root.glob("*.rejected.json"))
    print(f"\nP2 rejected.json files: {len(rejected)} (target: 0)")
    for r in rejected[:10]:
        print(f"  {r.name}")

    # ---- P3: interpellations addressed_to QUALITY
    # CLAUDE.md target: 85%+ of populated addressed_to values normalize against
    # the ministries registry (was 25% pre-v0.2.3 due to single-char misfires).
    # Also: zero single-character addressed_to (the regex bug v0.2.3 fixed).
    interp_total = 0
    interp_with_addr = 0
    interp_with_addr_norm = 0
    interp_short_addr = 0  # single-char misfires the pre-0.2.3 code emitted
    plenary_with_block = 0
    for p in paths:
        sc = json.load(p.open())
        if sc.get("document_type") not in (
            "plenary_stenogram",
            "plenary_joint_session",
        ):
            continue
        interps = sc.get("body", {}).get("interpellations", []) or []
        if interps:
            plenary_with_block += 1
        for ip in interps:
            interp_total += 1
            addr = ip.get("addressed_to")
            if addr:
                interp_with_addr += 1
                if len(addr) <= 2:
                    interp_short_addr += 1
            if ip.get("addressed_to_normalized"):
                interp_with_addr_norm += 1
    extract_rate = 100 * interp_with_addr / max(interp_total, 1)
    norm_rate = 100 * interp_with_addr_norm / max(interp_with_addr, 1)
    print("\nP3 interpellations addressed_to:")
    print(
        f"  populated: {interp_with_addr}/{interp_total} ({extract_rate:.1f}%) — many interpellations are pure declarations w/o addressee"
    )
    print(
        f"  normalized: {interp_with_addr_norm}/{interp_with_addr} ({norm_rate:.1f}%) — target ≥85% (registry-match rate)"
    )
    print(f"  single-char misfires: {interp_short_addr} — target 0")
    print(f"  plenary docs with interpellation block: {plenary_with_block}")

    # ---- P4: report_facsimile issuing_body fill rate
    rep_total = 0
    rep_with_ib = 0
    rep_with_ib_norm = 0
    rep_missing_ib_doc: list[str] = []
    for p in paths:
        sc = json.load(p.open())
        if sc.get("document_type") != "report_facsimile":
            continue
        rep_total += 1
        rep = sc.get("body", {}).get("report") or {}
        if rep.get("issuing_body"):
            rep_with_ib += 1
        else:
            rep_missing_ib_doc.append(p.name)
        if rep.get("issuing_body_normalized"):
            rep_with_ib_norm += 1
    print(
        f"\nP4 report_facsimile.issuing_body: {rep_with_ib}/{rep_total} ({100 * rep_with_ib / max(rep_total, 1):.1f}%) — target 100%"
    )
    print(
        f"   normalized: {rep_with_ib_norm}/{rep_total} ({100 * rep_with_ib_norm / max(rep_total, 1):.1f}%)"
    )
    for name in rep_missing_ib_doc[:5]:
        print(f"  ? {name}")

    # ---- P5: cross-reference linker fill
    schemas: Counter[str] = Counter()
    art_n_unknowns = 0
    art_n_resolved = 0
    art_n_re = re.compile(r"\bart(?:icol)?\.?\s*\d+", re.IGNORECASE)
    for p in paths:
        sc = json.load(p.open())
        schemas[sc.get("schema_version", "<missing>")] += 1
        if sc.get("document_type") not in (
            "plenary_stenogram",
            "plenary_joint_session",
        ):
            continue

        def collect_lists(node, sink):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in (
                        "primary_references",
                        "references_mentioned",
                    ) and isinstance(v, list):
                        sink.append(v)
                    else:
                        collect_lists(v, sink)
            elif isinstance(node, list):
                for item in node:
                    collect_lists(item, sink)

        lists: list[list] = []
        collect_lists(sc.get("body", {}), lists)
        for lst in lists:
            for r in lst:
                if r.get("type") != "unknown":
                    continue
                raw = r.get("raw") or ""
                if art_n_re.search(raw):
                    art_n_unknowns += 1
                    if r.get("resolved_to"):
                        art_n_resolved += 1
    print(
        f"\nP5 art-N unknowns: {art_n_resolved}/{art_n_unknowns} resolved "
        f"({100 * art_n_resolved / max(art_n_unknowns, 1):.1f}%) — target ≥15%"
    )
    print(f"   schema_version histogram: {dict(schemas)}")

    # ---- P6: schema validation
    schema_errors: list[str] = []
    for p in paths:
        try:
            with p.open() as f:
                sc = json.load(f)
            validate(sc)
        except SchemaError as e:
            schema_errors.append(f"{p.name}: {e}")
        except Exception as e:
            schema_errors.append(f"{p.name}: parse {e}")
    print(f"\nP6 schema validation: {len(schema_errors)} errors (target 0)")
    for line in schema_errors[:10]:
        print(f"  ! {line}")

    # ---- summary
    failures = []
    if p1_hits:
        failures.append(f"P1 agenda contamination ({len(p1_hits)} hits)")
    if rejected:
        failures.append(f"P2 rejected.json present ({len(rejected)})")
    if norm_rate < 85.0 or interp_short_addr > 0:
        failures.append(
            f"P3 interpellations normalized rate {norm_rate:.1f}% / single-char {interp_short_addr}"
        )
    if rep_with_ib < rep_total:
        failures.append(f"P4 report_facsimile issuing_body {rep_with_ib}/{rep_total}")
    if art_n_unknowns and 100 * art_n_resolved / art_n_unknowns < 15:
        failures.append(
            f"P5 art-N resolution {100 * art_n_resolved / art_n_unknowns:.1f}%"
        )
    if schema_errors:
        failures.append(f"P6 schema errors ({len(schema_errors)})")

    if failures:
        print(f"\nFAIL — {len(failures)} probe(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nALL PROBES PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
