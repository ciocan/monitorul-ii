"""Quick read-only survey of stamped versions on existing extraction.json sidecars.

Counts schema_version, doc_type, per-component extractor_versions distributions
so we can predict how many docs will re-extract on a version-aware sweep.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "pdfs")
    paths = sorted(root.glob("*.extraction.json"))
    print(f"sidecars: {len(paths)}")

    schema_versions: Counter[str] = Counter()
    doc_types: Counter[str] = Counter()
    component_versions: dict[str, Counter[str]] = {}
    coverage_buckets: Counter[str] = Counter()
    coverage_min = 1.0
    coverage_min_path = ""
    error_files: list[str] = []

    for p in paths:
        try:
            with p.open() as f:
                sc = json.load(f)
        except Exception as e:
            error_files.append(f"{p.name}: parse {e}")
            continue
        schema_versions[sc.get("schema_version", "<missing>")] += 1
        doc_types[sc.get("document_type", "<missing>")] += 1
        for k, v in sc.get("extraction", {}).get("extractor_versions", {}).items():
            component_versions.setdefault(k, Counter())[v] += 1
        cov = sc.get("coverage", {}).get("claimed_pct")
        if isinstance(cov, (int, float)):
            if cov < coverage_min:
                coverage_min = cov
                coverage_min_path = p.name
            if cov >= 0.99:
                coverage_buckets["≥0.99"] += 1
            elif cov >= 0.95:
                coverage_buckets["0.95–0.99"] += 1
            elif cov >= 0.90:
                coverage_buckets["0.90–0.95"] += 1
            elif cov >= 0.80:
                coverage_buckets["0.80–0.90"] += 1
            else:
                coverage_buckets["<0.80"] += 1

    print("\nschema_version:")
    for k, v in schema_versions.most_common():
        print(f"  {k}: {v}")
    print("\ndoc_type:")
    for k, v in doc_types.most_common():
        print(f"  {k}: {v}")
    print("\nextractor_versions (per component):")
    for comp, ctr in sorted(component_versions.items()):
        line = "  ".join(f"{vv}={cc}" for vv, cc in ctr.most_common())
        print(f"  {comp}: {line}")
    print("\ncoverage buckets:")
    for k in ("≥0.99", "0.95–0.99", "0.90–0.95", "0.80–0.90", "<0.80"):
        print(f"  {k}: {coverage_buckets[k]}")
    print(f"\ncoverage min: {coverage_min:.4f} ({coverage_min_path})")
    if error_files:
        print(f"\nerrors: {len(error_files)}")
        for line in error_files[:10]:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
