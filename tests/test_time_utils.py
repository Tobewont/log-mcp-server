"""Tests for utils.time_utils."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from log_mcp_server.utils.errors import ValidationError
from log_mcp_server.utils.time_utils import (
    format_in_tz,
    from_unix_ns,
    parse_user_time,
    resolve_time_range,
    to_unix_ns,
)


class TestParseUserTime:
    def test_z_suffix_treated_as_utc(self):
        dt = parse_user_time("2025-01-01T00:00:00Z")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)

    def test_offset_normalised_to_utc(self):
        dt = parse_user_time("2025-01-01T08:00:00+08:00")
        assert dt.utcoffset() == timedelta(0)
        assert dt.hour == 0

    def test_naive_treated_as_utc(self):
        dt = parse_user_time("2025-01-01T00:00:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)

    def test_invalid_raises(self):
        with pytest.raises(ValidationError):
            parse_user_time("not-a-date")

    def test_empty_raises(self):
        with pytest.raises(ValidationError):
            parse_user_time("")


class TestUnixNs:
    def test_round_trip(self):
        dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
        assert from_unix_ns(to_unix_ns(dt)) == dt

    def test_naive_rejected(self):
        with pytest.raises(ValidationError):
            to_unix_ns(datetime(2025, 1, 1))

    def test_from_str(self):
        ts = "1700000000000000000"
        dt = from_unix_ns(ts)
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)


class TestFormatInTz:
    def test_utc_to_shanghai(self):
        dt = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
        out = format_in_tz(dt, "Asia/Shanghai")
        assert out.startswith("2025-01-01T08:00:00")
        assert "Z" not in out  # offset format, never Z+offset junk

    def test_naive_rejected(self):
        with pytest.raises(ValidationError):
            format_in_tz(datetime(2025, 1, 1), "UTC")


class TestResolveTimeRange:
    def test_both_missing_uses_default_window(self):
        s, e = resolve_time_range(None, None, default_minutes=30)
        delta = e - s
        assert delta == timedelta(minutes=30)
        assert s.tzinfo is not None
        assert e.tzinfo is not None

    def test_only_end_given(self):
        s, e = resolve_time_range(None, "2025-01-01T01:00:00Z", 30)
        assert e == datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc)
        assert s == datetime(2025, 1, 1, 0, 30, tzinfo=timezone.utc)

    def test_only_start_given(self):
        s, e = resolve_time_range("2025-01-01T00:00:00Z", None, 30)
        assert s == datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
        assert e > s

    def test_both_given_offset(self):
        s, e = resolve_time_range(
            "2025-01-01T00:00:00+08:00",
            "2025-01-01T01:00:00+08:00",
            30,
        )
        # Both normalised to UTC, no naive/aware mixing.
        assert s == datetime(2024, 12, 31, 16, 0, tzinfo=timezone.utc)
        assert e == datetime(2024, 12, 31, 17, 0, tzinfo=timezone.utc)

    def test_start_must_be_before_end(self):
        with pytest.raises(ValidationError):
            resolve_time_range(
                "2025-01-01T01:00:00Z",
                "2025-01-01T00:00:00Z",
                30,
            )

    def test_malformed_z_not_globally_replaced(self):
        # Only a *trailing* Z is treated as UTC; a Z embedded in junk
        # input must not be silently mangled.
        with pytest.raises(ValidationError):
            from log_mcp_server.utils.time_utils import parse_user_time

            parse_user_time("2025-Z01-01T00:00:00")
