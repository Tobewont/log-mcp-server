"""Loki 鉴权请求头的构造。

鉴权请求头与租户隔离的单一权威来源。共享 HTTP client 和任何一次性
请求都应当走 ``build_headers``。
"""
from __future__ import annotations

import base64
from typing import Dict, Optional

from ...config import LogConfig
from ...utils.errors import ValidationError


def build_headers(
    config: LogConfig,
    tenant: Optional[str] = None,
    *,
    json_content: bool = False,
) -> Dict[str, str]:
    """Build HTTP headers for a Loki API call.

    - Sends ``Authorization: Bearer <token>`` if ``bearer_token`` is set,
      otherwise ``Authorization: Basic ...`` if both username and password
      are set, otherwise no auth header.
    - Always sends ``X-Scope-OrgID`` when ``tenant`` is provided.
    """
    headers = {"Accept": "application/json"}
    if json_content:
        headers["Content-Type"] = "application/json"

    bearer = config.get_bearer_token()
    password = config.get_password()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif config.username and password:
        raw = f"{config.username}:{password}".encode("utf-8")
        token = base64.b64encode(raw).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    if tenant:
        headers["X-Scope-OrgID"] = tenant
    return headers


def validate_tenant(tenant: str) -> None:
    """Tenant identifier sanity check.

    Raises ``ValidationError`` for consistency with other backend-side
    validation.
    """
    if not tenant or not tenant.strip():
        raise ValidationError("Tenant cannot be empty")
