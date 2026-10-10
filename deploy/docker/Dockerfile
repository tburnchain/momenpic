# syntax=docker/dockerfile:1.7
#
# ACVM application image — one Dockerfile, three targets:
#   api     uvicorn acvm.api.app:app                        (:8000  /healthz /readyz /metrics)
#   worker  python -m acvm.orchestration.worker             (:9464  /metrics)  — also runs loops / migrate
#   render  python -m acvm.orchestration.worker --queue acvm-render  (+ ffmpeg, fonts-noto-cjk)
#
# Build from the repository root:
#   docker buildx build -f deploy/docker/Dockerfile --target api -t ghcr.io/<org>/acvm-api:<ver> .
#
# Supply chain (§10): base images are pinned by digest through build args. CI (release.yml) passes the
# digests; leaving them empty falls back to the tag for local builds only.
#   --build-arg PYTHON_DIGEST=sha256:...   --build-arg UV_DIGEST=sha256:...
#
# Runtime contract: non-root uid 10001, read-only root filesystem (writable: /tmp tmpfs, /app/var volume),
# tini as PID 1, no secrets baked in (everything arrives through env / Vault AppRole at run time).

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG PYTHON_DIGEST=
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.5
ARG UV_DIGEST=

# ─────────────────────────── uv binary ───────────────────────────
FROM ${UV_IMAGE}${UV_DIGEST:+@}${UV_DIGEST} AS uv

# ─────────────────────────── builder: resolve the locked venv ───────────────────────────
FROM ${PYTHON_IMAGE}${PYTHON_DIGEST:+@}${PYTHON_DIGEST} AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_FROZEN=1
WORKDIR /app
# 1) third-party dependencies only — cached until uv.lock / pyproject.toml change
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project
# 2) the project itself. Kept as an editable install rooted at /app/src on purpose: `acvm db upgrade` resolves
#    alembic.ini relative to the source tree (src/acvm/cli.py → ../../alembic.ini = /app/alembic.ini).
COPY pyproject.toml uv.lock alembic.ini ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
 && /app/.venv/bin/python -m compileall -q /app/src \
 && /app/.venv/bin/python -c "import acvm, acvm.cli, acvm.db.migrations; print('acvm import ok')"

# ─────────────────────────── runtime base (root, shared by all targets) ───────────────────────────
FROM ${PYTHON_IMAGE}${PYTHON_DIGEST:+@}${PYTHON_DIGEST} AS runtime-base
ARG VERSION=0.0.0-dev
ARG VCS_REF=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z
ARG SOURCE_URL=https://github.com/tburn-labs/acvm
LABEL org.opencontainers.image.title="acvm" \
      org.opencontainers.image.description="ACVM — AI Affiliate Content Vending Machine" \
      org.opencontainers.image.vendor="TBURN LABS" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="${PYTHON_IMAGE}"
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 acvm \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin acvm \
 && mkdir -p /app/var \
 && chown 10001:10001 /app/var
ENV PATH=/app/.venv/bin:$PATH \
    VIRTUAL_ENV=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    TZ=Asia/Seoul \
    ACVM_DATA_DIR=/app/var
WORKDIR /app
COPY --from=builder --chown=0:0 /app/.venv /app/.venv
COPY --from=builder --chown=0:0 /app/src /app/src
COPY --chown=0:0 alembic.ini /app/alembic.ini
ENTRYPOINT ["/usr/bin/tini", "--"]

# ─────────────────────────── api ───────────────────────────
FROM runtime-base AS api
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["uvicorn", "acvm.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

# ─────────────────────────── worker (also: loops, migrate, CLI) ───────────────────────────
FROM runtime-base AS worker
USER 10001:10001
EXPOSE 9464
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9464/metrics', timeout=4)"]
CMD ["python", "-m", "acvm.orchestration.worker"]

# ─────────────────────────── render (worker + ffmpeg + CJK fonts) ───────────────────────────
FROM runtime-base AS render
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk fontconfig \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f \
 && ffmpeg -hide_banner -version | head -n1 \
 && fc-list :lang=ko family | grep -q "Noto Sans CJK"
ENV ACVM_RENDER__FFMPEG=/usr/bin/ffmpeg \
    ACVM_RENDER__FFPROBE=/usr/bin/ffprobe \
    ACVM_RENDER__FONT_FAMILY="Noto Sans CJK KR"
USER 10001:10001
EXPOSE 9464
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9464/metrics', timeout=4)"]
CMD ["python", "-m", "acvm.orchestration.worker", "--queue", "acvm-render"]
