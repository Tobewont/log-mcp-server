# Loki MCP Server

A Python-based [Model Context Protocol (MCP)](https://github.com/modelcontextprotocol/mcp) server for querying Grafana Loki logs via HTTP API. This server enables AI assistants to access, query, and analyze log data from Loki directly through standardized MCP tools.

## Features

- 🔍 **Query Loki logs** with full LogQL support
- 🏷️ **Browse labels and values** for data exploration
- 🏢 **Multi-tenant support** with per-request tenant specification
- ❤️ **Health monitoring** of Loki server status
- 🔐 **Authentication support** (Basic Auth, Bearer Token)
- ⚡ **Async HTTP client** for optimal performance
- 🛡️ **Comprehensive error handling** and logging

## Quick Start

### Installation

#### Option 1: Docker (Recommended)

```bash
# Clone the repository
git clone https://github.com/your-org/loki-mcp-server.git
cd loki-mcp-server

# Start with Docker Compose (includes Loki and Grafana)
docker-compose up -d

# Or build and run just the MCP server
docker build -t loki-mcp-server .
docker run -e LOKI_ADDR=http://your-loki-server:3100 loki-mcp-server
```

#### Option 2: Python Package

```bash
# Install from PyPI (when published)
pip install loki-mcp-server

# Or install from source
git clone https://github.com/your-org/loki-mcp-server.git
cd loki-mcp-server
pip install -e .
```

### Configuration

Configure the server using environment variables:

```bash
export LOKI_ADDR="https://your-loki-server.com"
export LOKI_USERNAME="your-username"  # Optional
export LOKI_PASSWORD="your-password"  # Optional
# OR
export LOKI_BEARER_TOKEN="your-token"  # Optional
```

Alternatively, create a `loki-config.yaml` file:

```yaml
addr: "https://your-loki-server.com"
username: "your-username"
password: "your-password"
# OR
bearer_token: "your-token"
```

### Usage with Claude Desktop

Add to your Claude Desktop configuration:

```json
{
  "mcpServers": {
    "loki": {
      "command": "loki-mcp-server",
      "env": {
        "LOKI_ADDR": "https://your-loki-server.com"
      }
    }
  }
}
```

## Docker Deployment

### 🐳 Docker Compose (Recommended for Development)

The easiest way to get started is using Docker Compose, which includes Loki, Grafana, and the MCP server:

```bash
# Clone the repository
git clone https://github.com/your-org/loki-mcp-server.git
cd loki-mcp-server

# Copy environment file and customize
cp env.example .env
# Edit .env with your configuration

# Start all services
docker-compose up -d

# View logs
docker-compose logs -f loki-mcp-server

# Stop services
docker-compose down
```

This will start:
- **Loki MCP Server** - The main MCP server
- **Loki** - Log aggregation system (http://localhost:3100)
- **Grafana** - Log visualization (http://localhost:3000, admin/admin)

### 🏗️ Docker Build

Build and run just the MCP server container:

```bash
# Build the image
docker build -t loki-mcp-server:latest .

# Run with environment variables
docker run -d \
  --name loki-mcp-server \
  -e LOKI_ADDR=http://your-loki-server:3100 \
  -e LOKI_USERNAME=your-username \
  -e LOKI_PASSWORD=your-password \
  loki-mcp-server:latest

# Run with configuration file
docker run -d \
  --name loki-mcp-server \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/logs:/app/logs \
  loki-mcp-server:latest

# View logs
docker logs -f loki-mcp-server
```

### 🔧 Docker Configuration

#### Environment Variables
All configuration can be passed via environment variables:

```bash
# Required
LOKI_ADDR=http://loki:3100

# Authentication (choose one)
LOKI_USERNAME=your-username
LOKI_PASSWORD=your-password
# OR
LOKI_BEARER_TOKEN=your-token

# Optional settings
LOKI_ORG_ID=your-org-id
LOKI_TLS_SKIP_VERIFY=false
LOKI_CONNECT_TIMEOUT=10.0
LOKI_READ_TIMEOUT=30.0
LOKI_DEFAULT_LIMIT=1000
LOKI_MAX_LIMIT=5000
```

#### Volume Mounts
- **Configuration**: `-v /path/to/config:/app/config:ro`
- **Logs**: `-v /path/to/logs:/app/logs`
- **Environment file**: `-v /path/to/.env:/app/.env:ro`

#### Health Check
The container includes a built-in health check:

```bash
# Check container health
docker inspect --format='{{.State.Health.Status}}' loki-mcp-server

# Manual health check
docker exec loki-mcp-server python -c "from loki_mcp_server.config import LokiConfig; print('OK' if LokiConfig().addr else 'FAIL')"
```

### 🚀 Production Deployment

For production environments:

```yaml
# docker-compose.prod.yml
version: '3.8'
services:
  loki-mcp-server:
    image: loki-mcp-server:latest
    restart: unless-stopped
    environment:
      - LOKI_ADDR=https://your-production-loki.com
      - LOKI_BEARER_TOKEN_FILE=/run/secrets/loki_token
    secrets:
      - loki_token
    volumes:
      - ./config:/app/config:ro
      - logs:/app/logs
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: '0.5'
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

secrets:
  loki_token:
    file: ./secrets/loki_token.txt

volumes:
  logs:
```

### 🐛 Troubleshooting Docker

#### Common Issues

**Container won't start:**
```bash
# Check logs
docker logs loki-mcp-server

# Check configuration
docker exec loki-mcp-server env | grep LOKI_
```

**Connection issues:**
```bash
# Test Loki connectivity from container
docker exec loki-mcp-server curl -f http://loki:3100/ready

# Check network connectivity
docker network ls
docker network inspect loki-mcp-network
```

**Permission issues:**
```bash
# Check file permissions
docker exec loki-mcp-server ls -la /app/config /app/logs

# Fix permissions
sudo chown -R 1000:1000 ./config ./logs
```

#### Debug Mode

Run container in debug mode:

```bash
# Interactive shell
docker run -it --rm \
  -e LOKI_ADDR=http://loki:3100 \
  loki-mcp-server:latest \
  /bin/bash

# Debug with Python
docker run -it --rm \
  -e LOKI_ADDR=http://loki:3100 \
  loki-mcp-server:latest \
  python -c "from loki_mcp_server.config import LokiConfig; print(LokiConfig().get_safe_config())"
```

## Available Tools

### 🔍 `query_loki`
Query logs from Loki using LogQL syntax.

**Parameters:**
- `tenant` (required): Tenant name for multi-tenant setups
- `query` (required): LogQL query string (e.g., `{job="app"} |= "error"`)
- `start` (optional): Start time in ISO 8601 format
- `end` (optional): End time in ISO 8601 format  
- `limit` (optional): Maximum number of entries (default: 1000, max: 5000)
- `direction` (optional): Query direction (`forward` or `backward`)

**Example:**
```
Query: {job="nginx"} |= "error"
Tenant: production
Start: 2023-12-01T10:00:00Z
End: 2023-12-01T11:00:00Z
Limit: 100
```

### 🏷️ `get_labels`
Get all available labels for a tenant.

**Parameters:**
- `tenant` (required): Tenant name

### 🔖 `get_label_values`
Get all values for a specific label.

**Parameters:**
- `tenant` (required): Tenant name
- `label` (required): Label name

### 🏢 `get_tenants`
Discover all available tenants.

**Parameters:** None

### ❤️ `health_check`
Check Loki server health and get current time.

**Parameters:** None

## Configuration Options

| Environment Variable | Description | Default |
|---------------------|-------------|---------|
| `LOKI_ADDR` | Loki server address | `http://localhost:3100` |
| `LOKI_USERNAME` | Username for basic auth | None |
| `LOKI_PASSWORD` | Password for basic auth | None |
| `LOKI_BEARER_TOKEN` | Bearer token for auth | None |
| `LOKI_BEARER_TOKEN_FILE` | Path to bearer token file | None |
| `LOKI_ORG_ID` | Organization ID header | None |
| `LOKI_TLS_SKIP_VERIFY` | Skip TLS verification | `false` |
| `LOKI_CONNECT_TIMEOUT` | Connection timeout (seconds) | `10.0` |
| `LOKI_READ_TIMEOUT` | Read timeout (seconds) | `30.0` |
| `LOKI_DEFAULT_LIMIT` | Default query result limit | `1000` |
| `LOKI_MAX_LIMIT` | Maximum query result limit | `5000` |

## Development

### Setup Development Environment

```bash
# Clone repository
git clone https://github.com/your-org/loki-mcp-server.git
cd loki-mcp-server

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e .[dev]
```

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=loki_mcp_server

# Run specific test file
pytest tests/test_config.py
```

### Code Quality

```bash
# Format code
black src/ tests/

# Lint code
flake8 src/ tests/

# Type checking
mypy src/
```

### Running the Server

```bash
# Run directly
python -m loki_mcp_server.main

# Or use the installed command
loki-mcp-server
```

## Architecture

The server is built with:

- **Python 3.8+** with modern async/await patterns
- **httpx** for async HTTP client functionality
- **pydantic** for configuration management and validation
- **structlog** for structured logging
- **MCP SDK** for Model Context Protocol implementation

### Project Structure

```
src/loki_mcp_server/
├── __init__.py
├── main.py              # MCP server entry point
├── config.py            # Configuration management
├── client/
│   ├── loki_client.py   # Loki HTTP API client
│   ├── http_client.py   # Generic HTTP client
│   └── auth.py          # Authentication handling
├── tools/               # MCP tool implementations
│   ├── health_check.py
│   ├── tenants.py
│   ├── query.py
│   └── labels.py
└── utils/
    ├── errors.py        # Error handling
    └── logging.py       # Logging setup
```

## Migration from TypeScript Version

This Python version replaces the previous TypeScript implementation with the following changes:

### ⚠️ Breaking Changes

- **Removed logcli dependency**: Now uses only HTTP API
- **Removed `LOKI_TENANT_ID` environment variable**: Use `tenant` parameter in tools instead
- **New tool parameters**: All query tools now require a `tenant` parameter
- **Different package name**: `loki-mcp-server` (Python) vs `simple-loki-mcp` (TypeScript)

### Migration Steps

1. **Update configuration**: Remove `LOKI_TENANT_ID` from environment variables
2. **Update tool calls**: Add `tenant` parameter to all Loki query tools
3. **Install Python version**: `pip install loki-mcp-server`
4. **Update MCP configuration**: Change command to `loki-mcp-server`

### New Features

- **Tenant discovery**: `get_tenants` tool to find available tenants
- **Health monitoring**: `health_check` tool for server status
- **Better error handling**: Structured error responses with context
- **Improved performance**: Async HTTP client with connection pooling

## Troubleshooting

### Common Issues

**Connection Errors**
- Verify `LOKI_ADDR` is correct and accessible
- Check authentication credentials
- Ensure Loki server is running and healthy

**Authentication Errors**
- Verify username/password or bearer token
- Check if multi-tenant mode requires specific headers

**Query Errors**
- Validate LogQL syntax
- Ensure tenant exists and is accessible
- Check time range parameters

**Tool Not Found Errors**
- Verify MCP server is properly registered
- Check tool names match exactly
- Ensure all required parameters are provided

### Debug Mode

Enable debug logging:

```bash
export LOG_LEVEL=DEBUG
loki-mcp-server
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass (`pytest`)
6. Run code quality checks (`black`, `flake8`, `mypy`)
7. Commit your changes (`git commit -m 'Add amazing feature'`)
8. Push to the branch (`git push origin feature/amazing-feature`)
9. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [Grafana Loki](https://grafana.com/oss/loki/) for the excellent log aggregation system
- [Model Context Protocol](https://github.com/modelcontextprotocol/mcp) for the standardized AI integration framework
- The original TypeScript implementation that inspired this Python version