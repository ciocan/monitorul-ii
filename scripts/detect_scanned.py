"""Identify PDFs in pdfs/ that are mostly scanned (image-only) and need OCR.

Each PDF is probed inside a child subprocess for crash isolation: PyMuPDF can
SIGSEGV on malformed image-only PDFs, and one bad file must not abort the
whole sweep. A JSONL state file is appended to as each result lands so a
crash mid-run loses at most the in-flight PDFs (re-run resumes).

The per-PDF classifier uses two metrics derived from per-page counts:

  - avg_chars_per_page = total_chars / pages
  - image_pages_pct    = image_pages / pages   (page has >= 1 embedded image)

Recommendation tiers (informed by the actual MO corpus distribution, where
even fully-scanned PDFs leak ~100 chars/page from headers / page numbers):

  - ocr_required    : avg_chars < 200  AND image_pages_pct >= 0.5
  - ocr_recommended : avg_chars < 500  AND image_pages_pct >= 0.2
  - hybrid          : image_pages_pct >= 0.5 AND avg_chars < 2000
  - text_ok         : everything else

Usage:
  .venv/bin/python scripts/detect_scanned.py [pdf_dir] [out_csv]
  .venv/bin/python scripts/detect_scanned.py --probe <pdf_path>   # worker mode
  .venv/bin/python scripts/detect_scanned.py --reset              # clear state
  .venv/bin/python scripts/detect_scanned.py --rebuild [out_csv]  # CSV-only, no probe
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

TEXT_THRESHOLD = (
    50  # per-page char floor used inside the worker (legacy bookkeeping only)
)
PROBE_TIMEOUT_S = 120
WORKERS = 8

STATE_PATH = Path("/tmp/detect_scanned_state.jsonl")


@dataclass(slots=True)
class Stats:
    file: str
    pages: int = 0
    total_chars: int = 0
    scanned_pages: int = 0
    blank_pages: int = 0
    text_pages: int = 0
    image_pages: int = 0
    size_mb: float = 0.0
    error: str = ""

    @property
    def image_pages_pct(self) -> float:
        return self.image_pages / self.pages if self.pages else 0.0

    @property
    def avg_chars_per_page(self) -> float:
        return self.total_chars / self.pages if self.pages else 0.0

    def recommendation(self) -> str:
        if self.error:
            return "error"
        avg = self.avg_chars_per_page
        img = self.image_pages_pct
        if avg < 200 and img >= 0.5:
            return "ocr_required"
        if avg < 500 and img >= 0.2:
            return "ocr_recommended"
        if img >= 0.5 and avg < 2000:
            return "hybrid"
        return "text_ok"


def probe(pdf_path: Path) -> dict:
    """Worker mode: analyze ONE pdf, JSON-print to stdout, exit cleanly."""
    import pymupdf  # heavy import; only paid in the worker

    doc = pymupdf.open(pdf_path)
    pages = doc.page_count
    total_chars = scanned = blank = text_pg = image_pg = 0
    for page in doc:
        chars = len(page.get_text())
        images = len(page.get_images())
        total_chars += chars
        if images:
            image_pg += 1
        if chars < TEXT_THRESHOLD:
            if images:
                scanned += 1
            else:
                blank += 1
        else:
            text_pg += 1
    doc.close()
    return {
        "pages": pages,
        "total_chars": total_chars,
        "scanned_pages": scanned,
        "blank_pages": blank,
        "text_pages": text_pg,
        "image_pages": image_pg,
    }


def analyze(pdf: Path) -> Stats:
    size_mb = pdf.stat().st_size / (1024 * 1024)
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--probe", str(pdf)],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return Stats(
            file=pdf.name, size_mb=size_mb, error=f"timeout > {PROBE_TIMEOUT_S}s"
        )
    if proc.returncode != 0:
        last_err = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        signal = -proc.returncode if proc.returncode < 0 else proc.returncode
        return Stats(
            file=pdf.name,
            size_mb=size_mb,
            error=f"exit={signal}: {last_err[0]}"[:200],
        )
    try:
        d = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return Stats(file=pdf.name, size_mb=size_mb, error=f"json decode: {e}")
    return Stats(
        file=pdf.name,
        pages=d["pages"],
        total_chars=d["total_chars"],
        scanned_pages=d["scanned_pages"],
        blank_pages=d["blank_pages"],
        text_pages=d["text_pages"],
        image_pages=d["image_pages"],
        size_mb=size_mb,
    )


def load_state() -> dict[str, Stats]:
    out: dict[str, Stats] = {}
    if not STATE_PATH.exists():
        return out
    for line in STATE_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        out[row["file"]] = Stats(
            **{k: row.get(k, "") for k in Stats.__dataclass_fields__}
        )
    return out


def main_probe() -> int:
    path = Path(sys.argv[2])
    try:
        d = probe(path)
    except Exception as e:
        sys.stderr.write(f"{type(e).__name__}: {e}"[:200])
        return 2
    json.dump(d, sys.stdout)
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rebuild_only = "--rebuild" in sys.argv
    if "--reset" in sys.argv:
        STATE_PATH.unlink(missing_ok=True)
        print(f"cleared {STATE_PATH}", file=sys.stderr)

    if rebuild_only:
        out_csv = Path(args[0]) if len(args) > 0 else Path("scanned_candidates.csv")
        results: list[Stats] = list(load_state().values())
        if not results:
            print(f"no state at {STATE_PATH} — run a full probe first", file=sys.stderr)
            return 1
        print(f"rebuilding CSV from {len(results)} cached results", file=sys.stderr)
    else:
        pdf_dir = Path(args[0]) if len(args) > 0 else Path("pdfs")
        out_csv = Path(args[1]) if len(args) > 1 else Path("scanned_candidates.csv")

        pdfs = sorted(pdf_dir.glob("*.pdf"))
        if not pdfs:
            print(f"no PDFs under {pdf_dir}", file=sys.stderr)
            return 1

        state = load_state()
        todo = [p for p in pdfs if p.name not in state]
        print(
            f"scanning {len(todo)} pdfs in {pdf_dir} ({len(state)} resumed from {STATE_PATH}) ...",
            file=sys.stderr,
        )

        results = list(state.values())

        if todo:
            with (
                STATE_PATH.open("a") as state_f,
                ThreadPoolExecutor(max_workers=WORKERS) as ex,
            ):
                futures = {ex.submit(analyze, p): p for p in todo}
                for i, fut in enumerate(as_completed(futures), 1):
                    pdf = futures[fut]
                    try:
                        stats = fut.result()
                    except Exception as e:
                        stats = Stats(
                            file=pdf.name,
                            size_mb=pdf.stat().st_size / (1024 * 1024),
                            error=f"runner: {type(e).__name__}: {str(e)[:160]}",
                        )
                    results.append(stats)
                    state_f.write(json.dumps(asdict(stats)) + "\n")
                    state_f.flush()
                    if i % 200 == 0 or i == len(todo):
                        print(f"  {i}/{len(todo)}", file=sys.stderr)

    rec_rank = {
        "ocr_required": 0,
        "ocr_recommended": 1,
        "hybrid": 2,
        "error": 3,
        "text_ok": 4,
    }
    candidates = [s for s in results if s.recommendation() != "text_ok"]
    candidates.sort(key=lambda s: (rec_rank[s.recommendation()], -s.pages, s.file))

    cols = [
        "file",
        "recommendation",
        "pages",
        "avg_chars_per_page",
        "image_pages_pct",
        "image_pages",
        "total_chars",
        "size_mb",
        "error",
    ]
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in candidates:
            w.writerow(
                [
                    s.file,
                    s.recommendation(),
                    s.pages,
                    f"{s.avg_chars_per_page:.1f}",
                    f"{s.image_pages_pct:.3f}",
                    s.image_pages,
                    s.total_chars,
                    f"{s.size_mb:.1f}",
                    s.error,
                ]
            )

    by_reco: dict[str, int] = {}
    for s in results:
        by_reco[s.recommendation()] = by_reco.get(s.recommendation(), 0) + 1
    print(f"\nwrote {len(candidates)} candidates to {out_csv}", file=sys.stderr)
    print("breakdown across full corpus:", file=sys.stderr)
    for k in ("ocr_required", "ocr_recommended", "hybrid", "text_ok", "error"):
        if by_reco.get(k):
            print(f"  {k:18s} {by_reco[k]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--probe":
        sys.exit(main_probe())
    sys.exit(main())
