"""Tests for the BGE-M3 embedding enrichment producer.

These tests mock out the embed service via `httpx.MockTransport` so the
producer never makes a real HTTP call. The test surface focuses on
contract behaviour:

  * Walks each doc-type body shape and emits one task per embeddable
    record.
  * Honours the `MIN_TEXT_CHARS` cutoff (chair turns aren't embedded).
  * Truncates at `MAX_TEXT_CHARS`, flagging `_meta.truncated: true`.
  * Idempotency: identical fingerprints → reuse vector verbatim, no
    HTTP call. Mismatched fingerprints → re-embed.
  * Atomic write contract: a `.part` file never lingers after success.
  * Filename shape matches the loader's regex.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from monitorul_ii.elasticsearch import enrichments as es_enrichments
from monitorul_ii.extraction.enrichments import embedding as emb


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _ones_vector(seed: int = 0) -> list[float]:
    """Return a deterministic 1024-dim vector. Components vary slightly
    per `seed` so different inputs get different vectors — useful for
    asserting "the third record's vector landed in the third position"
    style invariants.
    """
    base = (seed + 1) * 0.001
    return [base + i * 1e-6 for i in range(emb.EMBEDDING_DIMS)]


class _StubEmbedService:
    """A minimal stand-in for the FastAPI embed service.

    Tracks every call so tests can assert on batch sizes and the
    invariant "no HTTP call when every record's fingerprint matches".
    """

    def __init__(self) -> None:
        self.healthz_calls = 0
        self.embed_calls: list[list[str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/healthz":
            self.healthz_calls += 1
            return httpx.Response(
                200,
                json={"status": "ok", "model_id": emb.EMBEDDING_MODEL_ID, "dims": 1024},
            )
        if request.method == "POST" and request.url.path == "/embed":
            payload = json.loads(request.content)
            texts = payload.get("texts") or []
            self.embed_calls.append(list(texts))
            vectors = [_ones_vector(i) for i in range(len(texts))]
            return httpx.Response(
                200,
                json={
                    "vectors": vectors,
                    "model_id": emb.EMBEDDING_MODEL_ID,
                    "dims": 1024,
                },
            )
        return httpx.Response(404)


@pytest.fixture
def stub_service() -> _StubEmbedService:
    return _StubEmbedService()


@pytest.fixture
def mock_client(stub_service: _StubEmbedService) -> httpx.Client:
    transport = httpx.MockTransport(stub_service.handler)
    return httpx.Client(transport=transport)


def _write_sidecar(tmp_path: Path, body: dict, *, content_sha: str = "sha-A") -> Path:
    """Write a minimal sidecar with the provided body; return the path.
    Exposes `document_id` + `content_sha` (the producer reads both).
    """
    p = tmp_path / "doc.extraction.json"
    p.write_text(
        json.dumps(
            {
                "document_id": "mo://2024/II/100",
                "content_sha": content_sha,
                "body": body,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


# ----------------------------------------------------------------------
# Filename + helpers
# ----------------------------------------------------------------------


def test_embedding_filename_shape():
    assert (
        emb.embedding_filename("2018-11-20_MO-PII-168-2018")
        == "2018-11-20_MO-PII-168-2018.embedding.bge-m3.v0_1.json"
    )


def test_loader_regex_recognises_embedding_filename():
    """The indexer's loader regex must parse the producer's filename
    so the merge step picks up vectors automatically.
    """
    parsed = es_enrichments.parse_enrichment_filename(
        Path("doc.embedding.bge-m3.v0_1.json")
    )
    # Producer is `embedding` (the namespace key); model is `bge-m3`
    # but parse_enrichment_filename returns only (producer, version).
    assert parsed == ("embedding", "0.1")


def test_loader_regex_back_compat_with_simple_form():
    """The pre-existing single-segment producer filenames still parse."""
    assert es_enrichments.parse_enrichment_filename(Path("doc.topics.v0_1.json")) == (
        "topics",
        "0.1",
    )
    assert es_enrichments.parse_enrichment_filename(Path("doc.bge-m3.v1_2.json")) == (
        "bge-m3",
        "1.2",
    )


# ----------------------------------------------------------------------
# Record discovery — happy paths per body shape
# ----------------------------------------------------------------------


def _speech_body(text: str) -> dict:
    return {
        "agenda_items": [
            {
                "id": "mo://2024/II/100#agenda-1",
                "title": "Discutarea proiectului de lege X" * 4,  # >100 chars
                "ordinal": 1,
                "activities": [
                    {
                        "id": "mo://2024/II/100#agenda-1#act-1",
                        "type": "speech",
                        "text": text,
                        "speaker": {"name": "Ion Popescu"},
                    }
                ],
            }
        ]
    }


def test_embed_speech_text(tmp_path: Path, mock_client, stub_service):
    long = "Mulțumesc, doamnă președintă. " * 20  # well over 100 chars
    sidecar = _write_sidecar(tmp_path, _speech_body(long))

    result = emb.embed_sidecar(
        sidecar,
        embed_url="http://stub",
        client=mock_client,
    )

    assert result.action == "embedded"
    # Two records: agenda title (>100 chars) + speech text.
    assert result.embedded == 2
    assert result.reused == 0
    out_path = result.file_path
    assert out_path is not None
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    speech_id = "mo://2024/II/100#agenda-1#act-1"
    assert speech_id in payload
    entry = payload[speech_id]
    assert entry["_meta"]["producer"] == emb.EMBEDDING_PRODUCER
    assert entry["_meta"]["version"] == emb.EMBEDDING_VERSION_DOTTED
    assert entry["_meta"]["dims"] == emb.EMBEDDING_DIMS
    assert entry["_meta"]["source_sidecar_content_sha"] == "sha-A"
    assert entry["_meta"]["truncated"] is False
    assert isinstance(entry["vector"], list)
    assert len(entry["vector"]) == emb.EMBEDDING_DIMS
    assert isinstance(entry["text_fingerprint"], str)
    assert len(entry["text_fingerprint"]) == 12


def test_short_text_skipped(tmp_path: Path, mock_client, stub_service):
    """Speeches under MIN_TEXT_CHARS (chair turns) aren't embedded."""
    sidecar = _write_sidecar(
        tmp_path,
        {
            "agenda_items": [
                {
                    "id": "mo://2024/II/100#agenda-1",
                    "title": "x",  # also under threshold
                    "ordinal": 1,
                    "activities": [
                        {
                            "id": "mo://2024/II/100#agenda-1#act-1",
                            "type": "speech",
                            "text": "Mulțumesc.",  # well under 100 chars
                            "speaker": {"name": "Ion Popescu"},
                        }
                    ],
                }
            ]
        },
    )

    result = emb.embed_sidecar(
        sidecar,
        embed_url="http://stub",
        client=mock_client,
    )

    # No embeddable records → "skipped" without a service call.
    assert result.action == "skipped"
    assert result.embedded == 0
    assert stub_service.embed_calls == []


def test_non_speech_activities_not_embedded(tmp_path: Path, mock_client, stub_service):
    long_title = "Discutarea proiectului de lege X" * 4
    sidecar = _write_sidecar(
        tmp_path,
        {
            "agenda_items": [
                {
                    "id": "mo://2024/II/100#agenda-1",
                    "title": long_title,
                    "ordinal": 1,
                    "activities": [
                        {
                            "id": "mo://2024/II/100#agenda-1#vote-1",
                            "type": "vote",
                            "text": "Votul. " * 50,
                        },
                        {
                            "id": "mo://2024/II/100#agenda-1#act-1",
                            "type": "procedural",
                            "text": "Procedură. " * 50,
                        },
                    ],
                }
            ]
        },
    )

    result = emb.embed_sidecar(
        sidecar,
        embed_url="http://stub",
        client=mock_client,
    )
    # Only the agenda title (which exceeds 100 chars) should embed.
    assert result.embedded == 1
    payload = json.loads(result.file_path.read_text(encoding="utf-8"))
    assert "mo://2024/II/100#agenda-1" in payload
    assert "mo://2024/II/100#agenda-1#vote-1" not in payload
    assert "mo://2024/II/100#agenda-1#act-1" not in payload


def test_interpellation_embeds_topic_plus_question_text(
    tmp_path: Path, mock_client, stub_service
):
    long_topic = "Salarizarea profesorilor în mediul rural — politica MEC. "
    long_qt = "Vă rog să precizați măsurile concrete adoptate de minister. " * 5
    sidecar = _write_sidecar(
        tmp_path,
        {
            "interpellations": [
                {
                    "id": "mo://2024/II/100#interp-345",
                    "topic": long_topic,
                    "question_text": long_qt,
                }
            ]
        },
    )
    result = emb.embed_sidecar(sidecar, embed_url="http://stub", client=mock_client)
    assert result.embedded == 1
    payload = json.loads(result.file_path.read_text(encoding="utf-8"))
    assert "mo://2024/II/100#interp-345" in payload


def test_question_register_embeds(tmp_path: Path, mock_client, stub_service):
    sidecar = _write_sidecar(
        tmp_path,
        {
            "questions": [
                {
                    "id": "mo://2024/II/100#q-12345",
                    "topic": "Lipsa medicamentelor oncologice. " * 5,
                    "question_text": "Care este planul ministerului? " * 6,
                }
            ]
        },
    )
    result = emb.embed_sidecar(sidecar, embed_url="http://stub", client=mock_client)
    assert result.embedded == 1


def test_committee_meeting_embeds(tmp_path: Path, mock_client, stub_service):
    sidecar = _write_sidecar(
        tmp_path,
        {
            "committees": [
                {
                    "name": "Comisia juridică",
                    "meetings": [
                        {
                            "id": "mo://2024/II/100#cmt-juridica-1",
                            "purpose": "Examinarea proiectelor pe ordinea de zi a comisiei. "
                            * 3,
                            "agenda": [
                                {"title": "PL-x 100/2024 — modificarea Codului Civil. "}
                            ],
                        }
                    ],
                }
            ]
        },
    )
    result = emb.embed_sidecar(sidecar, embed_url="http://stub", client=mock_client)
    assert result.embedded == 1


def test_report_embeds_title_and_headings(tmp_path: Path, mock_client, stub_service):
    sidecar = _write_sidecar(
        tmp_path,
        {
            "report": {
                "id": "mo://2024/II/100",
                "title": "Raportul privind activitatea Curții de Conturi în anul 2023, cu detalii pe domeniile auditate.",
                "issuing_body": "Curtea de Conturi",
            },
            "headings": [
                {"level": 1, "text": "Sinteza activității Curții de Conturi"},
                {
                    "level": 2,
                    "text": "Domeniul financiar și controlul cheltuielilor publice",
                },
            ],
        },
    )
    result = emb.embed_sidecar(sidecar, embed_url="http://stub", client=mock_client)
    assert result.embedded == 1


# ----------------------------------------------------------------------
# Truncation
# ----------------------------------------------------------------------


def test_long_speech_truncated(tmp_path: Path, mock_client, stub_service):
    """Speeches over MAX_TEXT_CHARS are clipped; entry's truncated flag fires."""
    very_long = "În cadrul ședinței plenare. " * 1500  # ~42K chars
    sidecar = _write_sidecar(tmp_path, _speech_body(very_long))
    result = emb.embed_sidecar(sidecar, embed_url="http://stub", client=mock_client)
    payload = json.loads(result.file_path.read_text(encoding="utf-8"))
    speech = payload["mo://2024/II/100#agenda-1#act-1"]
    assert speech["_meta"]["truncated"] is True
    # The text sent to the service was truncated to MAX_TEXT_CHARS.
    sent_text = stub_service.embed_calls[0][1]  # second batch item; agenda is first
    # Title is short, speech is long. Order may vary; find the speech text.
    speech_text = max(stub_service.embed_calls[0], key=len)
    assert len(speech_text) == emb.MAX_TEXT_CHARS
    # Same truncation rule as in the producer module.
    assert speech_text == ("În cadrul ședinței plenare. " * 1500)[: emb.MAX_TEXT_CHARS]
    assert result.truncated >= 1
    # `sent_text` is just here to silence the linter — the real check is
    # against the longest text in the batch, computed above.
    _ = sent_text


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


def test_idempotent_reuse_when_fingerprint_matches(
    tmp_path: Path, mock_client, stub_service
):
    body = _speech_body("Mulțumesc, doamnă președintă. " * 20)
    sidecar = _write_sidecar(tmp_path, body)

    first = emb.embed_sidecar(sidecar, embed_url="http://stub", client=mock_client)
    assert first.action == "embedded"
    first_calls = len(stub_service.embed_calls)
    payload_v1 = json.loads(first.file_path.read_text(encoding="utf-8"))

    # Re-run with identical body → no embed calls.
    second = emb.embed_sidecar(sidecar, embed_url="http://stub", client=mock_client)
    # Either "skipped" (everything reused) or "embedded" with reused=N
    # and embedded=0 — both are acceptable shapes; the embed-calls
    # counter is the load-bearing assertion.
    assert second.action in ("skipped", "embedded")
    assert len(stub_service.embed_calls) == first_calls
    payload_v2 = json.loads(first.file_path.read_text(encoding="utf-8"))
    # Existing file unchanged (reuse path doesn't rewrite).
    assert payload_v1 == payload_v2
    # The reused entries are still present.
    for rid in payload_v1:
        assert rid in payload_v2


def test_stale_fingerprint_triggers_reembed(tmp_path: Path, mock_client, stub_service):
    """When a record's text changes, the fingerprint mismatch must
    trigger a re-embed for that record only.
    """
    sidecar = _write_sidecar(
        tmp_path,
        _speech_body("First version of the speech. " * 10),
    )
    first = emb.embed_sidecar(sidecar, embed_url="http://stub", client=mock_client)
    first_calls = len(stub_service.embed_calls)
    assert first.embedded == 2

    # Mutate the speech text but leave the agenda title alone.
    body = _speech_body("Different version of the speech. " * 10)
    sidecar.write_text(
        json.dumps(
            {
                "document_id": "mo://2024/II/100",
                "content_sha": "sha-B",  # bumped to simulate a re-extract
                "body": body,
            }
        ),
        encoding="utf-8",
    )

    second = emb.embed_sidecar(sidecar, embed_url="http://stub", client=mock_client)
    assert second.embedded == 1  # the speech only
    assert second.reused == 1  # the title
    # One additional service call (one record).
    assert len(stub_service.embed_calls) == first_calls + 1


def test_force_flag_reembeds_everything(tmp_path: Path, mock_client, stub_service):
    body = _speech_body("Stable speech text. " * 20)
    sidecar = _write_sidecar(tmp_path, body)
    emb.embed_sidecar(sidecar, embed_url="http://stub", client=mock_client)
    initial_calls = len(stub_service.embed_calls)

    second = emb.embed_sidecar(
        sidecar, embed_url="http://stub", client=mock_client, force=True
    )
    assert second.action == "embedded"
    assert second.reused == 0
    # Second call re-embedded all records → another batch.
    assert len(stub_service.embed_calls) > initial_calls


# ----------------------------------------------------------------------
# Service errors / dry-run
# ----------------------------------------------------------------------


def test_dry_run_skips_service(tmp_path: Path, mock_client, stub_service):
    body = _speech_body("Some long speech text. " * 30)
    sidecar = _write_sidecar(tmp_path, body)
    result = emb.embed_sidecar(
        sidecar,
        embed_url="http://stub",
        client=mock_client,
        dry_run=True,
    )
    assert result.action == "dry-run"
    assert result.embedded > 0
    assert stub_service.embed_calls == []
    # The file was not written.
    assert result.file_path is not None
    assert not result.file_path.exists()


def test_service_failure_records_error(tmp_path: Path):
    def _fail(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "model loading"})

    transport = httpx.MockTransport(_fail)
    client = httpx.Client(transport=transport)
    sidecar = _write_sidecar(tmp_path, _speech_body("A speech " * 30))

    result = emb.embed_sidecar(sidecar, embed_url="http://stub", client=client)
    assert result.action == "error"
    assert result.errors


def test_dimension_mismatch_records_error(tmp_path: Path):
    def _wrong_dims(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "vectors": [[0.0] * 768 for _ in payload["texts"]],
                "dims": 768,
            },
        )

    transport = httpx.MockTransport(_wrong_dims)
    client = httpx.Client(transport=transport)
    sidecar = _write_sidecar(tmp_path, _speech_body("A speech " * 30))
    result = emb.embed_sidecar(sidecar, embed_url="http://stub", client=client)
    assert result.action == "error"


# ----------------------------------------------------------------------
# Atomic write
# ----------------------------------------------------------------------


def test_atomic_write_no_part_lingers(tmp_path: Path, mock_client, stub_service):
    body = _speech_body("speech " * 30)
    sidecar = _write_sidecar(tmp_path, body)
    result = emb.embed_sidecar(sidecar, embed_url="http://stub", client=mock_client)
    assert result.action == "embedded"
    files = list(tmp_path.iterdir())
    assert all(not f.name.endswith(".part") for f in files)


# ----------------------------------------------------------------------
# embed_all batch wrapper
# ----------------------------------------------------------------------


def test_embed_all_iterates(tmp_path: Path, mock_client, stub_service):
    paths: list[Path] = []
    for i in range(3):
        sub = tmp_path / f"d{i}"
        sub.mkdir()
        body = _speech_body(f"text doc{i} " * 20)
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
        paths.append(p)

    results = list(emb.embed_all(paths, embed_url="http://stub", client=mock_client))
    assert len(results) == 3
    assert all(r.action == "embedded" for r in results)


# ----------------------------------------------------------------------
# Healthcheck
# ----------------------------------------------------------------------


def test_healthcheck_ok():
    """Patches `httpx.Client` inside the function to return 200 + model id.

    The healthcheck builds its own client (it's a one-shot probe), so
    we patch the module-level `httpx` to redirect.
    """
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200, json={"status": "ok", "model_id": "BAAI/bge-m3", "dims": 1024}
        )
    )

    real_client = httpx.Client
    try:
        httpx.Client = lambda **kw: real_client(transport=transport, **kw)
        ok, detail = emb.healthcheck("http://stub")
    finally:
        httpx.Client = real_client
    assert ok
    assert detail == "BAAI/bge-m3"


def test_healthcheck_failure():
    transport = httpx.MockTransport(lambda req: httpx.Response(500))
    real_client = httpx.Client
    try:
        httpx.Client = lambda **kw: real_client(transport=transport, **kw)
        ok, detail = emb.healthcheck("http://stub")
    finally:
        httpx.Client = real_client
    assert not ok
    assert detail
