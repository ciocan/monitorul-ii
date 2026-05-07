# BGE-M3 embedding service.
#
# Single image, runs in both CPU and GPU mode. Modern torch wheels from
# PyPI ship CUDA libraries (`nvidia-cublas-*`, `nvidia-cudnn-*`, `triton`)
# pulled in as transitive deps, so the same image:
#
#   - on CPU host:    `torch.cuda.is_available()` → False, falls back to CPU
#   - on GPU host with `--gpus all`: detects the NVIDIA runtime, encodes on GPU
#
# Earlier iterations of this Dockerfile tried to fork CPU vs GPU on
# different base images (`python:3.12-slim` vs `nvidia/cuda:...`) — but
# the resulting torch was identical (it bundles its own CUDA libs),
# the GPU base added apt-get juggling for python3.12, and the resulting
# image bifurcation just doubled build time. Single image is cleaner.
#
# Build:
#   docker build -t monitorul-embed services/embed
#
# Run on CPU:
#   docker run --rm -p 8000:8000 -v monitorul_hf-cache:/cache/hf monitorul-embed
#
# Run on GPU (host needs nvidia-container-toolkit):
#   docker run --rm -p 8000:8000 --gpus all \
#              -v monitorul_hf-cache:/cache/hf monitorul-embed
#
# Or use docker compose at the project root: `docker compose up` (CPU)
# or `docker compose --profile gpu up` (GPU).

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/cache/hf \
    TRANSFORMERS_CACHE=/cache/hf/transformers \
    SENTENCE_TRANSFORMERS_HOME=/cache/hf/sentence-transformers

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

COPY server.py /app/services/embed/server.py
RUN mkdir -p /app/services/embed \
 && touch /app/services/embed/__init__.py /app/services/__init__.py

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

ENV EMBED_MODEL_ID=BAAI/bge-m3 \
    EMBED_MAX_BATCH_SIZE=32 \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

CMD ["uvicorn", "services.embed.server:app", "--host", "0.0.0.0", "--port", "8000"]
