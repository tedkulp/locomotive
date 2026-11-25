# syntax=docker/dockerfile:1

# ---- frontend: static files served by the backend
FROM node:22.22.0-bookworm-slim AS frontend
WORKDIR /src/frontend
RUN npm install -g pnpm@12.6.0
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# ---- backend
FROM python:3.13-slim-bookworm AS app
ARG MISE_VERSION=2026.11.5
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git openssh-client tini \
 && rm -rf /var/lib/apt/lists/*

# uv runs the app and backs mise's pipx tools; mise installs each checkout's pinned Ansible per Run.
COPY --from=ghcr.io/astral-sh/uv:0.9.27 /uv /uvx /usr/local/bin/
RUN arch="$([ "$TARGETARCH" = "arm64" ] && echo arm64 || echo x64)" \
 && curl -fsSL -o /usr/local/bin/mise \
      "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-${arch}" \
 && chmod 0755 /usr/local/bin/mise \
 && mise --version

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
COPY README.md ./
RUN uv sync --frozen --no-dev
COPY --from=frontend /src/frontend/dist /app/static

# Everything mutable lives in the data volume: database, mirror, Run dirs, logs, tool caches.
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/var/lib/locomotive/home \
    LOCOMOTIVE_DATA_DIR=/var/lib/locomotive \
    LOCOMOTIVE_STATIC_DIR=/app/static \
    MISE_DATA_DIR=/var/lib/locomotive/mise \
    MISE_CACHE_DIR=/var/lib/locomotive/cache/mise \
    UV_CACHE_DIR=/var/lib/locomotive/cache/uv \
    UV_PYTHON_DOWNLOADS=automatic \
    MISE_PIPX_UVX=true \
    PYTHONUNBUFFERED=1

RUN mkdir -p /var/lib/locomotive/home && chown -R 1000:1000 /var/lib/locomotive
USER 1000:1000
VOLUME /var/lib/locomotive
EXPOSE 8000
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "locomotive"]
