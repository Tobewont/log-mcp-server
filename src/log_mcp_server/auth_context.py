"""Per-request client tenant filter helpers.

The MCP client tells the server which tenants it is allowed to query in
one of two equivalent ways:

* HTTP transports (streamable-http / SSE): the ``X-Allowed-Tenants``
  request header (comma-separated).  The streamable-http server passes
  the underlying Starlette ``Request`` through ``ServerMessageMetadata``
  to every tool invocation, so tools read the header from
  ``ctx.request_context.request`` (this works across the worker task
  boundary that an ASGI middleware + contextvar approach cannot reach).

* Stdio transport: ``LOKI_CLIENT_TENANTS`` env var on the server
  process (set by the MCP client config, e.g. ``mcp.json``'s ``env``
  block).  Stdio has no Starlette ``Request`` so we fall back to the
  process-level setting.

In both cases the value is a **comma-separated** subset of the server-
configured ``LOKI_TENANTS``.  The log-query tools refuse to run unless
the subset is set; ``health_check`` is exempt and stays available so
operators can introspect the active scope.

This is a *filter*, not authentication: anyone who can edit the MCP
client config can also change the header / env.  Use it to prevent AI /
user mistakes, not to enforce security boundaries.
"""
from __future__ import annotations

from typing import List, Optional


def parse_tenant_list(value: Optional[str]) -> Optional[List[str]]:
    """Parse a comma-separated tenant list, ignoring whitespace and empties.

    Returns ``None`` for ``None``, empty / whitespace-only strings, or
    inputs whose tokens are all blank (e.g. ``",,,"``).  These are all
    treated as "unset" rather than "forbid all" — a malformed or absent
    header should not silently break the request.
    """
    if value is None:
        return None
    items = [t.strip() for t in value.split(",") if t.strip()]
    if not items:
        return None
    return items
