# Multi-stage build for log-mcp-server using uv
FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /build

COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY README.md ./

RUN uv sync --frozen --no-dev --no-editable

# Stage 2: Runtime
FROM python:3.11-slim AS runtime

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN groupadd -r -g 1000 log-mcp && \
    useradd -r -u 1000 -g log-mcp -m -d /app -s /bin/bash log-mcp

WORKDIR /app

COPY --from=builder /build/.venv /app/.venv

RUN mkdir -p /app/config /app/logs && \
    chown -R log-mcp:log-mcp /app/config /app/logs

USER log-mcp

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    LOG_LEVEL=INFO

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import socket,os; s=socket.socket(); s.settimeout(3); \
        s.connect(('127.0.0.1', int(os.environ.get('MCP_PORT','8000'))))" \
        || exit 1

ENTRYPOINT ["log-mcp-server"]
CMD []

LABEL org.opencontainers.image.title="log-mcp-server" \
      org.opencontainers.image.description="FastMCP-based log MCP server with pluggable backends" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="MIT"
