"""Unit tests for the download token registry."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from log_mcp_server.downloads.registry import DownloadRegistry, media_type_for


@pytest.mark.asyncio
async def test_register_returns_unique_tokens(tmp_path: Path):
    reg = DownloadRegistry(ttl_seconds=60)
    f1 = tmp_path / "a.jsonl"
    f2 = tmp_path / "b.jsonl"
    f1.write_text("a")
    f2.write_text("b")
    e1 = await reg.register(path=f1, fmt="jsonl", download_filename="a.jsonl")
    e2 = await reg.register(path=f2, fmt="jsonl", download_filename="b.jsonl")
    assert e1.token != e2.token
    assert len(e1.token) >= 32  # token_urlsafe(32) ≥ 43 chars
    assert e1.media_type == "application/x-ndjson"
    assert e2.path == f2.resolve()


@pytest.mark.asyncio
async def test_get_returns_none_for_missing_token(tmp_path: Path):
    reg = DownloadRegistry(ttl_seconds=60)
    assert await reg.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_get_returns_none_for_expired_and_unlinks(tmp_path: Path):
    reg = DownloadRegistry(ttl_seconds=60)
    f = tmp_path / "exp.jsonl"
    f.write_text("x")
    entry = await reg.register(path=f, fmt="jsonl", download_filename="exp.jsonl")
    # Force-expire the entry by mutating the dataclass via internal store
    async with reg._lock:  # type: ignore[attr-defined]
        reg._items[entry.token] = entry.__class__(  # type: ignore[arg-type]
            token=entry.token,
            path=entry.path,
            media_type=entry.media_type,
            download_filename=entry.download_filename,
            expires_at=time.time() - 1.0,
        )
    assert await reg.get(entry.token) is None
    assert not f.exists(), "expired entries must remove their file"


@pytest.mark.asyncio
async def test_discard_removes_entry_and_file(tmp_path: Path):
    reg = DownloadRegistry(ttl_seconds=60)
    f = tmp_path / "d.jsonl"
    f.write_text("x")
    entry = await reg.register(path=f, fmt="jsonl", download_filename="d.jsonl")
    await reg.discard(entry.token)
    assert await reg.get(entry.token) is None
    assert not f.exists()


@pytest.mark.asyncio
async def test_cleanup_removes_only_expired(tmp_path: Path):
    reg = DownloadRegistry(ttl_seconds=60)
    fresh_file = tmp_path / "fresh.jsonl"
    stale_file = tmp_path / "stale.jsonl"
    fresh_file.write_text("a")
    stale_file.write_text("b")
    fresh = await reg.register(
        path=fresh_file, fmt="jsonl", download_filename="fresh.jsonl"
    )
    stale = await reg.register(
        path=stale_file, fmt="jsonl", download_filename="stale.jsonl"
    )
    async with reg._lock:  # type: ignore[attr-defined]
        reg._items[stale.token] = stale.__class__(  # type: ignore[arg-type]
            token=stale.token,
            path=stale.path,
            media_type=stale.media_type,
            download_filename=stale.download_filename,
            expires_at=time.time() - 1.0,
        )
    removed = await reg.cleanup_expired()
    assert removed == 1
    assert await reg.get(fresh.token) is not None
    assert await reg.get(stale.token) is None
    assert fresh_file.exists()
    assert not stale_file.exists()


@pytest.mark.asyncio
async def test_concurrent_registers_yield_distinct_tokens(tmp_path: Path):
    reg = DownloadRegistry(ttl_seconds=60)
    files = []
    for i in range(10):
        f = tmp_path / f"c{i}.jsonl"
        f.write_text(str(i))
        files.append(f)
    entries = await asyncio.gather(
        *[
            reg.register(path=f, fmt="jsonl", download_filename=f.name)
            for f in files
        ]
    )
    assert len({e.token for e in entries}) == 10


def test_media_type_known_formats():
    assert media_type_for("jsonl") == "application/x-ndjson"
    assert media_type_for("csv").startswith("text/csv")
    assert media_type_for("txt").startswith("text/plain")
    assert media_type_for("bin") == "application/octet-stream"


def test_zero_or_negative_ttl_rejected():
    with pytest.raises(ValueError):
        DownloadRegistry(ttl_seconds=0)
    with pytest.raises(ValueError):
        DownloadRegistry(ttl_seconds=-1)
