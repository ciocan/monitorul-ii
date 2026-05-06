"""How well are speeches actually captured?

For every plenary sidecar:
  - count `## **NAME:**` headers in the source MD body (the ground-truth signal
    that the activities partitioner is supposed to split on)
  - count extracted activities of type=speech
  - compute the speaker-name fill rate, role fill rate, text fill rate
  - compute char-level coverage: total chars across speech.source_span vs body

The headline output answers: "what fraction of source speeches end up as
typed records, and how complete are those records?"
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

SPEAKER_HEADER_RE = re.compile(r"^## \*\*[^*\n]+\*\*\s*$", re.MULTILINE)


def walk_activities(node, sink):
    if isinstance(node, dict):
        if node.get("type") == "speech":
            sink.append(node)
        for v in node.values():
            walk_activities(v, sink)
    elif isinstance(node, list):
        for item in node:
            walk_activities(item, sink)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "pdfs")
    paths = sorted(root.glob("*.extraction.json"))

    total_md_headers = 0
    total_speeches = 0
    speeches_with_name = 0
    speeches_with_role = 0
    speeches_with_title = 0
    speeches_with_text = 0
    speech_text_lens: list[int] = []
    speech_text_chars_total = 0
    body_chars_total = 0
    plenary_docs = 0

    per_doc_capture: list[float] = []  # speeches / md_headers per doc
    per_doc_text_cov: list[float] = []  # speech text chars / body chars per doc

    docs_no_speeches: list[tuple[int, str]] = []

    for p in paths:
        with p.open() as f:
            sc = json.load(f)
        if sc.get("document_type") not in (
            "plenary_stenogram",
            "plenary_joint_session",
        ):
            continue
        plenary_docs += 1

        # Count source-MD headers
        md_path = p.with_suffix("")  # strip .extraction.json
        md_path = Path(str(md_path).replace(".extraction", "") + ".md")
        try:
            body_text = md_path.read_text()
        except FileNotFoundError:
            continue
        # Strip frontmatter if present
        if body_text.startswith("---\n"):
            end = body_text.find("\n---\n", 4)
            if end >= 0:
                body_text = body_text[end + 5 :]
        body_chars_total += len(body_text)
        md_headers = len(SPEAKER_HEADER_RE.findall(body_text))
        total_md_headers += md_headers

        # Count extracted speeches
        speeches: list[dict] = []
        walk_activities(sc.get("body", {}), speeches)
        total_speeches += len(speeches)

        if md_headers and not speeches:
            docs_no_speeches.append((md_headers, p.name))

        for sp in speeches:
            spk = sp.get("speaker") or {}
            if spk.get("name"):
                speeches_with_name += 1
            if spk.get("role"):
                speeches_with_role += 1
            if spk.get("title"):
                speeches_with_title += 1
            text = sp.get("text") or ""
            if text.strip():
                speeches_with_text += 1
                speech_text_lens.append(len(text))
                speech_text_chars_total += len(text)

        if md_headers > 0:
            per_doc_capture.append(len(speeches) / md_headers)
        if len(body_text) > 0 and speeches:
            cov = sum(len(sp.get("text") or "") for sp in speeches) / len(body_text)
            per_doc_text_cov.append(cov)

    print(f"plenary docs: {plenary_docs}")
    print("\n=== source-MD `## **NAME:**` headers ===")
    print(f"  total: {total_md_headers}")
    print(f"  extracted as speech activities: {total_speeches}")
    capture_rate = 100 * total_speeches / max(total_md_headers, 1)
    print(f"  capture rate (speeches / md_headers): {capture_rate:.1f}%")

    print("\n=== speech-record completeness ===")
    print(
        f"  speeches with speaker.name: {speeches_with_name}/{total_speeches} "
        f"({100 * speeches_with_name / max(total_speeches, 1):.1f}%)"
    )
    print(
        f"  speeches with speaker.role: {speeches_with_role}/{total_speeches} "
        f"({100 * speeches_with_role / max(total_speeches, 1):.1f}%)"
    )
    print(
        f"  speeches with speaker.title: {speeches_with_title}/{total_speeches} "
        f"({100 * speeches_with_title / max(total_speeches, 1):.1f}%)"
    )
    print(
        f"  speeches with non-empty text: {speeches_with_text}/{total_speeches} "
        f"({100 * speeches_with_text / max(total_speeches, 1):.1f}%)"
    )

    if speech_text_lens:
        speech_text_lens.sort()
        print("\n=== speech text length (chars) ===")
        print(f"  count: {len(speech_text_lens)}")
        print(f"  mean: {statistics.fmean(speech_text_lens):.0f}")
        print(f"  median: {statistics.median(speech_text_lens):.0f}")
        print(f"  p10: {speech_text_lens[len(speech_text_lens) // 10]}")
        print(f"  p90: {speech_text_lens[len(speech_text_lens) * 9 // 10]}")
        print(f"  total speech-text chars: {speech_text_chars_total:,}")
        print(f"  total plenary body chars: {body_chars_total:,}")
        print(
            f"  speech-text / body chars: "
            f"{100 * speech_text_chars_total / max(body_chars_total, 1):.1f}%"
        )

    print("\n=== per-doc capture-rate distribution ===")
    if per_doc_capture:
        per_doc_capture.sort()
        for label, q in [
            ("p10", 0.10),
            ("p25", 0.25),
            ("p50", 0.50),
            ("p75", 0.75),
            ("p90", 0.90),
        ]:
            v = per_doc_capture[int(len(per_doc_capture) * q)]
            print(f"  {label}: {v:.3f}")
    print(f"  100% bucket (rate ≥ 1.0): {sum(1 for r in per_doc_capture if r >= 1.0)}")
    print(f"  ≥0.95 bucket: {sum(1 for r in per_doc_capture if r >= 0.95)}")
    print(f"  ≥0.50 bucket: {sum(1 for r in per_doc_capture if r >= 0.50)}")
    print(f"  <0.10 bucket: {sum(1 for r in per_doc_capture if r < 0.10)}")

    if docs_no_speeches:
        docs_no_speeches.sort(reverse=True)
        print(
            f"\n=== docs with MD headers but ZERO extracted speeches: {len(docs_no_speeches)} ==="
        )
        for hdrs, name in docs_no_speeches[:10]:
            print(f"  {hdrs:5d} headers  {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
