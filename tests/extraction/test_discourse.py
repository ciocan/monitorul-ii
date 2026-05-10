"""Tests for the discourse-analysis enrichment producer.

These tests mock out OpenRouter via `httpx.MockTransport` so the
producer never makes a real HTTP call. The test surface focuses on
contract behaviour:

  * Walks every speech in a synthetic sidecar and emits one task per
    canonical speech with `text_length >= MIN_TEXT_CHARS` and word count
    `<= max_words`.
  * Honours the substantive cutoff (chair turns aren't coded).
  * Drops non-canonical speakers (`<chair narration>` / `Din sală` / etc.).
  * Skips speeches above `--max-words` with reason `text_too_long`.
  * Idempotency: identical fingerprints → reuse payload verbatim, no
    HTTP call.  `--force` overrides.
  * Conditional voice: skipped when Hawkins emits no markers.
  * Atomic write contract: a `.part` file never lingers after success.
  * `--dry-run` short-circuits before any HTTP call.
  * `-j N` dispatches via ThreadPoolExecutor.
  * `--budget-usd` cap exits cleanly with `action="budget-exhausted"`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from monitorul_ii.extraction.enrichments import discourse as disc


# ----------------------------------------------------------------------
# Stub OpenRouter service
# ----------------------------------------------------------------------


def _hawkins_payload(
    *, score: int = 0, markers: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "score": score,
        "score_unit": "ordinal_0_2",
        "framework_version": "hawkins@2018",
        "framework_confidence": 0.85,
        "markers": markers or [],
        "rationale": "Test rationale.",
    }


def _voice_payload(marker_count: int) -> dict[str, Any]:
    return {
        "classifications": [
            {
                "marker_id": f"m_{i}",
                "voice": "speaker_first_person",
                "voice_confidence": 0.9,
                "voice_evidence": None,
                "attributed_to": None,
                "rationale_short": "Voice test.",
            }
            for i in range(marker_count)
        ]
    }


def _dqi_payload() -> dict[str, Any]:
    return {
        "framework_version": "steiner-bachtiger@2017",
        "score_unit": "dqi_multi",
        "framework_confidence": 0.8,
        "level_of_justification": 1,
        "content_of_justification": "common_good",
        "respect_for_groups": 1,
        "respect_for_demands": 1,
        "respect_for_counterarguments": 1,
        "constructive_politics": "positional",
        "markers": [
            {
                "kind": "level_of_justification",
                "value": 1,
                "evidence": {"text": "test fragment"},
                "preliminary_voice": "speaker_first_person",
                "preliminary_voice_confidence": 0.9,
                "marker_confidence": 0.9,
                "rationale_short": "test",
            }
        ],
        "rationale": "DQI test rationale.",
    }


def _vparty_payload(*, score: int = 0) -> dict[str, Any]:
    return {
        "score": score,
        "score_unit": "ordinal_0_2",
        "framework_version": "vparty@2020 + vdem-attacks-on@v13",
        "framework_confidence": 0.85,
        "markers": [],
        "rationale": "V-Party test rationale.",
    }


class _StubOpenRouter:
    """Minimal stand-in for OpenRouter's `/chat/completions` + `/models`.

    Tracks every call; the per-call response is keyed by the prompt
    schema's `$id` so the stub can return a Hawkins-shaped payload for
    Hawkins calls, voice for voice, etc.

    Set `hawkins_markers=N` to make Hawkins emit N markers (which
    triggers the conditional voice call); default 0 (voice skipped).
    """

    def __init__(
        self,
        *,
        hawkins_markers: int = 0,
        fail_on: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []  # (kind, body)
        self.models_calls = 0
        self.hawkins_markers = hawkins_markers
        self.fail_on: set[str] = fail_on or set()

    def _classify_kind(self, body: dict[str, Any]) -> str:
        rf = body.get("response_format") or {}
        js = rf.get("json_schema") or {}
        name = js.get("name") or ""
        if name.startswith("hawkins"):
            return "hawkins"
        if name.startswith("voice"):
            return "voice"
        if name.startswith("dqi"):
            return "dqi"
        if name.startswith("vparty"):
            return "vparty"
        # Fallback: scan the prompt text for the schema $id.
        msgs = body.get("messages") or []
        content = ""
        for m in msgs:
            if isinstance(m, dict):
                content += str(m.get("content") or "")
        if "hawkins_populism_v1" in content:
            return "hawkins"
        if "voice_classifier_v1" in content:
            return "voice"
        if "dqi_v1" in content:
            return "dqi"
        if "vparty_antipluralism" in content:
            return "vparty"
        return "unknown"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/models":
            self.models_calls += 1
            return httpx.Response(200, json={"data": [{"id": disc.DISCOURSE_MODEL}]})
        if request.method == "POST" and request.url.path == "/chat/completions":
            body = json.loads(request.content)
            kind = self._classify_kind(body)
            self.calls.append((kind, body))
            if kind in self.fail_on:
                return httpx.Response(503, json={"error": "service unavailable"})
            if kind == "hawkins":
                # Marker text must appear in the speech excerpt so
                # `find_text_offsets` can recover offsets and the voice
                # pass actually runs.  We extract a fragment from the
                # request body's prompt text.
                msgs = body.get("messages") or []
                content = ""
                for m in msgs:
                    if isinstance(m, dict):
                        content += str(m.get("content") or "")
                # The speech fixture starts with "Stimaţi colegi"; pick
                # that as the marker text in default flow.
                marker_text = (
                    "Stimaţi colegi" if "Stimaţi colegi" in content else "speech"
                )
                markers = [
                    {
                        "kind": "people_vs_elite",
                        "evidence": {"text": marker_text},
                        "preliminary_voice": "speaker_first_person",
                        "preliminary_voice_confidence": 0.9,
                        "marker_confidence": 0.9,
                        "rationale_short": "test",
                    }
                    for _ in range(self.hawkins_markers)
                ]
                output = _hawkins_payload(score=1 if markers else 0, markers=markers)
            elif kind == "voice":
                # Count input markers from the request body.
                msgs = body.get("messages") or []
                content = ""
                for m in msgs:
                    if isinstance(m, dict):
                        content += str(m.get("content") or "")
                marker_count = content.count('"marker_id"')
                output = _voice_payload(marker_count)
            elif kind == "dqi":
                output = _dqi_payload()
            elif kind == "vparty":
                output = _vparty_payload(score=0)
            else:
                return httpx.Response(400, json={"error": "unknown kind"})
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(output, ensure_ascii=False),
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1500, "completion_tokens": 500},
                },
            )
        return httpx.Response(404)


@pytest.fixture
def stub() -> _StubOpenRouter:
    return _StubOpenRouter()


@pytest.fixture
def mock_client(stub: _StubOpenRouter) -> httpx.Client:
    transport = httpx.MockTransport(stub.handler)
    return httpx.Client(transport=transport)


# ----------------------------------------------------------------------
# Sidecar / prompt fixtures
# ----------------------------------------------------------------------


def _write_sidecar(
    tmp_path: Path,
    *,
    speeches: list[dict[str, Any]],
    document_id: str = "mo://2024/II/100",
    content_sha: str = "sha-A",
) -> Path:
    """Write a minimal sidecar with one agenda item containing the
    given speech activities. Caller passes activity dicts directly.
    """
    body = {
        "agenda_items": [
            {
                "id": f"{document_id}#agenda-1",
                "title": "Test agenda",
                "ordinal": 1,
                "activities": speeches,
            }
        ]
    }
    p = tmp_path / "doc.extraction.json"
    p.write_text(
        json.dumps(
            {
                "document_id": document_id,
                "content_sha": content_sha,
                "body": body,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


def _speech_activity(
    record_id: str, text: str, *, speaker_name: str = "Ion Popescu"
) -> dict[str, Any]:
    return {
        "id": record_id,
        "type": "speech",
        "text": text,
        "speaker": {"name": speaker_name, "raw": f"Domnul {speaker_name}"},
    }


@pytest.fixture
def prompts() -> dict[str, tuple[str, dict[str, Any]]]:
    """Real prompt + schema files (loaded once per test session)."""
    return disc._load_prompts()


@pytest.fixture
def long_speech() -> str:
    """Substantive speech text — well over MIN_TEXT_CHARS, under MAX_WORDS."""
    return "Stimaţi colegi, propunem aprobarea acestui proiect de lege. " * 20


# ----------------------------------------------------------------------
# Filename + helpers
# ----------------------------------------------------------------------


def test_discourse_filename_shape() -> None:
    assert (
        disc.discourse_filename("2024-01-15_MO-PII-1-2024")
        == "2024-01-15_MO-PII-1-2024.discourse.flash-lite.v0_1.json"
    )


def test_loader_regex_recognises_discourse_filename() -> None:
    """The indexer's loader regex must parse the producer's filename
    so the merge step picks up the discourse payload automatically.
    """
    from monitorul_ii.elasticsearch import enrichments as es_enrichments

    parsed = es_enrichments.parse_enrichment_filename(
        Path("doc.discourse.flash-lite.v0_1.json")
    )
    # The loader returns (producer, version) — producer is `discourse`,
    # not `flash-lite` (the model is the optional middle segment).
    assert parsed == ("discourse", "0.1")


# ----------------------------------------------------------------------
# Substantive / canonical-speaker filters
# ----------------------------------------------------------------------


def test_short_speech_skipped(
    tmp_path: Path, mock_client, stub: _StubOpenRouter, long_speech: str
) -> None:
    """Speeches under MIN_TEXT_CHARS aren't coded — chair turns."""
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", "Mulțumesc.")],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=mock_client,
    )
    assert result.action == "skipped"
    assert result.analyzed == 0
    # Pre-walk filtered out the only speech → no API calls beyond `/models`.
    assert all(c[0] in ("hawkins", "voice", "dqi", "vparty") for c in stub.calls)
    assert stub.calls == []


def test_non_canonical_speaker_skipped(
    tmp_path: Path, mock_client, stub: _StubOpenRouter, long_speech: str
) -> None:
    """Speeches under `<chair narration>` / `Din sală` / `Voci` aren't coded."""
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[
            {
                "id": "mo://2024/II/100#agenda-1#act-1",
                "type": "speech",
                "text": long_speech,
                "speaker": {"name": "<chair narration>", "raw": "<chair narration>"},
            },
            {
                "id": "mo://2024/II/100#agenda-1#act-2",
                "type": "speech",
                "text": long_speech,
                "speaker": {"name": "Din sală", "raw": "Din sală"},
            },
            _speech_activity("mo://2024/II/100#agenda-1#act-3", long_speech),
        ],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=mock_client,
    )
    assert result.action == "analyzed"
    assert result.analyzed == 1  # only the canonical speaker
    payload = json.loads(result.file_path.read_text(encoding="utf-8"))
    assert "mo://2024/II/100#agenda-1#act-3" in payload
    assert "mo://2024/II/100#agenda-1#act-1" not in payload
    assert "mo://2024/II/100#agenda-1#act-2" not in payload


def test_max_words_long_tail_skipped(
    tmp_path: Path, mock_client, stub: _StubOpenRouter
) -> None:
    """Speeches above max_words are deferred (long-tail v0.2 task)."""
    long = "cuvânt " * 1000  # 1000 words
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long)],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        max_words=500,
        client=mock_client,
    )
    # No coding-eligible speeches after long-tail filter.
    assert result.skipped == 1
    assert result.action == "skipped"
    assert stub.calls == []


def test_non_speech_activities_ignored(
    tmp_path: Path, mock_client, stub: _StubOpenRouter, long_speech: str
) -> None:
    """Vote / procedural / narrator activities aren't coded."""
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[
            {
                "id": "mo://2024/II/100#agenda-1#vote-1",
                "type": "vote",
                "text": "Votul. " * 30,
            },
            _speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech),
        ],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=mock_client,
    )
    assert result.analyzed == 1
    payload = json.loads(result.file_path.read_text(encoding="utf-8"))
    assert "mo://2024/II/100#agenda-1#act-1" in payload
    assert "mo://2024/II/100#agenda-1#vote-1" not in payload


# ----------------------------------------------------------------------
# Pipeline order + voice conditional
# ----------------------------------------------------------------------


def test_full_pipeline_runs_when_hawkins_emits_markers(
    tmp_path: Path, long_speech: str
) -> None:
    """Hawkins emits markers → voice + dqi + vparty all run (4 calls)."""
    stub = _StubOpenRouter(hawkins_markers=2)
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)

    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=client,
    )
    assert result.action == "analyzed"
    kinds = [c[0] for c in stub.calls]
    # Hawkins → voice → dqi → vparty
    assert kinds == ["hawkins", "voice", "dqi", "vparty"]
    payload = json.loads(result.file_path.read_text(encoding="utf-8"))
    entry = payload["mo://2024/II/100#agenda-1#act-1"]
    assert entry["hawkins"] is not None
    assert entry["voice"] is not None
    assert entry["dqi"] is not None
    assert entry["vparty"] is not None


def test_voice_skipped_when_hawkins_empty(tmp_path: Path, long_speech: str) -> None:
    """Hawkins emits 0 markers → voice is skipped (3 API calls, not 4)."""
    stub = _StubOpenRouter(hawkins_markers=0)
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)

    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=client,
    )
    assert result.action == "analyzed"
    kinds = [c[0] for c in stub.calls]
    assert "voice" not in kinds
    assert kinds == ["hawkins", "dqi", "vparty"]
    payload = json.loads(result.file_path.read_text(encoding="utf-8"))
    entry = payload["mo://2024/II/100#agenda-1#act-1"]
    assert entry["voice"] is None
    assert entry["hawkins"] is not None
    assert entry["dqi"] is not None
    assert entry["vparty"] is not None


def test_meta_block_carries_provenance(
    tmp_path: Path, mock_client, long_speech: str
) -> None:
    """Each entry's `_meta` carries namespace / producer / version /
    model_id / source_sidecar_content_sha / prompt_versions.
    """
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=mock_client,
    )
    payload = json.loads(result.file_path.read_text(encoding="utf-8"))
    entry = payload["mo://2024/II/100#agenda-1#act-1"]
    meta = entry["_meta"]
    assert meta["namespace"] == disc.DISCOURSE_NAMESPACE
    assert meta["producer"] == disc.DISCOURSE_PRODUCER
    assert meta["version"] == disc.DISCOURSE_VERSION_DOTTED
    assert meta["model_id"] == disc.DISCOURSE_MODEL
    assert meta["source_sidecar_content_sha"] == "sha-A"
    assert meta["prompt_versions"] == disc.PROMPT_VERSIONS
    assert isinstance(entry["text_fingerprint"], str)
    assert len(entry["text_fingerprint"]) == 12


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


def test_idempotent_reuse_when_fingerprint_matches(
    tmp_path: Path, mock_client, stub: _StubOpenRouter, long_speech: str
) -> None:
    """Re-running with identical text → fingerprint match → no API calls."""
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    first = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=mock_client,
    )
    assert first.action == "analyzed"
    first_calls = len(stub.calls)

    second = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=mock_client,
    )
    assert second.action == "skipped"
    assert second.reused == 1
    assert second.analyzed == 0
    # No new API calls.
    assert len(stub.calls) == first_calls


def test_force_overrides_fingerprint(
    tmp_path: Path, mock_client, stub: _StubOpenRouter, long_speech: str
) -> None:
    """`--force` re-codes every record regardless of fingerprint match."""
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=mock_client,
    )
    initial_calls = len(stub.calls)

    second = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=mock_client,
        force=True,
    )
    assert second.action == "analyzed"
    assert second.analyzed == 1
    assert second.reused == 0
    assert len(stub.calls) > initial_calls


def test_text_change_triggers_recoding(tmp_path: Path) -> None:
    """When a record's text changes, fingerprint mismatch triggers re-coding."""
    stub = _StubOpenRouter(hawkins_markers=0)
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)

    sidecar = _write_sidecar(
        tmp_path,
        speeches=[
            _speech_activity(
                "mo://2024/II/100#agenda-1#act-1",
                "First version of the speech. " * 10,
            )
        ],
    )
    first = disc.analyze_sidecar(
        sidecar, openrouter_url="http://stub", api_key="sk-test", client=client
    )
    assert first.analyzed == 1
    initial_calls = len(stub.calls)

    sidecar.write_text(
        json.dumps(
            {
                "document_id": "mo://2024/II/100",
                "content_sha": "sha-B",
                "body": {
                    "agenda_items": [
                        {
                            "id": "mo://2024/II/100#agenda-1",
                            "title": "Test",
                            "ordinal": 1,
                            "activities": [
                                _speech_activity(
                                    "mo://2024/II/100#agenda-1#act-1",
                                    "Different version of the speech. " * 10,
                                )
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    second = disc.analyze_sidecar(
        sidecar, openrouter_url="http://stub", api_key="sk-test", client=client
    )
    assert second.analyzed == 1
    assert second.reused == 0
    assert len(stub.calls) > initial_calls


# ----------------------------------------------------------------------
# Atomic write
# ----------------------------------------------------------------------


def test_atomic_write_no_part_lingers(
    tmp_path: Path, mock_client, long_speech: str
) -> None:
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=mock_client,
    )
    assert result.action == "analyzed"
    files = list(tmp_path.iterdir())
    assert all(not f.name.endswith(".part") for f in files)


# ----------------------------------------------------------------------
# Dry-run
# ----------------------------------------------------------------------


def test_dry_run_skips_service(
    tmp_path: Path, mock_client, stub: _StubOpenRouter, long_speech: str
) -> None:
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=mock_client,
        dry_run=True,
    )
    assert result.action == "dry-run"
    assert result.analyzed == 1
    assert stub.calls == []
    # The file was not written.
    assert result.file_path is not None
    assert not result.file_path.exists()


# ----------------------------------------------------------------------
# Budget cap
# ----------------------------------------------------------------------


def test_budget_cap_stops_cleanly(tmp_path: Path, long_speech: str) -> None:
    """A very low budget cap makes the producer exit cleanly mid-sidecar.

    The first speech burns ~$0.0007 in the stub (1500 in × $0.10/M +
    500 out × $0.40/M = $0.000350 per call × 3 calls = $0.00105),
    enough to exceed a $0.0005 cap after the first speech finishes —
    so the second speech is never coded.
    """
    stub = _StubOpenRouter(hawkins_markers=0)
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)

    sidecar = _write_sidecar(
        tmp_path,
        speeches=[
            _speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech),
            _speech_activity("mo://2024/II/100#agenda-1#act-2", long_speech + " B"),
        ],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=client,
        budget_remaining_usd=0.0005,
    )
    assert result.action == "budget-exhausted"
    assert result.analyzed == 1  # first speech finished atomically
    # Output was still persisted for the first speech.
    payload = json.loads(result.file_path.read_text(encoding="utf-8"))
    assert "mo://2024/II/100#agenda-1#act-1" in payload
    # No `.part` file leftover.
    files = list(tmp_path.iterdir())
    assert all(not f.name.endswith(".part") for f in files)


# ----------------------------------------------------------------------
# Service errors
# ----------------------------------------------------------------------


def test_service_failure_does_not_persist_partial_entry(
    tmp_path: Path, long_speech: str
) -> None:
    """Hawkins call fails → record dropped from this run so re-runs retry it.

    The v0.1.0 contract was "persist with errors[]" but that broke
    re-runs: the fingerprint match short-circuited retries forever.
    The fixed contract is: failed records are NOT written. The next
    run will see no fingerprint match and re-code cleanly.
    """
    stub = _StubOpenRouter(hawkins_markers=0, fail_on={"hawkins"})
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)

    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=client,
        retry_on_error=0,
    )
    # Action reflects "everything failed". No discourse file is created.
    assert result.action == "all-failed"
    assert result.analyzed == 0
    assert result.failed == 1
    assert not result.file_path.exists()


def test_failed_records_retried_on_rerun(tmp_path: Path, long_speech: str) -> None:
    """First run: hawkins fails → record absent from output.
    Second run: hawkins succeeds → record now persisted.
    """
    stub = _StubOpenRouter(hawkins_markers=0, fail_on={"hawkins"})
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)

    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    first = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=client,
        retry_on_error=0,
    )
    assert first.action == "all-failed"
    assert first.analyzed == 0

    # Second stub: hawkins now succeeds.
    stub2 = _StubOpenRouter(hawkins_markers=0)
    transport2 = httpx.MockTransport(stub2.handler)
    client2 = httpx.Client(transport=transport2)
    second = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=client2,
        retry_on_error=0,
    )
    assert second.action == "analyzed"
    assert second.analyzed == 1
    payload = json.loads(second.file_path.read_text(encoding="utf-8"))
    entry = payload["mo://2024/II/100#agenda-1#act-1"]
    assert entry["hawkins"] is not None
    assert entry["dqi"] is not None
    assert entry["vparty"] is not None


def test_missing_api_key_returns_error(
    tmp_path: Path, mock_client, long_speech: str
) -> None:
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key=None,
        client=mock_client,
    )
    assert result.action == "error"
    assert any("OPENROUTER_API_KEY" in e for e in result.errors)


# ----------------------------------------------------------------------
# CLI smoke tests
# ----------------------------------------------------------------------


def test_analyze_cli_parser_includes_all_flags() -> None:
    """The argparse setup exposes every flag from the handoff spec."""
    from monitorul_ii.cli import _build_parser

    parser = _build_parser()
    # Parse with the analyze subcommand and every flag set to a value.
    args = parser.parse_args(
        [
            "analyze",
            "/tmp/x",
            "--force",
            "--dry-run",
            "--limit",
            "5",
            "--reverse",
            "--openrouter-url",
            "http://stub",
            "--retry-on-error",
            "2",
            "--max-words",
            "500",
            "--budget-usd",
            "10.5",
            "-j",
            "4",
            "--no-upload",
        ]
    )
    assert args.command == "analyze"
    assert args.force is True
    assert args.dry_run is True
    assert args.limit == 5
    assert args.reverse is True
    assert args.openrouter_url == "http://stub"
    assert args.retry_on_error == 2
    assert args.max_words == 500
    assert args.budget_usd == 10.5
    assert args.workers == 4
    assert args.no_upload is True


def test_analyze_cli_dry_run_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, long_speech: str
) -> None:
    """`--dry-run` calls `analyze_sidecar` with `dry_run=True`, never
    contacting OpenRouter (no env var needed).
    """
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )

    from monitorul_ii.cli import cmd_analyze

    args = argparse.Namespace(
        paths=[sidecar],
        force=False,
        dry_run=True,
        limit=None,
        reverse=False,
        openrouter_url="http://stub",
        retry_on_error=0,
        max_words=None,
        budget_usd=None,
        workers=1,
        no_upload=True,
        bucket=None,
    )
    # Patch healthcheck so we don't try to actually call OpenRouter
    # (dry_run skips that path anyway, but defence in depth).
    rc = cmd_analyze(args)
    assert rc == 0
    # No discourse file should land on disk in dry-run mode.
    expected = sidecar.parent / disc.discourse_filename("doc")
    assert not expected.exists()


def test_analyze_cli_workers_threaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, long_speech: str
) -> None:
    """`-j 4` dispatches via ThreadPoolExecutor — verify the cmd_analyze
    handler accepts the threaded path without raising.
    """
    stub = _StubOpenRouter(hawkins_markers=0)
    transport = httpx.MockTransport(stub.handler)

    # Patch the global httpx.Client used by cmd_analyze.
    real_client = httpx.Client

    def _factory(*a, **kw):
        return real_client(*a, transport=transport, **kw)

    monkeypatch.setattr(httpx, "Client", _factory)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    sidecars = []
    for i in range(3):
        sub = tmp_path / f"d{i}"
        sub.mkdir()
        body = {
            "agenda_items": [
                {
                    "id": f"mo://2024/II/{i + 100}#agenda-1",
                    "title": "Test",
                    "ordinal": 1,
                    "activities": [
                        _speech_activity(
                            f"mo://2024/II/{i + 100}#agenda-1#act-1", long_speech
                        )
                    ],
                }
            ]
        }
        p = sub / "doc.extraction.json"
        p.write_text(
            json.dumps(
                {
                    "document_id": f"mo://2024/II/{i + 100}",
                    "content_sha": f"sha-{i}",
                    "body": body,
                }
            ),
            encoding="utf-8",
        )
        sidecars.append(p)

    from monitorul_ii.cli import cmd_analyze

    args = argparse.Namespace(
        paths=sidecars,
        force=False,
        dry_run=False,
        limit=None,
        reverse=False,
        openrouter_url="http://stub",
        retry_on_error=0,
        max_words=None,
        budget_usd=None,
        workers=4,
        no_upload=True,
        bucket=None,
    )
    rc = cmd_analyze(args)
    assert rc == 0
    # All three sidecars produced a discourse file.
    for p in sidecars:
        target = p.parent / disc.discourse_filename(p.stem.replace(".extraction", ""))
        # The basename strip drops `.extraction` leaving `doc`.
        target = p.parent / "doc.discourse.flash-lite.v0_1.json"
        assert target.exists()


# ----------------------------------------------------------------------
# Healthcheck
# ----------------------------------------------------------------------


def test_healthcheck_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": [{"id": "x"}, {"id": "y"}]})
    )
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda *a, **kw: real_client(*a, transport=transport, **kw)
    )
    ok, detail = disc.healthcheck("http://stub", api_key="sk-test")
    assert ok
    assert "2 models" in detail


def test_partial_flush_every_n_records(
    tmp_path: Path, mock_client, long_speech: str
) -> None:
    """Periodic atomic flush bounds Ctrl+C data loss to PARTIAL_FLUSH_EVERY records.

    Verifies the discourse file exists with partial entries midway
    through a long sidecar — a worker crashing between flush windows
    would only lose at most PARTIAL_FLUSH_EVERY records.
    """
    n_speeches = disc.PARTIAL_FLUSH_EVERY * 3 + 2  # 3 flushes + 2 trailing
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[
            _speech_activity(f"mo://2024/II/100#agenda-1#act-{i}", long_speech)
            for i in range(1, n_speeches + 1)
        ],
    )
    target = sidecar.parent / disc.discourse_filename("doc")

    flush_snapshots: list[int] = []
    real_atomic_write = disc._atomic_write_json

    def _spy(path, payload):
        if path == target:
            flush_snapshots.append(len(payload))
        return real_atomic_write(path, payload)

    import monitorul_ii.extraction.enrichments.discourse as disc_mod

    real = disc_mod._atomic_write_json
    disc_mod._atomic_write_json = _spy
    try:
        result = disc.analyze_sidecar(
            sidecar,
            openrouter_url="http://stub",
            api_key="sk-test",
            client=mock_client,
        )
    finally:
        disc_mod._atomic_write_json = real

    assert result.action == "analyzed"
    # Expect periodic flushes at 5, 10, 15 records + final at 17.
    assert len(flush_snapshots) >= 3, (
        f"expected ≥3 flush events for {n_speeches} records "
        f"(PARTIAL_FLUSH_EVERY={disc.PARTIAL_FLUSH_EVERY}), got {flush_snapshots}"
    )
    # The last flush must contain ALL records.
    assert flush_snapshots[-1] == n_speeches
    # Earlier flushes must be monotonically growing.
    assert flush_snapshots == sorted(flush_snapshots)


def test_keyboard_interrupt_flushes_partial(tmp_path: Path, long_speech: str) -> None:
    """A KeyboardInterrupt mid-sidecar still flushes the in-memory entries.

    Simulates Ctrl+C by raising KeyboardInterrupt from the stub on the
    nth call. The producer's BaseException-handler must flush whatever
    new_entries it has accumulated before re-raising.
    """
    interrupt_after = 2  # raise on the 3rd speech's hawkins call

    class _InterruptStub(_StubOpenRouter):
        def __init__(self) -> None:
            super().__init__(hawkins_markers=0)
            self._speeches_processed = 0

        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/chat/completions":
                kind = self._classify_kind(json.loads(request.content))
                if kind == "hawkins":
                    self._speeches_processed += 1
                    if self._speeches_processed > interrupt_after:
                        raise KeyboardInterrupt("simulated Ctrl+C")
            return super().handler(request)

    stub = _InterruptStub()
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)

    sidecar = _write_sidecar(
        tmp_path,
        speeches=[
            _speech_activity(f"mo://2024/II/100#agenda-1#act-{i}", long_speech)
            for i in range(1, 6)
        ],
    )

    with pytest.raises(KeyboardInterrupt):
        disc.analyze_sidecar(
            sidecar,
            openrouter_url="http://stub",
            api_key="sk-test",
            client=client,
        )

    # File must exist with the records that completed before the interrupt.
    target = sidecar.parent / disc.discourse_filename("doc")
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert len(payload) == interrupt_after, (
        f"expected {interrupt_after} flushed records before Ctrl+C, got {len(payload)}"
    )


def test_log_file_writes_one_line_per_speech(
    tmp_path: Path, mock_client, long_speech: str
) -> None:
    """`--log-file` mode appends one JSON line per coded speech for live tail-f."""
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[
            _speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech),
            _speech_activity("mo://2024/II/100#agenda-1#act-2", long_speech + " B"),
        ],
    )
    log_path = tmp_path / "analyze.jsonl"
    log = disc.JsonlLogger(log_path)
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=mock_client,
        log=log,
    )
    assert result.action == "analyzed"
    assert log_path.exists()
    lines = [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]
    assert len(lines) == 2
    # Each entry carries the canonical telemetry fields.
    for entry in lines:
        assert entry["sidecar"] == sidecar.name
        assert entry["record_id"].startswith("mo://2024/II/100#agenda-1#act-")
        assert entry["outcome"] == "ok"
        assert "hawkins_score" in entry
        assert "vparty_score" in entry
        assert "dqi_level" in entry
        assert "voice_ran" in entry
        assert "tokens_in" in entry
        assert "cost_usd" in entry
        assert "calls" in entry


def test_log_file_records_failed_outcome(tmp_path: Path, long_speech: str) -> None:
    """Failed records still get logged (outcome='failed')."""
    stub = _StubOpenRouter(hawkins_markers=0, fail_on={"hawkins"})
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    log_path = tmp_path / "analyze.jsonl"
    log = disc.JsonlLogger(log_path)
    disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=client,
        retry_on_error=0,
        log=log,
    )
    lines = [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["outcome"] == "failed"
    assert lines[0]["errors"]


class _StubGoogleAiStudio:
    """Stand-in for Google AI Studio's native Gemini API.

    Different request/response shape than OpenRouter:
    - GET /models?key=… returns {models: [...]}
    - POST /models/{model}:generateContent?key=… takes
      {contents, generationConfig} and returns {candidates, usageMetadata}.
    """

    def __init__(self, *, hawkins_markers: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.models_calls = 0
        self.hawkins_markers = hawkins_markers

    def _classify_kind(self, body: dict[str, Any]) -> str:
        # Google body has `contents[].parts[].text` instead of messages.
        # Match on the schema `$id` literal (which appears verbatim in the
        # JSON Schema embedded in the prompt) — substring matching on the
        # prompt's prose hits cross-references between frameworks.
        contents = body.get("contents") or []
        text = ""
        for c in contents:
            for p in c.get("parts") or []:
                if "text" in p:
                    text += p["text"]
        # Check the most-specific schema-id markers first.
        if '"$id": "vparty_antipluralism_v2"' in text:
            return "vparty"
        if '"$id": "dqi_v1"' in text:
            return "dqi"
        if '"$id": "voice_classifier_v1"' in text:
            return "voice"
        if '"$id": "hawkins_populism_v1"' in text:
            return "hawkins"
        return "unknown"

    def handler(self, request: httpx.Request) -> httpx.Response:
        # Models list endpoint (healthcheck).
        if request.method == "GET" and "/models" in request.url.path:
            self.models_calls += 1
            return httpx.Response(
                200, json={"models": [{"name": "models/gemini-3.1-flash-lite"}]}
            )
        # generateContent endpoint
        if request.method == "POST" and ":generateContent" in request.url.path:
            body = json.loads(request.content)
            kind = self._classify_kind(body)
            self.calls.append((kind, body))
            if kind == "hawkins":
                marker_text = "Stimaţi colegi"
                markers = [
                    {
                        "kind": "people_vs_elite",
                        "evidence": {"text": marker_text},
                        "preliminary_voice": "speaker_first_person",
                        "preliminary_voice_confidence": 0.9,
                        "marker_confidence": 0.9,
                        "rationale_short": "test",
                    }
                    for _ in range(self.hawkins_markers)
                ]
                output = _hawkins_payload(score=1 if markers else 0, markers=markers)
            elif kind == "voice":
                # Voice prompt for Google never fires unless Hawkins emits markers.
                output = _voice_payload(self.hawkins_markers)
            elif kind == "dqi":
                output = _dqi_payload()
            elif kind == "vparty":
                output = _vparty_payload(score=0)
            else:
                return httpx.Response(400, json={"error": "unknown kind"})
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": json.dumps(output, ensure_ascii=False)}
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 1500,
                        "candidatesTokenCount": 500,
                        "totalTokenCount": 2000,
                    },
                },
            )
        return httpx.Response(404)


def test_google_provider_codes_speech_end_to_end(
    tmp_path: Path, long_speech: str
) -> None:
    """Provider=google routes through Google AI Studio's native API and
    produces clean entries with the same _meta + framework keys.
    """
    stub = _StubGoogleAiStudio(hawkins_markers=0)
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)

    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    result = disc.analyze_sidecar(
        sidecar,
        provider=disc.PROVIDER_GOOGLE,
        base_url="http://stub",
        api_key="test-google-key",
        client=client,
    )
    assert result.action == "analyzed"
    assert result.analyzed == 1
    # Hawkins=0 markers → voice should have been skipped.
    kinds = [c[0] for c in stub.calls]
    assert "voice" not in kinds
    assert kinds == ["hawkins", "dqi", "vparty"]
    payload = json.loads(result.file_path.read_text(encoding="utf-8"))
    entry = payload["mo://2024/II/100#agenda-1#act-1"]
    assert entry["_meta"]["model_id"] == "gemini-3.1-flash-lite"  # the Google default
    assert entry["hawkins"] is not None
    assert entry["dqi"] is not None
    assert entry["vparty"] is not None


def test_google_provider_uses_google_cost_rate(
    tmp_path: Path, long_speech: str
) -> None:
    """Cost is computed from the Google rates (0.075/M in + 0.30/M out),
    not OpenRouter's (0.10/M + 0.40/M). At 1500 in + 500 out per call,
    3 calls → 4500 in + 1500 out total → $0.000338 + $0.00045 = $0.000788.
    """
    stub = _StubGoogleAiStudio(hawkins_markers=0)
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)

    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    result = disc.analyze_sidecar(
        sidecar,
        provider=disc.PROVIDER_GOOGLE,
        base_url="http://stub",
        api_key="k",
        client=client,
    )
    # 3 calls × (1500*0.075/M + 500*0.30/M) = 3 * (0.0001125 + 0.00015) = $0.000788
    expected = 3 * (
        1500 * disc.GOOGLE_FLASH_LITE_INPUT_RATE_USD
        + 500 * disc.GOOGLE_FLASH_LITE_OUTPUT_RATE_USD
    )
    assert abs(result.cost_estimate_usd - expected) < 1e-9


def test_google_healthcheck_uses_query_string_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the Google healthcheck sends `?key=…` not Authorization header."""
    seen_url: list[str] = []
    seen_auth: list[str | None] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        seen_url.append(str(req.url))
        seen_auth.append(req.headers.get("authorization"))
        return httpx.Response(
            200, json={"models": [{"name": "models/gemini-3.1-flash-lite"}]}
        )

    transport = httpx.MockTransport(_handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda *a, **kw: real_client(*a, transport=transport, **kw)
    )
    ok, detail = disc.healthcheck(
        api_key="my-google-key",
        provider=disc.PROVIDER_GOOGLE,
        base_url="http://stub/v1beta",
    )
    assert ok
    assert "1 models" in detail
    # Auth via query string only — no bearer header
    assert "key=my-google-key" in seen_url[0]
    assert seen_auth[0] is None


def test_unsupported_provider_returns_error(tmp_path: Path, long_speech: str) -> None:
    """Bad provider value yields action=error before any HTTP call."""
    sidecar = _write_sidecar(
        tmp_path,
        speeches=[_speech_activity("mo://2024/II/100#agenda-1#act-1", long_speech)],
    )
    result = disc.analyze_sidecar(
        sidecar,
        provider="bogus-provider",
        api_key="k",
    )
    assert result.action == "error"
    assert any("unsupported provider" in e for e in result.errors)


def test_healthcheck_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(401, text="unauthorized")
    )
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda *a, **kw: real_client(*a, transport=transport, **kw)
    )
    ok, detail = disc.healthcheck("http://stub", api_key="sk-bad")
    assert not ok
    assert detail


# ----------------------------------------------------------------------
# Typography-tolerant evidence-text matching
# ----------------------------------------------------------------------


def test_find_text_offsets_strict_byte_exact() -> None:
    speech = "Stimaţi colegi, suveranitatea aparţine poporului."
    fragment = "suveranitatea aparţine poporului"
    result = disc.find_text_offsets(speech, fragment)
    assert result is not None
    start, end = result
    assert speech[start:end] == fragment


def test_find_text_offsets_strict_rejects_paraphrase() -> None:
    speech = 'El a spus „suveranitatea aparţine poporului".'
    fragment = '"suveranitatea aparține poporului"'
    assert disc.find_text_offsets(speech, fragment) is None


def test_tolerant_byte_exact_uses_fast_path() -> None:
    speech = "Această lege este o lovitură directă."
    fragment = "lovitură directă"
    result = disc.find_text_offsets_tolerant(speech, fragment)
    assert result == [speech.index(fragment), speech.index(fragment) + len(fragment)]
    assert speech[result[0] : result[1]] == fragment


def test_tolerant_recovers_curly_quotes() -> None:
    speech = 'Citez: „statul paralel există" — aici este pericolul.'
    fragment = '"statul paralel există"'
    result = disc.find_text_offsets_tolerant(speech, fragment)
    assert result is not None
    start, end = result
    assert speech[start:end] == '„statul paralel există"'


def test_tolerant_recovers_em_dash() -> None:
    speech = "Aceasta — atenție — este lovitura finală."
    fragment = "Aceasta - atenție - este lovitura finală."
    result = disc.find_text_offsets_tolerant(speech, fragment)
    assert result is not None
    start, end = result
    assert speech[start:end] == "Aceasta — atenție — este lovitura finală."


def test_tolerant_recovers_collapsed_newlines() -> None:
    speech = "Această lege\neste o lovitură\ndirectă împotriva poporului."
    fragment = "Această lege este o lovitură directă împotriva poporului."
    result = disc.find_text_offsets_tolerant(speech, fragment)
    assert result is not None
    start, end = result
    assert (
        speech[start:end]
        == "Această lege\neste o lovitură\ndirectă împotriva poporului."
    )


def test_tolerant_recovers_ellipsis_glyph() -> None:
    speech = "Eu nu spun că… dar cred că ei mint."
    fragment = "Eu nu spun că... dar cred că ei mint."
    result = disc.find_text_offsets_tolerant(speech, fragment)
    assert result is not None
    start, end = result
    assert speech[start:end] == "Eu nu spun că… dar cred că ei mint."


def test_tolerant_recovers_case_drift() -> None:
    """Real-world failure: Hawkins emits `un ministru ...` (lowercase
    sentence start) for a speech that says `Un ministru ...` (capital).
    Case is typography, not content — the matcher must recover this.
    """
    speech = "Ce avem aici? Un ministru al economiei care distruge economia."
    fragment = "un ministru al economiei care distruge economia"
    result = disc.find_text_offsets_tolerant(speech, fragment)
    assert result is not None
    start, end = result
    assert speech[start:end] == "Un ministru al economiei care distruge economia"


def test_tolerant_recovers_combined_case_and_typography() -> None:
    speech = 'El a strigat: „Statul Paralel Există"!'
    fragment = '"statul paralel există"'
    result = disc.find_text_offsets_tolerant(speech, fragment)
    assert result is not None
    start, end = result
    assert speech[start:end] == '„Statul Paralel Există"'


def test_tolerant_rejects_real_paraphrase() -> None:
    speech = "Justiția aparţine poporului român."
    # Word added — this is real paraphrase, not typography drift.
    fragment = "Justiția aparține întregului popor român."
    assert disc.find_text_offsets_tolerant(speech, fragment) is None


def test_tolerant_empty_inputs() -> None:
    assert disc.find_text_offsets_tolerant("", "anything") is None
    assert disc.find_text_offsets_tolerant("speech", "") is None
    # Whitespace-only fragment normalises to empty → unmatchable.
    assert disc.find_text_offsets_tolerant("speech", "   \n  ") is None


def test_tolerant_offsets_correctly_span_glyph_at_end() -> None:
    # End of fragment lands on a folded glyph: the tolerant matcher must
    # use the source-end (exclusive) of the LAST matched normalised char,
    # not its source-start, otherwise the returned slice clips the glyph.
    speech = 'Soros zice „statul paralel"'
    fragment = '"statul paralel"'
    result = disc.find_text_offsets_tolerant(speech, fragment)
    assert result is not None
    start, end = result
    assert speech[start:end] == '„statul paralel"'


# ----------------------------------------------------------------------
# Voice prompt builder: marker fallback when offsets aren't recoverable
# ----------------------------------------------------------------------


def test_voice_prompt_includes_char_range_when_offsets_recovered() -> None:
    speech = "Această lege este o lovitură directă."
    markers = [
        {
            "kind": "evil_elite",
            "evidence": {"text": "lovitură directă"},
        }
    ]
    schema = {"type": "object"}
    out = disc.build_prompt_for_voice("PROMPT", speech, markers, schema)
    assert out, "voice prompt must be non-empty"
    payload = json.loads(out.split("```json", 2)[1].split("```", 1)[0])
    assert payload["markers"][0]["marker_text"] == "lovitură directă"
    assert "char_range" in payload["markers"][0]
    start, end = payload["markers"][0]["char_range"]
    assert speech[start:end] == "lovitură directă"


def test_voice_prompt_includes_char_range_after_typography_fold() -> None:
    speech = 'El a spus „statul paralel" — pericol.'
    markers = [
        {
            "kind": "evil_elite",
            "evidence": {"text": '"statul paralel"'},
        }
    ]
    schema = {"type": "object"}
    out = disc.build_prompt_for_voice("PROMPT", speech, markers, schema)
    assert out, "voice prompt must be non-empty after typography fold"
    payload = json.loads(out.split("```json", 2)[1].split("```", 1)[0])
    assert "char_range" in payload["markers"][0]
    start, end = payload["markers"][0]["char_range"]
    assert speech[start:end] == '„statul paralel"'


def test_voice_prompt_keeps_marker_when_offsets_unrecoverable() -> None:
    # Real paraphrase: the marker text isn't a substring of the speech
    # even after typography folding. Pre-fix, the marker was dropped and
    # an all-paraphrased marker list returned "" → voice silently
    # skipped → entry marked failed. Post-fix the marker is included
    # WITHOUT char_range so the voice classifier still gets the work.
    speech = "Această lege este lovitura finală împotriva poporului."
    markers = [
        {
            "kind": "evil_elite",
            "evidence": {"text": "Această lege e lovitura definitivă a poporului."},
        }
    ]
    schema = {"type": "object"}
    out = disc.build_prompt_for_voice("PROMPT", speech, markers, schema)
    assert out, (
        "voice prompt MUST be non-empty even when the only marker has "
        "an unrecoverable evidence.text — voice must still run"
    )
    payload = json.loads(out.split("```json", 2)[1].split("```", 1)[0])
    assert len(payload["markers"]) == 1
    assert (
        payload["markers"][0]["marker_text"]
        == "Această lege e lovitura definitivă a poporului."
    )
    assert "char_range" not in payload["markers"][0]


def test_voice_prompt_mixes_recoverable_and_unrecoverable_markers() -> None:
    speech = "Această lege este o lovitură directă împotriva poporului."
    markers = [
        # Recoverable verbatim.
        {"kind": "evil_elite", "evidence": {"text": "lovitură directă"}},
        # Real paraphrase — shape unrecoverable, but voice should still
        # see the marker so it can classify (e.g. as paraphrase / quoted).
        {
            "kind": "homogeneous_people",
            "evidence": {"text": "împotriva poporului român întreg"},
        },
    ]
    schema = {"type": "object"}
    out = disc.build_prompt_for_voice("PROMPT", speech, markers, schema)
    assert out
    payload = json.loads(out.split("```json", 2)[1].split("```", 1)[0])
    assert len(payload["markers"]) == 2
    assert "char_range" in payload["markers"][0]
    assert "char_range" not in payload["markers"][1]
    # marker_id ordering must follow the input.
    assert [m["marker_id"] for m in payload["markers"]] == ["m_0", "m_1"]


def test_voice_prompt_drops_markers_with_empty_evidence_text() -> None:
    speech = "Speech text here."
    markers = [
        {"kind": "evil_elite", "evidence": {"text": ""}},
        {"kind": "evil_elite", "evidence": {}},
        {"kind": "evil_elite"},
    ]
    schema = {"type": "object"}
    out = disc.build_prompt_for_voice("PROMPT", speech, markers, schema)
    # All markers have no usable text — payload is empty, signal-skip.
    assert out == ""


def test_voice_prompt_skips_non_dict_markers() -> None:
    """Defensive guard: a non-dict marker (e.g. a stray string from a
    malformed model output that escaped the validation gate) must not
    crash the voice pass — skip it and let the rest classify.
    Pre-fix this was the source of the
    `AttributeError: 'str' object has no attribute 'get'` regression
    on `2021-09-22_MO-PII-131-2021` and similar sidecars where the model
    emitted Hawkins's `markers` as a string instead of an array.
    """
    speech = "Speech text here with a quoted lovitură directă inside."
    markers = [
        "stray string that should be skipped",
        12345,
        None,
        {"kind": "evil_elite", "evidence": {"text": "lovitură directă"}},
    ]
    schema = {"type": "object"}
    out = disc.build_prompt_for_voice("PROMPT", speech, markers, schema)
    assert out, "valid marker should still produce a prompt"
    payload = json.loads(out.split("```json", 2)[1].split("```", 1)[0])
    assert len(payload["markers"]) == 1
    assert payload["markers"][0]["marker_text"] == "lovitură directă"


def test_pipeline_skips_hawkins_when_validation_failed_with_bogus_markers(
    long_speech: str,
    prompts: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    """End-to-end regression: when Hawkins emits a malformed shape
    (e.g. `markers` as a string instead of an array), validation
    rejects it AND salvage can't recover (no list to filter from).
    The bogus output MUST NOT propagate into `results["hawkins"]` —
    voice pass must skip cleanly. Pre-fix this pattern crashed the
    entire sidecar with `AttributeError: 'str' object has no attribute
    'get'`.
    """

    class _BogusMarkersStub(_StubOpenRouter):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/models":
                return httpx.Response(
                    200, json={"data": [{"id": disc.DISCOURSE_MODEL}]}
                )
            body = json.loads(request.content)
            kind = self._classify_kind(body)
            self.calls.append((kind, body))
            if kind == "hawkins":
                # Bogus shape: markers as a string. Schema validation
                # rejects (markers must be array); salvage can't help
                # (it filters lists; this isn't a list).
                output = {
                    "score": 1,
                    "score_unit": "ordinal_0_2",
                    "framework_version": "hawkins@2018",
                    "framework_confidence": 0.85,
                    "markers": "this should be a list but isn't",
                    "rationale": "Test.",
                }
            elif kind == "voice":
                output = _voice_payload(0)
            elif kind == "dqi":
                output = _dqi_payload()
            elif kind == "vparty":
                output = _vparty_payload(score=0)
            else:
                return httpx.Response(400, json={"error": "unknown kind"})
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(output, ensure_ascii=False),
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1500, "completion_tokens": 500},
                },
            )

    stub = _BogusMarkersStub()
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)
    # Crucially: this run MUST NOT raise AttributeError.
    results, totals, errors = disc._run_pipeline_for_speech(
        client=client,
        provider=disc.PROVIDER_OPENROUTER,
        base_url="http://stub",
        api_key="sk-test",
        model=disc.DISCOURSE_MODEL,
        speech_text=long_speech,
        prompts=prompts,
        max_tokens=4096,
        max_retries=1,
    )
    client.close()

    # Hawkins's bogus output must not pollute results — the validation
    # gate keeps it out.
    assert results["hawkins"] is None
    # Voice never runs because hawkins emitted no usable markers.
    assert results["voice"] is None
    # DQI / V-Party still run independently and succeed.
    assert results["dqi"] is not None
    assert results["vparty"] is not None
    # The hawkins error surfaces in the per-speech errors list so the
    # operator sees what happened.
    assert any("hawkins" in e for e in errors)


class _ParaphrasingStub(_StubOpenRouter):
    """Stub that emits a Hawkins marker whose evidence.text is REAL
    paraphrase — NOT a verbatim substring of the speech, even after
    typography folding. Pre-fix, voice silently skipped → entry failed.
    Post-fix, voice runs against marker_text alone → entry clean.
    """

    def __init__(self, paraphrased_marker_text: str) -> None:
        super().__init__(hawkins_markers=1)
        self._paraphrase = paraphrased_marker_text

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/models":
            return super().handler(request)
        if not (request.method == "POST" and request.url.path == "/chat/completions"):
            return httpx.Response(404)
        body = json.loads(request.content)
        kind = self._classify_kind(body)
        self.calls.append((kind, body))
        if kind == "hawkins":
            output = _hawkins_payload(
                score=1,
                markers=[
                    {
                        "kind": "people_vs_elite",
                        "evidence": {"text": self._paraphrase},
                        "preliminary_voice": "speaker_first_person",
                        "preliminary_voice_confidence": 0.85,
                        "marker_confidence": 0.85,
                        "rationale_short": "test",
                    }
                ],
            )
        elif kind == "voice":
            output = _voice_payload(1)
        elif kind == "dqi":
            output = _dqi_payload()
        elif kind == "vparty":
            output = _vparty_payload(score=0)
        else:
            return httpx.Response(400, json={"error": "unknown kind"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(output, ensure_ascii=False),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1500, "completion_tokens": 500},
            },
        )


# ----------------------------------------------------------------------
# Schema-aware salvage in parse_and_validate
# ----------------------------------------------------------------------


def _hawkins_schema() -> dict[str, Any]:
    """Minimal Hawkins-shaped schema with the marker-kind enum closed.

    Mirrors the production prompts/hawkins_populism_v1.schema.json's
    structure on the fields the salvage tests poke at.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["score", "markers", "rationale"],
        "properties": {
            "score": {"type": "integer", "enum": [0, 1, 2]},
            "markers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "evidence"],
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "people_vs_elite",
                                "evil_elite",
                                "homogeneous_people",
                            ],
                        },
                        "evidence": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["text"],
                            "properties": {"text": {"type": "string"}},
                        },
                    },
                },
            },
            "rationale": {"type": "string"},
        },
    }


def test_salvage_drops_marker_with_out_of_enum_kind() -> None:
    """The model emitted a Hawkins marker whose `kind` is `"quoted"` —
    a voice-classifier value that escaped into the wrong enum. The
    salvage MUST drop the offending marker, keep the valid one, and
    mark `repaired=True`.
    """
    schema = _hawkins_schema()
    payload = {
        "score": 1,
        "markers": [
            {
                "kind": "quoted",  # Bad — voice-classifier value
                "evidence": {"text": "x"},
            },
            {
                "kind": "people_vs_elite",  # Valid
                "evidence": {"text": "y"},
            },
        ],
        "rationale": "Test rationale.",
    }
    out, err, detail, repaired = disc.parse_and_validate(json.dumps(payload), schema)
    assert err is None, f"salvage should have succeeded; got err={err!r} ({detail})"
    assert repaired is True
    assert len(out["markers"]) == 1
    assert out["markers"][0]["kind"] == "people_vs_elite"


def test_salvage_strips_unknown_top_level_property() -> None:
    """Real production drift: DQI emits
    `respect_for_constructive_politics` (a hybrid of `respect_for_*`
    and `constructive_politics`). Salvage strips the unknown key.
    """
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["score", "rationale"],
        "properties": {
            "score": {"type": "integer"},
            "rationale": {"type": "string"},
        },
    }
    payload = {
        "score": 1,
        "rationale": "Test.",
        "respect_for_constructive_politics": "alternative_proposal",
    }
    out, err, detail, repaired = disc.parse_and_validate(json.dumps(payload), schema)
    assert err is None, f"salvage failed: err={err!r} ({detail})"
    assert repaired is True
    assert "respect_for_constructive_politics" not in out
    assert out["score"] == 1


def test_salvage_strips_unknown_nested_evidence_keys() -> None:
    """Real production drift: DQI markers' `evidence` has stray
    `text_2` / `text_3` keys alongside the schema-required `text`.
    Salvage strips the extras at the nested `evidence` level.
    """
    schema = _hawkins_schema()
    payload = {
        "score": 1,
        "markers": [
            {
                "kind": "people_vs_elite",
                "evidence": {
                    "text": "primary fragment",
                    "text_2": "extra fragment",
                    "text_3": "yet another",
                },
            }
        ],
        "rationale": "Test rationale.",
    }
    out, err, detail, repaired = disc.parse_and_validate(json.dumps(payload), schema)
    assert err is None, f"salvage failed: err={err!r} ({detail})"
    assert repaired is True
    assert out["markers"][0]["evidence"] == {"text": "primary fragment"}


def test_salvage_handles_combined_drift_in_one_pass() -> None:
    """A record with BOTH a bad-kind marker AND extra evidence keys
    AND an unknown top-level property. Salvage applies both passes;
    output must validate cleanly.
    """
    schema = _hawkins_schema()
    payload = {
        "score": 2,
        "markers": [
            {"kind": "quoted", "evidence": {"text": "x"}},  # Bad kind
            {
                "kind": "evil_elite",  # Valid
                "evidence": {
                    "text": "elite drift",
                    "text_2": "extra",  # Strip
                },
            },
        ],
        "rationale": "Combined drift.",
        "stray_field": "should be stripped",  # Strip
    }
    out, err, detail, repaired = disc.parse_and_validate(json.dumps(payload), schema)
    assert err is None, f"salvage failed: err={err!r} ({detail})"
    assert repaired is True
    assert "stray_field" not in out
    assert len(out["markers"]) == 1
    assert out["markers"][0]["kind"] == "evil_elite"
    assert out["markers"][0]["evidence"] == {"text": "elite drift"}


def test_salvage_does_not_fabricate_missing_required_field() -> None:
    """The model omitted a required field (e.g. truncated output ran
    out of tokens before emitting `rationale`). Salvage MUST NOT try
    to fill it — the failure stays a real failure, surfaced to the
    operator. This is the residual ~0.5% we can't auto-recover.
    """
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["score", "rationale"],
        "properties": {
            "score": {"type": "integer"},
            "rationale": {"type": "string"},
        },
    }
    payload = {"score": 1}  # Missing rationale.
    out, err, detail, repaired = disc.parse_and_validate(json.dumps(payload), schema)
    assert err == "schema_invalid"
    assert "rationale" in (detail or "")
    assert repaired is False


def test_salvage_drops_marker_missing_required_field() -> None:
    """Real production drift: DQI emits a marker that lacks the
    required `preliminary_voice` field (other markers in the same
    response are complete). The generalized salvage must drop the
    incomplete marker but keep the rest of the response.

    Reproduces `2020-02-21_MO-PII-16-2020 mo://2020/II/16#agenda-1#act-219`:
    `dqi: schema_invalid ('preliminary_voice' is a required property)`.
    """
    schema = _hawkins_schema()
    # Add `preliminary_voice` to the marker requirement set so the
    # test mirrors the DQI shape.
    schema["properties"]["markers"]["items"]["required"] = [
        "kind",
        "evidence",
        "preliminary_voice",
    ]
    schema["properties"]["markers"]["items"]["properties"]["preliminary_voice"] = {
        "type": "string"
    }
    payload = {
        "score": 1,
        "markers": [
            {
                "kind": "people_vs_elite",
                "evidence": {"text": "x"},
                # missing preliminary_voice → drop this marker
            },
            {
                "kind": "evil_elite",
                "evidence": {"text": "y"},
                "preliminary_voice": "speaker_first_person",
            },
        ],
        "rationale": "Test rationale.",
    }
    out, err, detail, repaired = disc.parse_and_validate(json.dumps(payload), schema)
    assert err is None, f"salvage failed: err={err!r} ({detail})"
    assert repaired is True
    assert len(out["markers"]) == 1
    assert out["markers"][0]["kind"] == "evil_elite"


def test_salvage_drops_marker_with_evidence_as_string() -> None:
    """Real production drift: model collapses one nesting level and
    emits `evidence: "string"` instead of `evidence: {text: "string"}`.

    Reproduces `2021-04-01_MO-PII-41-2021 mo://2021/II/41#agenda-4#act-25`:
    `hawkins: schema_invalid ('Înțelegeți de ce...' is not of type 'object')`.
    The bad marker is dropped; the rest of the response is preserved.
    """
    schema = _hawkins_schema()
    payload = {
        "score": 1,
        "markers": [
            {
                "kind": "evil_elite",
                # WRONG: evidence is a bare string instead of {text: ...}
                "evidence": "Înțelegeți de ce Domniile Lor sunt cei care apără",
            },
            {
                "kind": "people_vs_elite",
                "evidence": {"text": "people fragment"},
            },
        ],
        "rationale": "Test rationale.",
    }
    out, err, detail, repaired = disc.parse_and_validate(json.dumps(payload), schema)
    assert err is None, f"salvage failed: err={err!r} ({detail})"
    assert repaired is True
    assert len(out["markers"]) == 1
    assert out["markers"][0]["kind"] == "people_vs_elite"


def test_salvage_pass_order_preserves_markers_with_extra_evidence_keys() -> None:
    """Salvage pass order matters: strip-unknown-properties MUST run
    BEFORE filter-invalid-markers, otherwise a marker with one extra
    `text_2` key inside `evidence` would be DROPPED by the filter
    (since it fails `additionalProperties: false`) instead of having
    just the extra key stripped. This regression test pins the order.
    """
    schema = _hawkins_schema()
    payload = {
        "score": 1,
        "markers": [
            {
                "kind": "people_vs_elite",
                "evidence": {
                    "text": "primary fragment",
                    "text_2": "extra fragment that should be stripped",
                },
            }
        ],
        "rationale": "Test rationale.",
    }
    out, err, detail, repaired = disc.parse_and_validate(json.dumps(payload), schema)
    assert err is None, f"salvage failed: err={err!r} ({detail})"
    assert repaired is True
    # The marker survives — only the extra key is stripped, not the
    # whole marker.
    assert len(out["markers"]) == 1
    assert out["markers"][0]["evidence"] == {"text": "primary fragment"}


def test_salvage_no_op_when_payload_already_valid() -> None:
    """When the model emits a clean payload, parse_and_validate should
    not touch it — no `repaired=True` false positive.
    """
    schema = _hawkins_schema()
    payload = {
        "score": 0,
        "markers": [],
        "rationale": "Clean.",
    }
    out, err, detail, repaired = disc.parse_and_validate(json.dumps(payload), schema)
    assert err is None
    assert repaired is False
    assert out == payload


# ----------------------------------------------------------------------
# Truncation detection + terseness-on-retry
# ----------------------------------------------------------------------


def test_truncation_detector_fires_on_missing_required_at_cap() -> None:
    """Real-world shape: schema_invalid + missing-rationale + tokens
    near the cap → detector says yes.
    """
    call = disc.CallResult(
        prompt_kind="dqi",
        output={"score": 1, "markers": []},
        error="schema_invalid",
        error_detail="'rationale' is a required property",
        fragments_not_found=[],
        parse_repaired=False,
        tokens_in=39000,
        tokens_out=33159,
        cost_usd=0.013,
        latency_ms=10000,
        rate_limit_retries=0,
        fallback_to_json_object=False,
    )
    assert disc._is_truncation_failure(call, max_tokens=32768) is True


def test_truncation_detector_ignores_non_schema_errors() -> None:
    call = disc.CallResult(
        prompt_kind="dqi",
        output=None,
        error="transport",
        error_detail="http 503",
        fragments_not_found=[],
        parse_repaired=False,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        latency_ms=0,
        rate_limit_retries=0,
        fallback_to_json_object=False,
    )
    assert disc._is_truncation_failure(call, max_tokens=32768) is False


def test_truncation_detector_ignores_schema_invalid_at_low_token_use() -> None:
    """A schema_invalid that occurs at low token usage isn't a
    truncation — it's something else (wrong enum, bad shape) and
    terseness wouldn't help. Don't burn a retry on the wrong fix.
    """
    call = disc.CallResult(
        prompt_kind="hawkins",
        output={"score": 1},
        error="schema_invalid",
        error_detail="'rationale' is a required property",
        fragments_not_found=[],
        parse_repaired=False,
        tokens_in=5000,
        tokens_out=600,
        cost_usd=0.001,
        latency_ms=2000,
        rate_limit_retries=0,
        fallback_to_json_object=False,
    )
    assert disc._is_truncation_failure(call, max_tokens=32768) is False


def test_truncation_detector_ignores_other_schema_violations() -> None:
    """Wrong enum at high token usage isn't truncation — model emitted
    a complete output, just with a bad value. Salvage handles it.
    """
    call = disc.CallResult(
        prompt_kind="hawkins",
        output={"score": 1, "markers": [{"kind": "quoted"}], "rationale": "..."},
        error="schema_invalid",
        error_detail="'quoted' is not one of [...]",
        fragments_not_found=[],
        parse_repaired=False,
        tokens_in=5000,
        tokens_out=30000,
        cost_usd=0.012,
        latency_ms=10000,
        rate_limit_retries=0,
        fallback_to_json_object=False,
    )
    assert disc._is_truncation_failure(call, max_tokens=32768) is False


def test_terseness_suffix_applied_on_truncation_retry(
    long_speech: str,
    prompts: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    """End-to-end: when the first attempt truncates, the retry MUST
    include the terseness suffix in its user prompt.
    """
    sent_prompts: list[str] = []
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": disc.DISCOURSE_MODEL}]})
        body = json.loads(request.content)
        msgs = body.get("messages") or []
        sent_prompts.append(
            "".join(str(m.get("content") or "") for m in msgs if isinstance(m, dict))
        )
        attempts["n"] += 1
        if attempts["n"] == 1:
            output = {
                "framework_version": "steiner-bachtiger@2017",
                "score_unit": "dqi_multi",
                "framework_confidence": 0.5,
                "level_of_justification": 0,
                "content_of_justification": "none",
                "respect_for_groups": 0,
                "respect_for_demands": 0,
                "respect_for_counterarguments": 0,
                "constructive_politics": "positional",
                "markers": [],
                # rationale intentionally omitted to simulate truncation
            }
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(output, ensure_ascii=False),
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5000,
                        "completion_tokens": 31200,
                    },
                },
            )
        output = _dqi_payload()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(output, ensure_ascii=False),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5000, "completion_tokens": 1500},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    dqi_text, dqi_schema = prompts["dqi"]
    full_prompt = disc.build_prompt_for_speech(dqi_text, long_speech, dqi_schema)
    result = disc._run_one_prompt(
        client=client,
        provider=disc.PROVIDER_OPENROUTER,
        base_url="http://stub",
        api_key="sk-test",
        model=disc.DISCOURSE_MODEL,
        prompt_kind="dqi",
        full_prompt=full_prompt,
        schema=dqi_schema,
        speech_text=long_speech,
        max_tokens=32768,
        max_retries=1,
    )
    client.close()

    assert result.error is None, f"retry should have succeeded; err={result.error}"
    assert attempts["n"] == 2
    assert "token-budget guard" not in sent_prompts[0]
    assert "token-budget guard" in sent_prompts[1]
    assert "AT MOST 6 markers" in sent_prompts[1]


def test_terseness_NOT_applied_when_first_attempt_succeeds(
    long_speech: str,
    prompts: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    sent_prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": disc.DISCOURSE_MODEL}]})
        body = json.loads(request.content)
        msgs = body.get("messages") or []
        sent_prompts.append(
            "".join(str(m.get("content") or "") for m in msgs if isinstance(m, dict))
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(_dqi_payload(), ensure_ascii=False),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5000, "completion_tokens": 1500},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    dqi_text, dqi_schema = prompts["dqi"]
    full_prompt = disc.build_prompt_for_speech(dqi_text, long_speech, dqi_schema)
    disc._run_one_prompt(
        client=client,
        provider=disc.PROVIDER_OPENROUTER,
        base_url="http://stub",
        api_key="sk-test",
        model=disc.DISCOURSE_MODEL,
        prompt_kind="dqi",
        full_prompt=full_prompt,
        schema=dqi_schema,
        speech_text=long_speech,
        max_tokens=32768,
        max_retries=1,
    )
    client.close()

    assert len(sent_prompts) == 1
    assert "token-budget guard" not in sent_prompts[0]


def test_terseness_NOT_applied_on_non_truncation_retries(
    long_speech: str,
    prompts: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    """When the first attempt fails for a NON-truncation reason
    (e.g., transport 503), the retry prompt must NOT carry the
    terseness suffix — would be a false-positive cost.
    """
    sent_prompts: list[str] = []
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": disc.DISCOURSE_MODEL}]})
        body = json.loads(request.content)
        msgs = body.get("messages") or []
        sent_prompts.append(
            "".join(str(m.get("content") or "") for m in msgs if isinstance(m, dict))
        )
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, json={"error": "service unavailable"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(_dqi_payload(), ensure_ascii=False),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5000, "completion_tokens": 1500},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    dqi_text, dqi_schema = prompts["dqi"]
    full_prompt = disc.build_prompt_for_speech(dqi_text, long_speech, dqi_schema)
    disc._run_one_prompt(
        client=client,
        provider=disc.PROVIDER_OPENROUTER,
        base_url="http://stub",
        api_key="sk-test",
        model=disc.DISCOURSE_MODEL,
        prompt_kind="dqi",
        full_prompt=full_prompt,
        schema=dqi_schema,
        speech_text=long_speech,
        max_tokens=32768,
        max_retries=1,
    )
    client.close()

    assert attempts["n"] == 2
    assert "token-budget guard" not in sent_prompts[0]
    assert "token-budget guard" not in sent_prompts[1]


def test_salvage_real_world_dqi_drift_shape() -> None:
    """Reproduces the actual drift pattern observed in the production
    log on `2024-10-29_MO-PII-117-2024 mo://2024/II/117#agenda-1#act-61`:
    DQI emits a hybrid `respect_for_constructive_politics` field at
    the top level, alongside the canonical `constructive_politics`.
    Salvage drops the hybrid; the real one stays.
    """
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["score", "constructive_politics", "rationale"],
        "properties": {
            "score": {"type": "integer"},
            "constructive_politics": {
                "type": "string",
                "enum": ["positional", "alternative_proposal"],
            },
            "rationale": {"type": "string"},
        },
    }
    payload = {
        "score": 1,
        "constructive_politics": "alternative_proposal",
        "respect_for_constructive_politics": "alternative_proposal",
        "rationale": "Production-shape drift.",
    }
    out, err, detail, repaired = disc.parse_and_validate(json.dumps(payload), schema)
    assert err is None, f"salvage failed: err={err!r} ({detail})"
    assert repaired is True
    assert out["constructive_politics"] == "alternative_proposal"
    assert "respect_for_constructive_politics" not in out


def test_full_pipeline_recovers_voice_pass_with_paraphrased_marker(
    tmp_path: Path,
    long_speech: str,
    prompts: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    """End-to-end: a Hawkins marker whose evidence.text is NOT a verbatim
    substring of the speech (real paraphrase) MUST still trigger the
    voice pass — the speech finishes clean, not in `failed`.
    """
    sidecar = _write_sidecar(
        tmp_path, speeches=[_speech_activity("act-1", long_speech)]
    )
    stub = _ParaphrasingStub("frază care NU apare verbatim în discurs")
    transport = httpx.MockTransport(stub.handler)
    client = httpx.Client(transport=transport)
    result = disc.analyze_sidecar(
        sidecar,
        openrouter_url="http://stub",
        api_key="sk-test",
        client=client,
        prompts=prompts,
    )
    client.close()

    # Voice MUST have run (4 calls per speech: hawkins + voice + dqi + vparty).
    kinds = [k for k, _ in stub.calls]
    assert kinds.count("voice") == 1, (
        f"voice did not run with paraphrased marker: kinds={kinds}"
    )
    # Sidecar produced one analysed entry, no failed-record drop.
    assert result.analyzed == 1
    assert result.failed == 0
    # The persisted entry must carry voice payload.
    out_files = list(tmp_path.glob("*.discourse.flash-lite.v0_1.json"))
    assert len(out_files) == 1
    payload = json.loads(out_files[0].read_text(encoding="utf-8"))
    entries = list(payload.values())
    assert len(entries) == 1
    assert entries[0].get("voice") is not None
