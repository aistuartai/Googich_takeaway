# syntax=docker/dockerfile:1
# Googich Takeaway container image.
# Base images are pinned by digest; Dependabot proposes updates.

FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv

FROM python:3.13-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, from the lockfile only, so this layer is cached across code changes.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
LABEL org.opencontainers.image.title="Googich Takeaway" \
      org.opencontainers.image.description="Moves Google Photos into Immich using Google Takeout archives" \
      org.opencontainers.image.source="https://github.com/aistuartai/Googich_takeaway" \
      org.opencontainers.image.licenses="MIT"
RUN groupadd --gid 10001 googich \
 && useradd --uid 10001 --gid googich --no-create-home --home-dir /data --shell /usr/sbin/nologin googich \
 && mkdir -p /data && chown googich:googich /data && chmod 700 /data
COPY --from=build --chown=root:root /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GOOGICH_STATE=/data/state.db \
    GOOGICH_HOST=0.0.0.0 \
    GOOGICH_PORT=8080
USER googich
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["googich", "serve"]
