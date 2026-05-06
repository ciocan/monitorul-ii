"""Validate post-sweep idempotency: re-running extract without --force, link
without --force, and backfill without --force should each leave the corpus
unchanged. Reports counts only — does not write."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "pdfs")
    bin_path = ".venv/bin/monitorul-ii"

    # snapshot every sidecar's mtime
    paths = sorted(root.glob("*.extraction.json"))
    pre_mtimes = {p.name: p.stat().st_mtime_ns for p in paths}

    print(f"snapshot: {len(paths)} sidecars")

    print("\n=== extract (no --force) — expect skip=N, ok=0 ===")
    rc, out, err = run([bin_path, "extract", "--no-upload", str(root)])
    print(err.splitlines()[-1] if err else f"rc={rc} (no stderr)")

    print("\n=== link (no --force, dry-run) — expect 0 changes ===")
    rc, out, err = run([bin_path, "link", "--dry-run", "--no-upload", str(root)])
    last = err.splitlines()[-3:] if err else []
    for line in last:
        print(line)

    print("\n=== backfill (no --force, dry-run) — expect 0 changes ===")
    rc, out, err = run(
        [bin_path, "backfill", "--dry-run", "--no-upload", "--kind=all", str(root)]
    )
    last = err.splitlines()[-3:] if err else []
    for line in last:
        print(line)

    # mtimes should not have changed (extract skipped, link/backfill dry-run)
    post_paths = sorted(root.glob("*.extraction.json"))
    post_mtimes = {p.name: p.stat().st_mtime_ns for p in post_paths}
    changed = [n for n in pre_mtimes if pre_mtimes[n] != post_mtimes.get(n)]
    print(f"\nmtime drift: {len(changed)} sidecars (expected 0)")
    for n in changed[:10]:
        print(f"  {n}")
    return 0 if not changed else 1


if __name__ == "__main__":
    sys.exit(main())
