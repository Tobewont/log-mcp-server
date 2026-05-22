"""Integration test for the /<mcp_path>/download/<token> ASGI route.

Bypasses uvicorn / sockets by talking to the route via Starlette's
``TestClient``, but exercises the **real** route handler we mount in
``main._run_http``.  This is the regression net for the bug where the
download URL handed back to the user 404'd because the route was
mounted under a path that the surrounding ingress did not forward.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from log_mcp_server.downloads import DownloadRegistry


def _build_app(registry: DownloadRegistry, mount_path: str) -> Starlette:
    """Build a minimal ASGI app that mounts the same handler used in main.py."""

    async def download_route(request):
        token = request.path_params["token"]
        entry = await registry.get(token)
        if entry is None:
            return PlainTextResponse(
                "Download token not found or expired.", status_code=404
            )
        if not entry.path.exists():
            await registry.discard(token)
            return PlainTextResponse(
                "Download file is missing on the server.", status_code=410
            )
        return FileResponse(
            path=str(entry.path),
            media_type=entry.media_type,
            filename=entry.download_filename,
            background=BackgroundTask(registry.discard, token),
        )

    return Starlette(
        routes=[
            Route(
                f"{mount_path.rstrip('/')}/{{token:str}}",
                download_route,
                methods=["GET"],
            )
        ]
    )


@pytest.mark.asyncio
async def test_serves_registered_file_with_correct_headers(tmp_path: Path):
    reg = DownloadRegistry(ttl_seconds=60)
    f = tmp_path / "ok.jsonl"
    # Use binary write to keep newline bytes deterministic across
    # platforms — Windows otherwise inserts \r\n via write_text().
    f.write_bytes(b'{"line":"hello"}\n')
    entry = await reg.register(
        path=f, fmt="jsonl", download_filename="ok.jsonl"
    )

    app = _build_app(reg, "/mcp/download")
    with TestClient(app) as client:
        r = client.get(f"/mcp/download/{entry.token}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-ndjson"
    assert 'attachment; filename="ok.jsonl"' in r.headers["content-disposition"]
    assert r.text == '{"line":"hello"}\n'


@pytest.mark.asyncio
async def test_successful_download_consumes_token_and_file(tmp_path: Path):
    reg = DownloadRegistry(ttl_seconds=60)
    f = tmp_path / "once.jsonl"
    f.write_bytes(b'{"line":"once"}\n')
    entry = await reg.register(
        path=f, fmt="jsonl", download_filename="once.jsonl"
    )

    app = _build_app(reg, "/mcp/download")
    with TestClient(app) as client:
        first = client.get(f"/mcp/download/{entry.token}")
        second = client.get(f"/mcp/download/{entry.token}")

    assert first.status_code == 200
    assert first.text == '{"line":"once"}\n'
    assert second.status_code == 404
    assert not f.exists()


@pytest.mark.asyncio
async def test_unknown_token_returns_404_with_explicit_text(tmp_path: Path):
    reg = DownloadRegistry(ttl_seconds=60)
    app = _build_app(reg, "/mcp/download")
    with TestClient(app) as client:
        r = client.get("/mcp/download/this-token-was-never-registered")
    # Crucially this is OUR 404 text, not starlette's default 'Not Found'
    # — that's how we know the route was matched.
    assert r.status_code == 404
    assert r.text == "Download token not found or expired."


@pytest.mark.asyncio
async def test_token_outside_mount_path_is_starlette_default_404(tmp_path: Path):
    """Path NOT under the configured mount → starlette's default 'Not Found'.

    This is exactly the bug we fixed: when the ingress only forwarded
    ``/mcp`` but the route was mounted under ``/download`` the user got
    a 9-byte ``Not Found`` body instead of our handler's text.
    """
    reg = DownloadRegistry(ttl_seconds=60)
    app = _build_app(reg, "/mcp/download")
    with TestClient(app) as client:
        r = client.get("/download/some-token")
    assert r.status_code == 404
    # starlette default body is exactly "Not Found"
    assert r.text == "Not Found"


@pytest.mark.asyncio
async def test_file_missing_on_disk_returns_410_and_drops_token(tmp_path: Path):
    """If the file disappeared between register and request, surface 410."""
    reg = DownloadRegistry(ttl_seconds=60)
    f = tmp_path / "vanish.jsonl"
    f.write_text("x", encoding="utf-8")
    entry = await reg.register(
        path=f, fmt="jsonl", download_filename="vanish.jsonl"
    )
    f.unlink()
    app = _build_app(reg, "/mcp/download")
    with TestClient(app) as client:
        r = client.get(f"/mcp/download/{entry.token}")
    assert r.status_code == 410
    # Token should be gone now → second hit returns 404 (token not found)
    with TestClient(app) as client:
        r2 = client.get(f"/mcp/download/{entry.token}")
    assert r2.status_code == 404


@pytest.mark.asyncio
async def test_works_with_sse_mount_path(tmp_path: Path):
    """SSE deployments mount under /sse/download; verify the same handler
    works there with no special-casing."""
    reg = DownloadRegistry(ttl_seconds=60)
    f = tmp_path / "sse.jsonl"
    f.write_text("x", encoding="utf-8")
    entry = await reg.register(
        path=f, fmt="jsonl", download_filename="sse.jsonl"
    )
    app = _build_app(reg, "/sse/download")
    with TestClient(app) as client:
        r = client.get(f"/sse/download/{entry.token}")
    assert r.status_code == 200
    assert r.text == "x"
