FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS runtime

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/root/.local/bin:${PATH}"

WORKDIR /app
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/install-bdpan.sh ./scripts/install-bdpan.sh
RUN ./scripts/install-bdpan.sh --yes && bdpan version --no-check-update
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

EXPOSE 8787 18096
CMD ["uv", "run", "strmflow"]
