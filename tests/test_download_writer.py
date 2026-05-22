"""Unit tests for the download writer."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from log_mcp_server.backends.base import LogEntry
from log_mcp_server.downloads.writer import (
    SUPPORTED_FORMATS,
    build_filename,
    write_download,
)


def _entry(**overrides) -> LogEntry:
    base = dict(
        timestamp=datetime(2026, 5, 14, 8, 30, 0, tzinfo=timezone.utc),
        labels={"app": "drama", "env": "prod"},
        line="hello, world",
        tenant="tenant-a",
        cluster="loki-bj:3100",
    )
    base.update(overrides)
    return LogEntry(**base)


def test_supported_formats_are_exactly_three():
    assert set(SUPPORTED_FORMATS) == {"jsonl", "csv", "txt"}


def test_unsupported_format_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        write_download(
            [_entry()],
            target_path=tmp_path / "x.bin",
            fmt="bin",
            timezone="UTC",
        )


def test_jsonl_round_trip(tmp_path: Path):
    target = tmp_path / "logs.jsonl"
    result = write_download(
        [_entry(line="a"), _entry(line="b")],
        target_path=target,
        fmt="jsonl",
        timezone="UTC",
    )
    assert result.entry_count == 2
    assert result.fmt == "jsonl"
    assert result.path == target.resolve()
    rows = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["line"] == "a"
    assert rows[0]["labels"] == {"app": "drama", "env": "prod"}
    assert rows[0]["tenant"] == "tenant-a"
    assert rows[0]["cluster"] == "loki-bj:3100"


def test_jsonl_smart_parses_json_object_lines(tmp_path: Path):
    """JSON-in-JSON should NOT produce \\\\\" chains.

    When the log line is itself JSON, embed it as a nested object so a
    user reading the file sees ``"line":{"level":"info"...}`` instead
    of ``"line":"{\\\\"level\\\\":\\\\"info\\\\"...}"``.
    """
    raw = '{"level":"info","msg":"Response","req_body":"{\\"id\\":1}"}'
    target = tmp_path / "smart.jsonl"
    write_download(
        [_entry(line=raw)], target_path=target, fmt="jsonl", timezone="UTC"
    )
    parsed = json.loads(target.read_text("utf-8").splitlines()[0])
    # The line field is now a nested object, not a string.
    assert isinstance(parsed["line"], dict)
    assert parsed["line"]["level"] == "info"
    # And the inner JSON-string field is preserved as a string (it would
    # itself need parsing if the user wants the next level).
    assert parsed["line"]["req_body"] == '{"id":1}'
    # Critically: NO triple-backslash escaping in the on-disk text.
    text = target.read_text("utf-8")
    assert "\\\\\\\"" not in text  # no \\\" sequence anywhere
    assert "\\\\" not in text  # no double-backslash either


def test_jsonl_smart_parses_json_array_lines(tmp_path: Path):
    """Arrays at the top level should also be embedded structurally."""
    target = tmp_path / "arr.jsonl"
    write_download(
        [_entry(line='[1, 2, 3]')],
        target_path=target,
        fmt="jsonl",
        timezone="UTC",
    )
    parsed = json.loads(target.read_text("utf-8").splitlines()[0])
    assert parsed["line"] == [1, 2, 3]


def test_jsonl_keeps_non_json_lines_as_strings(tmp_path: Path):
    """Plain text / partial JSON / numbers must remain string-typed."""
    target = tmp_path / "mixed.jsonl"
    write_download(
        [
            _entry(line="GET /index.html 200"),
            _entry(line='{"unclosed":'),  # invalid JSON
            _entry(line=" "),  # whitespace only
            _entry(line="42"),  # numeric — JSON, but we only embed object/array
        ],
        target_path=target,
        fmt="jsonl",
        timezone="UTC",
    )
    rows = [json.loads(line) for line in target.read_text("utf-8").splitlines()]
    assert all(isinstance(r["line"], str) for r in rows)
    assert rows[0]["line"] == "GET /index.html 200"
    assert rows[1]["line"] == '{"unclosed":'
    assert rows[2]["line"] == " "
    assert rows[3]["line"] == "42"


def test_jsonl_handles_unicode_in_line(tmp_path: Path):
    target = tmp_path / "u.jsonl"
    write_download(
        [_entry(line="日志✓")],
        target_path=target,
        fmt="jsonl",
        timezone="UTC",
    )
    parsed = json.loads(target.read_text("utf-8").splitlines()[0])
    assert parsed["line"] == "日志✓"


def test_csv_columns_and_quoting(tmp_path: Path):
    target = tmp_path / "logs.csv"
    write_download(
        [
            _entry(line="plain"),
            _entry(line='has "quotes" and ,commas'),
        ],
        target_path=target,
        fmt="csv",
        timezone="UTC",
    )
    with target.open("r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)
    assert header == ["time", "tenant", "cluster", "labels", "line"]
    assert rows[0][4] == "plain"
    assert rows[1][4] == 'has "quotes" and ,commas'
    # labels column is JSON-encoded so commas inside don't break columns
    assert json.loads(rows[0][3]) == {"app": "drama", "env": "prod"}


def test_txt_format_one_line_per_entry(tmp_path: Path):
    target = tmp_path / "logs.txt"
    write_download(
        [_entry(line="msg1"), _entry(line="msg2")],
        target_path=target,
        fmt="txt",
        timezone="UTC",
    )
    lines = target.read_text("utf-8").splitlines()
    assert len(lines) == 2
    # Sanity: includes time, tenant, labels, message
    assert "tenant-a" in lines[0]
    assert "loki-bj:3100" in lines[0]
    assert "app=drama" in lines[0]
    assert lines[0].endswith("msg1")


def test_empty_entries_writes_header_only_or_blank(tmp_path: Path):
    """Even zero-entry downloads must produce a deterministic file."""
    for fmt, expect_size in [("jsonl", 0), ("txt", 0), ("csv", None)]:
        target = tmp_path / f"empty.{fmt}"
        result = write_download([], target_path=target, fmt=fmt, timezone="UTC")
        assert result.entry_count == 0
        assert target.exists()
        if expect_size == 0:
            assert target.stat().st_size == 0
        else:
            # csv: header row only
            expected = "time,tenant,cluster,labels,line"
            assert target.read_text("utf-8").strip() == expected


def test_build_filename_sanitises_tenant_label():
    name = build_filename(
        tenant_label="ops/team a@2026",
        fmt="jsonl",
        now=datetime(2026, 5, 14, 8, 30, 0, tzinfo=timezone.utc),
    )
    # No path separators or whitespace, valid timestamp prefix.
    assert "/" not in name
    assert " " not in name
    assert name.endswith(".jsonl")
    assert name.startswith("logs-20260514T083000Z-")


def test_build_filename_rejects_unknown_format():
    with pytest.raises(ValueError):
        build_filename(
            tenant_label="t", fmt="xml", now=datetime.now(tz=timezone.utc)
        )


def test_build_filename_appends_suffix_when_provided():
    """Two calls in the same second with different suffixes must differ."""
    now = datetime(2026, 5, 14, 8, 30, 0, tzinfo=timezone.utc)
    a = build_filename(tenant_label="t", fmt="jsonl", now=now, suffix="aabb")
    b = build_filename(tenant_label="t", fmt="jsonl", now=now, suffix="ccdd")
    assert a != b
    assert a.endswith("-aabb.jsonl")
    assert b.endswith("-ccdd.jsonl")


def test_build_filename_sanitises_suffix():
    """Suffix is sanitised the same way as the tenant label."""
    name = build_filename(
        tenant_label="t",
        fmt="jsonl",
        now=datetime(2026, 5, 14, 8, 30, 0, tzinfo=timezone.utc),
        suffix="bad/chars here",
    )
    assert "/" not in name
    assert " " not in name
