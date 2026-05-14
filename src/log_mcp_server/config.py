"""Configuration management for the log MCP server.

Configuration sources (highest priority first):

1. Constructor kwargs (used by tests / programmatic configuration)
2. Environment variables (``LOKI_*``, ``LOG_*``, ``MCP_*``)
3. ``.env`` file in the working directory
4. YAML config file (``LOG_CONFIG_PATH`` / ``LOKI_CONFIG_PATH`` env var,
   ``./loki-config.yaml``, ``./.loki-config.yaml`` or
   ``~/.loki-config.yaml``)
5. Built-in defaults
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

import structlog
import yaml
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from .auth_context import parse_tenant_list
from .utils.time_utils import get_timezone

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# YAML config loading (lowest-priority configuration source)
# ---------------------------------------------------------------------------
def _load_yaml_config() -> Dict[str, Any]:
    """Load the first existing YAML config file from the search paths.

    Keys may be written in three forms (all case-insensitive):

    - bare:     ``addr: ...``
    - prefixed: ``loki_addr: ...`` or ``log_default_limit: ...``

    If both bare and prefixed forms appear in the same file, the **bare**
    form wins for determinism (independent of dict iteration order).

    When ``LOG_CONFIG_PATH`` or ``LOKI_CONFIG_PATH`` is set explicitly,
    only that path is tried (no fallback to default search paths).
    """
    explicit = os.environ.get("LOG_CONFIG_PATH") or os.environ.get("LOKI_CONFIG_PATH")
    if explicit:
        candidates: List[Path] = [Path(explicit)]
    else:
        candidates = [
            Path.cwd() / "loki-config.yaml",
            Path.cwd() / ".loki-config.yaml",
            Path.home() / ".loki-config.yaml",
        ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            prefixed: Dict[str, Any] = {}
            bare: Dict[str, Any] = {}
            for key, value in data.items():
                k = key.lower()
                if k.startswith("loki_"):
                    prefixed[k[5:]] = value
                elif k.startswith("log_"):
                    prefixed[k[4:]] = value
                else:
                    bare[k] = value
            normalised = {**prefixed, **bare}
            logger.info("Configuration file loaded", path=str(path))
            return normalised
        except Exception as e:
            logger.warning(
                "Failed to load configuration file",
                path=str(path),
                error=str(e),
            )
            return {}
    return {}


class _YamlConfigSource(PydanticBaseSettingsSource):
    """Pydantic-settings source that pulls values from the YAML config file."""

    def __init__(self, settings_cls: Type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data: Dict[str, Any] = _load_yaml_config()

    def get_field_value(
        self, field, field_name: str
    ) -> Tuple[Any, str, bool]:
        if field_name in self._data:
            return self._data[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name in self.settings_cls.model_fields:
            value, key, _ = self.get_field_value(None, name)
            if value is not None:
                out[key] = value
        return out


class LogConfig(BaseSettings):
    """Top-level config for the log MCP server.

    Loki-specific fields keep the historical ``LOKI_*`` env prefix for
    backward compatibility. Generic / cross-backend fields use ``LOG_*``
    via :class:`AliasChoices`. MCP transport fields use ``MCP_*``.
    """

    model_config = SettingsConfigDict(
        env_prefix="LOKI_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Backend selection -------------------------------------------------
    backend: str = Field(
        default="loki",
        description="Active log backend ('loki' is the only one for now)",
        validation_alias=AliasChoices("backend", "LOG_BACKEND"),
    )

    # ---- MCP transport -----------------------------------------------------
    mcp_transport: str = Field(
        default="stdio",
        description="MCP transport: 'stdio', 'sse' or 'streamable-http'",
        validation_alias=AliasChoices("mcp_transport", "MCP_TRANSPORT"),
    )
    mcp_host: str = Field(
        default="127.0.0.1",
        description="Listen address for HTTP transports",
        validation_alias=AliasChoices("mcp_host", "MCP_HOST"),
    )
    mcp_port: int = Field(
        default=8000,
        description="Listen port for HTTP transports",
        validation_alias=AliasChoices("mcp_port", "MCP_PORT"),
    )
    log_level: str = Field(
        default="INFO",
        description="Server log level (DEBUG / INFO / WARNING / ERROR)",
        validation_alias=AliasChoices("log_level", "LOG_LEVEL"),
    )

    # ---- Loki backend ------------------------------------------------------
    addr: str = Field(default="http://localhost:3100")
    tenants: str = Field(default="fake")
    username: Optional[str] = Field(default=None)
    password: Optional[SecretStr] = Field(default=None)
    bearer_token: Optional[SecretStr] = Field(default=None)
    bearer_token_file: Optional[str] = Field(default=None)

    ca_file: Optional[str] = Field(default=None)
    cert_file: Optional[str] = Field(default=None)
    key_file: Optional[str] = Field(default=None)
    tls_skip_verify: bool = Field(default=False)

    connect_timeout: float = Field(default=10.0)
    read_timeout: float = Field(default=15.0)
    write_timeout: float = Field(default=10.0)
    pool_timeout: float = Field(default=10.0)

    # ---- Health cache (multi-cluster) --------------------------------------
    health_check_interval: float = Field(
        default=300.0,
        description="Background health refresh interval in seconds",
        validation_alias=AliasChoices(
            "health_check_interval", "HEALTH_CHECK_INTERVAL"
        ),
    )
    health_check_timeout: float = Field(
        default=5.0,
        description="Per-cluster health probe timeout in seconds",
        validation_alias=AliasChoices(
            "health_check_timeout", "HEALTH_CHECK_TIMEOUT"
        ),
    )

    # ---- Per-process client tenant filter ---------------------------------
    # Stdio fallback for the per-request HTTP header X-Allowed-Tenants.
    # Comma-separated.  When set, the server can only see this subset of
    # the configured tenants (must be a subset of LOKI_TENANTS).
    client_tenants: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("client_tenants", "LOKI_CLIENT_TENANTS"),
    )

    # ---- Generic query / time settings ------------------------------------
    default_limit: int = Field(
        default=100,
        validation_alias=AliasChoices(
            "default_limit", "LOG_DEFAULT_LIMIT", "LOKI_DEFAULT_LIMIT"
        ),
    )
    max_limit: int = Field(
        default=5000,
        validation_alias=AliasChoices(
            "max_limit", "LOG_MAX_LIMIT", "LOKI_MAX_LIMIT"
        ),
    )
    default_time_range_minutes: int = Field(
        default=30,
        validation_alias=AliasChoices(
            "default_time_range_minutes",
            "LOG_DEFAULT_TIME_RANGE_MINUTES",
            "LOKI_DEFAULT_TIME_RANGE_MINUTES",
        ),
    )
    timezone: str = Field(
        default="Asia/Shanghai",
        validation_alias=AliasChoices(
            "timezone", "LOG_TIMEZONE", "LOKI_TIMEZONE"
        ),
    )

    # ---- Source priority --------------------------------------------------
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Register YAML as a low-priority source (below env / dotenv)."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlConfigSource(settings_cls),
            file_secret_settings,
        )

    # ---- Validators --------------------------------------------------------
    @field_validator("addr")
    @classmethod
    def _validate_addr(cls, v: str) -> str:
        if not v:
            raise ValueError("Loki server address cannot be empty")
        parts = [p.strip() for p in v.split("|") if p.strip()]
        if not parts:
            raise ValueError("Loki server address cannot be empty")
        cleaned: List[str] = []
        for part in parts:
            if not part.startswith(("http://", "https://")):
                raise ValueError(
                    f"Loki server address must start with http:// or https:// "
                    f"(got {part!r})"
                )
            cleaned.append(part.rstrip("/"))
        return "|".join(cleaned)

    @field_validator("default_limit", "max_limit", "default_time_range_minutes")
    @classmethod
    def _validate_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Value must be a positive integer")
        return v

    @field_validator("connect_timeout", "read_timeout", "write_timeout", "pool_timeout")
    @classmethod
    def _validate_positive_float(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Timeout must be positive")
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        get_timezone(v)
        return v

    @field_validator("mcp_port")
    @classmethod
    def _validate_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("MCP port must be between 1 and 65535")
        return v

    @field_validator("mcp_host")
    @classmethod
    def _validate_host(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("MCP host cannot be empty")
        return v

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("loki",):
            raise ValueError(f"Unsupported backend: {v}")
        return v

    @field_validator("mcp_transport")
    @classmethod
    def _validate_transport(cls, v: str) -> str:
        v = (v or "stdio").strip().lower()
        if v not in ("stdio", "sse", "streamable-http"):
            raise ValueError(
                f"Unsupported MCP transport: {v!r} "
                "(use 'stdio', 'sse' or 'streamable-http')"
            )
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v = (v or "INFO").strip().upper()
        if v not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"Invalid log level: {v!r}")
        return v

    @model_validator(mode="after")
    def _post_init(self) -> "LogConfig":
        if self.bearer_token_file and not self.bearer_token:
            try:
                token = (
                    Path(self.bearer_token_file).read_text(encoding="utf-8").strip()
                )
                if token:
                    object.__setattr__(self, "bearer_token", SecretStr(token))
                    logger.info(
                        "Bearer token loaded from file", path=self.bearer_token_file
                    )
            except FileNotFoundError:
                logger.warning(
                    "Bearer token file not found", path=self.bearer_token_file
                )
            except Exception as e:
                logger.warning(
                    "Failed to load bearer token from file",
                    path=self.bearer_token_file,
                    error=str(e),
                )

        if self.default_limit > self.max_limit:
            raise ValueError(
                f"default_limit ({self.default_limit}) cannot exceed "
                f"max_limit ({self.max_limit})"
            )

        if bool(self.cert_file) ^ bool(self.key_file):
            raise ValueError(
                "cert_file and key_file must be set together (or both unset)"
            )

        client_subset = self.get_client_tenant_list()
        if client_subset is not None:
            server_set = set(self.get_tenant_list())
            invalid = [t for t in client_subset if t not in server_set]
            if invalid:
                raise ValueError(
                    f"client_tenants {invalid!r} are not a subset of "
                    f"configured tenants {sorted(server_set)!r}"
                )
        return self

    # ---- Helpers -----------------------------------------------------------
    def get_tenant_list(self) -> List[str]:
        """Parse the pipe-separated tenant list."""
        if not self.tenants:
            return ["fake"]
        items = [t.strip() for t in self.tenants.split("|") if t.strip()]
        return items or ["fake"]

    def get_client_tenant_list(self) -> Optional[List[str]]:
        """Parse the comma-separated client_tenants override.

        Returns ``None`` when not set (meaning "no client-side filter").
        Uses the same parser as the HTTP header path for consistency.
        """
        return parse_tenant_list(self.client_tenants)

    def get_loki_addrs(self) -> List[str]:
        """Return the list of configured Loki addresses (1 or more)."""
        if not self.addr:
            return []
        return [a.strip() for a in self.addr.split("|") if a.strip()]

    def get_password(self) -> Optional[str]:
        """Return the plaintext password (or ``None``)."""
        return self.password.get_secret_value() if self.password else None

    def get_bearer_token(self) -> Optional[str]:
        """Return the plaintext bearer token (or ``None``)."""
        return self.bearer_token.get_secret_value() if self.bearer_token else None

    def get_safe_config(self) -> Dict[str, Any]:
        """Return a config dict with secrets redacted (for logging/debug)."""
        d = self.model_dump()
        for k in ("password", "bearer_token"):
            if d.get(k):
                d[k] = "[REDACTED]"
        return d
