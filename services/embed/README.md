# `services/embed/` — BGE-M3 embedding service

FastAPI wrapper over `sentence-transformers` that produces 1024-dim
dense vectors for the `monitorul-ii embed` enrichment producer. Lives
outside the main Python package because it ships a heavy dependency
closure (`torch` + `sentence-transformers` + `transformers` ≈ 2 GB on
disk) that the CLI doesn't need.

See [`docs/elasticsearch-indexing.md`](../../docs/elasticsearch-indexing.md)
§ Q8 for the design rationale (why local BGE-M3 over OpenAI / Cohere /
Voyage), and [`docs/architecture.md`](../../docs/architecture.md) for
the day-to-day operational notes.

## Endpoints

* `GET /healthz` — liveness probe. Reports the configured model ID
  without forcing a load. Suitable for Docker / Kubernetes
  `livenessProbe`.
* `POST /embed` — embed one batch.
  Request: `{"texts": [...], "normalize": true}`.
  Response: `{"vectors": [[float; 1024]], "model_id": "BAAI/bge-m3", "dims": 1024}`.

## Run via docker compose (recommended)

Project root carries a `docker-compose.yml` with the embed service +
HF-cache volume + healthcheck wired up. From the project root:

```bash
# CPU (default)
docker compose up           # foreground; Ctrl-C to stop
docker compose up -d        # detached
docker compose logs -f embed
docker compose down         # stop and remove

# GPU (requires nvidia-container-toolkit on host)
TARGET=gpu docker compose build embed
docker compose --profile gpu up
```

The named `hf-cache` volume (`monitorul_hf-cache`) holds the ~2 GB
BAAI/bge-m3 weights so subsequent `docker compose up` runs are
instant. Delete with `docker volume rm monitorul_hf-cache` to force a
re-download (e.g. after a model bump).

Override defaults via env vars before invoking compose:

```bash
EMBED_PORT=9000 docker compose up         # publish on 9000 instead of 8000
EMBED_MAX_BATCH_SIZE=64 docker compose up  # bigger internal batches
```

GPU on a single consumer card is ~10× faster than CPU; the bootstrap
pass over the 5552-doc corpus drops from ~30 hours (CPU) to ~3 hours
(GPU).

## Run without Docker (local Python venv)

For iterating on `server.py` itself:

```bash
cd services/embed
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn services.embed.server:app --host 0.0.0.0 --port 8000
```

The first call downloads ~2 GB of weights from HuggingFace into
`~/.cache/huggingface/`. Subsequent calls are instant.

## Run via raw docker (no compose)

```bash
# CPU
docker build -t monitorul-embed services/embed
docker run --rm -p 8000:8000 \
    -v monitorul_hf-cache:/cache/hf \
    monitorul-embed

# GPU
docker build --build-arg TARGET=gpu -t monitorul-embed:gpu services/embed
docker run --rm -p 8000:8000 --gpus all \
    -v monitorul_hf-cache:/cache/hf \
    monitorul-embed:gpu
```

Use this only when you need single-shot control without a compose file
(e.g. on a remote box where the project tree isn't checked out).

## Smoke test

```bash
curl -X POST http://localhost:8000/embed \
    -H 'Content-Type: application/json' \
    -d '{"texts":["bună ziua, doamnelor și domnilor"]}' \
    | jq '.vectors[0] | length'
# → 1024
```

## Wiring with `monitorul-ii embed`

The CLI subcommand reads `EMBED_URL` from the environment (default
`http://127.0.0.1:8000`). When the service is reachable, every
substantive record gets embedded; the result is persisted alongside
each sidecar as `<basename>.embedding.bge-m3.v0_1.json` and the
`monitorul-ii index` step picks them up automatically (Q3 of the
design doc — enrichments are merged into ES bulk actions per
`record_id`).

## Scaling notes

* **One worker per GPU.** Embedding is GPU-bound on the GPU image and
  CPU-bound on the CPU image. With `uvicorn --workers N`, each worker
  loads its own copy of the model — be aware of memory cost.
* **Batch size.** `EMBED_MAX_BATCH_SIZE` (default 32) controls the
  internal batch the model sees. Higher = better GPU utilization but
  larger peak memory.
* **Sequence length.** BGE-M3 supports up to 8192 tokens. The producer
  module truncates to ~8000 chars (≈2K tokens) before sending — Q8 of
  the design doc keeps v0.1 to first-2K-tokens-only for long speeches;
  v0.2 will add chunking with `record_id#chunk-N` keys.

## Disaster recovery

The service is stateless. Lose the container → rebuild from
`Dockerfile`; weights re-download on first call. Embeddings on disk
(in the project's `pdfs/` mirror) are the canonical artifact and are
indexer-driven, not service-driven.
