# Multi-stage build for Loki MCP Server
# Stage 1: Builder - Install dependencies and build application
FROM python:3.11-slim as builder

# Set build arguments
ARG DEBIAN_FRONTEND=noninteractive

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /build

# Copy dependency files first (for better layer caching)
COPY requirements.txt requirements-dev.txt pyproject.toml ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY README.md ./

# Install the application
RUN pip install --no-cache-dir -e .

# Stage 2: Runtime - Create minimal runtime image
FROM python:3.11-slim as runtime

# Set runtime arguments
ARG DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies only
RUN apt-get update && apt-get install -y \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user
RUN groupadd -r -g 1000 loki-mcp && \
    useradd -r -u 1000 -g loki-mcp -m -d /app -s /bin/bash loki-mcp

# Set working directory
WORKDIR /app

# Copy Python packages from builder stage
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source
COPY --from=builder --chown=loki-mcp:loki-mcp /build/src ./src
COPY --from=builder --chown=loki-mcp:loki-mcp /build/README.md ./

# Create directories for configuration and logs
RUN mkdir -p /app/config /app/logs && \
    chown -R loki-mcp:loki-mcp /app/config /app/logs

# Switch to non-root user
USER loki-mcp

# Set environment variables
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; from loki_mcp_server.config import LokiConfig; config = LokiConfig(); sys.exit(0 if config.addr else 1)" || exit 1

# Expose no ports (MCP uses stdio)
# EXPOSE - MCP protocol uses stdin/stdout, no network ports needed

# Set entrypoint and default command
ENTRYPOINT ["python", "-m", "loki_mcp_server.main"]
CMD []

# Add labels for metadata
LABEL org.opencontainers.image.title="Loki MCP Server" \
      org.opencontainers.image.description="Model Context Protocol server for Grafana Loki integration" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.authors="Loki MCP Server Team" \
      org.opencontainers.image.source="https://github.com/your-org/loki-mcp-server" \
      org.opencontainers.image.licenses="MIT"
