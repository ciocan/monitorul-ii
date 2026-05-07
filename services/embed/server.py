"""BGE-M3 embedding service — FastAPI wrapper over `sentence-transformers`.

Exposes one endpoint, `POST /embed`, used by the `monitorul-ii embed`
subcommand to project sidecar records into 1024-dim dense vectors. The
service is the operational counterpart to Q8 of
`docs/elasticsearch-indexing.md`: BGE-M3 (1024-dim) running locally,
free at re-embed time, strong on Romanian.

Why a separate service instead of in-process embedding:

1. **Lifecycle decoupling.** The embedder loads ~2 GB of model weights
   on startup and keeps them resident; running it in-process inside the
   `monitorul-ii` CLI would force every `extract` / `link` / `backfill`
   invocation to either pay the load cost (slow) or skip it (complex
   conditional imports). A long-lived FastAPI process loads once and
   serves many CLI runs.
2. **Hardware swap.** The same producer can target a CPU host or a GPU
   host — only the service URL changes. CPU bootstrap (~30 hrs on the
   5552-doc corpus) and GPU re-embed (~3 hrs) share the same client
   contract.
3. **Optional dependency.** `sentence-transformers` + `torch` weigh
   gigabytes. Keeping them out of the main project's dependency closure
   means `pyproject.toml` stays lean for everyday use; embedding is
   opt-in via the service.

Run locally with `uvicorn services.embed.server:app --host 0.0.0.0
--port 8000` (after installing the service deps from
`services/embed/requirements.txt`). See `Dockerfile` for the canonical
deploy.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("monitorul.embed")
logging.basicConfig(level=logging.INFO)


MODEL_ID = os.environ.get("EMBED_MODEL_ID", "BAAI/bge-m3")
MAX_BATCH_SIZE = int(os.environ.get("EMBED_MAX_BATCH_SIZE", "32"))
EMBED_DIMS = 1024

# Precision policy. BGE-M3 supports fp16 inference at negligible
# retrieval-quality loss; running in fp16 roughly halves the per-batch
# encode time AND halves activation memory, so we can safely enable it
# in production. CPU hosts (no CUDA available) silently fall back to
# fp32 since `Tensor.half()` on CPU is slow / unsupported on some ops.
EMBED_FP16 = os.environ.get("EMBED_FP16", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


class EmbedRequest(BaseModel):
    """One batch of texts to embed.

    `normalize` is True by default: cosine similarity over normalised
    vectors is the indexer's expected mode (the ES mapping declares
    `similarity: cosine`). Callers asking for raw L2-distance vectors
    should set False explicitly.
    """

    texts: list[str] = Field(..., min_length=1, max_length=512)
    normalize: bool = True


class EmbedResponse(BaseModel):
    """Response shape: one row per input text, in the same order."""

    vectors: list[list[float]]
    model_id: str
    dims: int


def _build_model() -> Any:
    """Load the SentenceTransformer once at process start.

    Imports are deferred so the FastAPI app object can be constructed
    without `torch` / `sentence-transformers` available — useful for
    the test suite, which mounts a fake `embed` model and avoids the
    ~2 GB weight download.

    When `EMBED_FP16=1` (default) AND CUDA is available, the model is
    cast to fp16 — roughly halves the per-batch encode time and
    activation memory, with retrieval-quality loss small enough to be
    invisible at the 1024-dim BGE-M3 scale. CPU-only hosts silently
    skip the cast (fp16 ops on CPU are slow / sometimes unsupported).
    """
    from sentence_transformers import SentenceTransformer

    logger.info("loading model %s (fp16=%s)", MODEL_ID, EMBED_FP16)
    model = SentenceTransformer(MODEL_ID)
    if EMBED_FP16:
        try:
            import torch

            if torch.cuda.is_available():
                model = model.half()
                logger.info("model cast to fp16 on GPU")
            else:
                logger.info("CUDA unavailable; staying at fp32 on CPU")
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning("fp16 cast failed, staying at fp32: %s", exc)
    logger.info("model %s ready", MODEL_ID)
    return model


def _encode(model: Any, texts: list[str], normalize: bool) -> list[list[float]]:
    """Run the model with a sane internal batch.

    `sentence-transformers` will tokenise + truncate to the model's
    `max_seq_length` (8192 for BGE-M3). Callers that want a hard cap
    below that should truncate at the producer side (we do — see
    `MAX_TEXT_CHARS` in the producer module).
    """
    if not texts:
        return []
    vectors = model.encode(
        texts,
        batch_size=MAX_BATCH_SIZE,
        normalize_embeddings=normalize,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return vectors.tolist()


def create_app(*, model: Any | None = None) -> FastAPI:
    """Application factory.

    Tests pass an in-memory stub model that avoids torch/HF entirely.
    Production calls `create_app()` with no args; the factory loads
    the real model on first request via `app.state.model`.
    """
    app = FastAPI(title="monitorul embedder", version="0.1.0")
    app.state.model = model

    # Serialise GPU encode calls so multiple concurrent HTTP requests
    # don't oversubscribe device memory. FastAPI runs sync endpoints in
    # a threadpool (40 workers default), so without the lock 4 parallel
    # producers would each hold their batch's activation memory in the
    # same CUDA stream — at batch=32 that's 4× ~2.5 GB ≈ 10 GB on top
    # of the 2.5 GB resident model, blowing past a 12 GB GPU.
    #
    # With the lock, throughput is bounded by GPU throughput (the same
    # ceiling sequential clients hit), but parallel producers still
    # win on overlapped CPU work — while one batch is encoding, others
    # prep their next batch (read sidecar, walk records, hash text).
    app.state.encode_lock = threading.Lock()
    # Separate lock for the lazy first-load path so the model never
    # gets built twice if two requests race on a fresh container.
    app.state.load_lock = threading.Lock()

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """Lightweight liveness probe used by Docker/K8s health checks.

        Reports the configured model id but does NOT trigger a load —
        a fresh container responds immediately even before the weights
        are paged in.
        """
        return {"status": "ok", "model_id": MODEL_ID, "dims": EMBED_DIMS}

    @app.post("/embed", response_model=EmbedResponse)
    def embed(req: EmbedRequest) -> EmbedResponse:
        if not req.texts:
            raise HTTPException(status_code=400, detail="texts must be non-empty")
        if app.state.model is None:
            with app.state.load_lock:
                if app.state.model is None:
                    app.state.model = _build_model()
        with app.state.encode_lock:
            vectors = _encode(app.state.model, list(req.texts), req.normalize)
        return EmbedResponse(vectors=vectors, model_id=MODEL_ID, dims=EMBED_DIMS)

    return app


# Default app surface — uvicorn picks this up via `services.embed.server:app`.
app = create_app()


__all__ = [
    "EmbedRequest",
    "EmbedResponse",
    "MODEL_ID",
    "EMBED_DIMS",
    "create_app",
    "app",
]
