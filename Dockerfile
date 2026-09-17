FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS runtime

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_NO_DEV=1 \
    UV_FROZEN=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/root/.local/bin:${PATH}"

WORKDIR /app
COPY scripts/install-bdpan.sh ./scripts/install-bdpan.sh
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl ffmpeg tzdata \
    && ./scripts/install-bdpan.sh --yes \
    && bdpan version --no-check-update \
    && apt-get purge --yes --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

EXPOSE 8787 18096
CMD ["/app/.venv/bin/strmflow"]
