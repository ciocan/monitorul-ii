from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import pymupdf4llm

ConvertEvent = Literal["skip", "convert", "error"]


@dataclass(frozen=True)
class ConvertEventPayload:
    kind: ConvertEvent
    pdf_path: Path
    md_path: Path
    detail: str | None = None


ProgressFn = Callable[[ConvertEventPayload], None]


# --- noise patterns ---------------------------------------------------------
# pymupdf4llm prepends "**==> picture [WxH] intentionally omitted <==**" for every
# image — MO PDFs carry seal/logo images that are pure noise.
_PICTURE_RE = re.compile(
    r"^\*\*==> picture \[[^\]]+\] intentionally omitted <==\*\*\s*\n",
    re.MULTILINE,
)
# Running header that repeats on every page after the first.
# Variations seen: extra spaces, presence/absence of "AL", date format "1.IV.2026" etc.
_RUNNING_HEADER_RE = re.compile(
    r"^\s*MONITORUL\s+OFICIAL\s+AL\s+ROM[ÂA]NIEI[^\n]*\n",
    re.MULTILINE,
)
# Standalone page numbers (lines containing only an integer).
_PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*\n", re.MULTILINE)
# Hyphenated line break: "comple-\ntare" → "completare".
_HYPHEN_BREAK_RE = re.compile(r"(\w)-\n(\w)")
# Collapse 3+ consecutive newlines to a single blank line.
_BLANK_LINES_RE = re.compile(r"\n{3,}")
# Trailing whitespace at end of lines (pymupdf4llm leaves them).
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)


# --- metadata patterns -------------------------------------------------------
_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_MO-P(?P<part>[IVXM]+)-(?P<num>[0-9A-Za-z]+)-(?P<year>\d{4})\.pdf$"
)
_RO_MONTHS = {
    "ianuarie": 1, "februarie": 2, "martie": 3, "aprilie": 4,
    "mai": 5, "iunie": 6, "iulie": 7, "august": 8,
    "septembrie": 9, "octombrie": 10, "noiembrie": 11, "decembrie": 12,
}  # fmt: skip
_CHAMBER_RE = re.compile(
    # Nominative + genitive ("CAMEREI / SENATULUI" appear in 'c'-suffixed
    # commission-summary issues). The `Þ` covers old PDFs where CP1250 → Latin-1
    # mistranslation turned `Ț` into `Þ` (e.g. 2000-01-25_MO-PII-1c-2000.pdf).
    r"\b(SENATULUI|SENATUL|CAMEREI\s+DEPUTA[ȚTÞ]ILOR|CAMERA\s+DEPUTA[ȚTÞ]ILOR)\b",
    re.IGNORECASE,
)
_SESSION_RE = re.compile(
    r"(SESIUNEA[^\n]+?)(?:\s*\(Legislatura|\s*$)",
    re.MULTILINE,
)
_SESSION_DATE_RE = re.compile(
    r"Ședința\s+din\s+ziua\s+de\s+(\d{1,2})\s+(\w+)\s+(\d{4})",
    re.IGNORECASE,
)
_LEGISLATURE_RE = re.compile(
    r"Legislatura\s+a\s+([IVXM]+)-a",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IssueMeta:
    issue: str
    year: int
    part: str
    published: date
    chamber: str | None = None
    session: str | None = None
    session_date: date | None = None
    legislature: str | None = None

    def to_yaml_frontmatter(self) -> str:
        lines = [
            "---",
            f'issue: "{self.issue}"',
            f"year: {self.year}",
            f'part: "{self.part}"',
            f"published: {self.published.isoformat()}",
        ]
        if self.chamber:
            lines.append(f'chamber: "{self.chamber}"')
        if self.session:
            lines.append(f'session: "{_yaml_escape(self.session)}"')
        if self.session_date:
            lines.append(f"session_date: {self.session_date.isoformat()}")
        if self.legislature:
            lines.append(f'legislature: "{self.legislature}"')
        lines.append("---")
        return "\n".join(lines) + "\n"


def _yaml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def parse_filename(filename: str) -> IssueMeta | None:
    """Extract the metadata that's already encoded in the PDF filename."""
    m = _FILENAME_RE.match(filename)
    if not m:
        return None
    try:
        published = date.fromisoformat(m.group("date"))
    except ValueError:
        return None
    return IssueMeta(
        issue=m.group("num"),
        year=int(m.group("year")),
        part=m.group("part"),
        published=published,
    )


def enrich_meta(text: str, base: IssueMeta) -> IssueMeta:
    """Best-effort enrichment of metadata from the first ~5 KB of body text."""
    head = text[:5000]
    chamber = None
    if (m := _CHAMBER_RE.search(head)) is not None:
        raw = m.group(1).upper()
        chamber = "Senatul" if raw.startswith("SENAT") else "Camera Deputaților"
    session = None
    if (m := _SESSION_RE.search(head)) is not None:
        session = m.group(1).strip()
    session_date = None
    if (m := _SESSION_DATE_RE.search(head)) is not None:
        day = int(m.group(1))
        month = _RO_MONTHS.get(m.group(2).lower())
        year = int(m.group(3))
        if month:
            try:
                session_date = date(year, month, day)
            except ValueError:
                session_date = None
    legislature = None
    if (m := _LEGISLATURE_RE.search(head)) is not None:
        legislature = m.group(1).upper()
    return IssueMeta(
        issue=base.issue,
        year=base.year,
        part=base.part,
        published=base.published,
        chamber=chamber,
        session=session,
        session_date=session_date,
        legislature=legislature,
    )


def clean_markdown(md: str) -> str:
    md = _PICTURE_RE.sub("", md)
    md = _RUNNING_HEADER_RE.sub("", md)
    md = _PAGE_NUMBER_RE.sub("", md)
    md = _HYPHEN_BREAK_RE.sub(r"\1\2", md)
    md = _TRAILING_WS_RE.sub("", md)
    md = _BLANK_LINES_RE.sub("\n\n", md)
    return md.strip() + "\n"


@dataclass
class ConvertResult:
    pdf_path: Path
    md_path: Path
    bytes_written: int


def convert_pdf(pdf_path: Path, md_path: Path) -> ConvertResult:
    """Convert one PDF to MD with MO-specific cleanup + YAML frontmatter.

    Frontmatter is best-effort: if first-page parsing fails, only the filename-derived
    fields are emitted and the body is still produced.
    """
    raw = pymupdf4llm.to_markdown(str(pdf_path), show_progress=False)
    body = clean_markdown(raw)
    base = parse_filename(pdf_path.name)
    frontmatter = ""
    if base is not None:
        try:
            meta = enrich_meta(body, base)
        except Exception:
            meta = base
        frontmatter = meta.to_yaml_frontmatter() + "\n"
    output = frontmatter + body
    md_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = md_path.with_suffix(md_path.suffix + ".part")
    tmp.write_text(output, encoding="utf-8")
    tmp.replace(md_path)
    return ConvertResult(pdf_path=pdf_path, md_path=md_path, bytes_written=len(output))


def collect_pdfs(paths: list[Path], *, reverse: bool = False) -> list[Path]:
    """Resolve a list of file/dir paths into a sorted list of PDF files.

    With `reverse=True` the final list is reversed — for filenames prefixed by
    the publication date this gives newest→oldest order, mirroring `fetch`'s
    `--reverse` semantics.
    """
    out: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix.lower() == ".pdf":
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(p.glob("*.pdf")))
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


@dataclass
class ConvertSummary:
    converted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _process_one(pdf: Path, force: bool) -> tuple[ConvertEvent, Path, Path, str | None]:
    """Worker body — pure function safe to run in a thread pool.

    Returns (kind, pdf_path, md_path, detail). Never raises; errors are wrapped.
    """
    md = pdf.with_suffix(".md")
    if not force and md.exists() and md.stat().st_size > 0:
        return ("skip", pdf, md, None)
    try:
        convert_pdf(pdf, md)
        return ("convert", pdf, md, None)
    except Exception as exc:
        return ("error", pdf, md, f"{pdf}: {exc}")


def convert_all(
    pdfs: list[Path],
    *,
    force: bool = False,
    workers: int = 1,
    on_event: ProgressFn | None = None,
) -> ConvertSummary:
    """Convert every PDF in `pdfs`. Skips MDs that already exist unless `force=True`.

    With `workers > 1`, conversions run in a `ThreadPoolExecutor` — PyMuPDF
    releases the GIL during PDF parsing so threads parallelize on multi-core
    machines. `on_event` fires in the calling thread as workers finish (input
    order is *not* preserved when workers > 1); this keeps S3-upload side
    effects in the caller's thread without locks.
    """
    summary = ConvertSummary()

    def _emit(kind: ConvertEvent, pdf: Path, md: Path, detail: str | None) -> None:
        if kind == "skip":
            summary.skipped += 1
        elif kind == "convert":
            summary.converted += 1
        else:
            summary.errors.append(detail or str(pdf))
        if on_event:
            on_event(
                ConvertEventPayload(kind=kind, pdf_path=pdf, md_path=md, detail=detail)
            )

    if workers <= 1 or len(pdfs) <= 1:
        for pdf in pdfs:
            _emit(*_process_one(pdf, force))
        return summary

    ex = ThreadPoolExecutor(max_workers=workers)
    futures = {ex.submit(_process_one, pdf, force): pdf for pdf in pdfs}
    try:
        for fut in as_completed(futures):
            _emit(*fut.result())
    except KeyboardInterrupt:
        # Cancel queued work and let in-flight threads finish so the
        # interpreter's atexit join doesn't race with a second Ctrl+C
        # (which would print a traceback). In-flight PDFs take a few seconds.
        ex.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        ex.shutdown(wait=True)
    return summary
