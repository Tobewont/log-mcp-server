"""Unit tests for the tenant-list parsing helper."""
from __future__ import annotations

import pytest

from log_mcp_server.auth_context import parse_tenant_list


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("a", ["a"]),
        ("a,b", ["a", "b"]),
        ("  a  ,  b  ", ["a", "b"]),
        ("a,,b,", ["a", "b"]),
        (",,,", None),
        ("team-a, team-b ,, team-a", ["team-a", "team-b", "team-a"]),
    ],
)
def test_parse_tenant_list(raw, expected):
    assert parse_tenant_list(raw) == expected
