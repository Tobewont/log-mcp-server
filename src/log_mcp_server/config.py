"""log-mcp-server 配置加载。

配置来源优先级（从高到低）：

1. 构造参数（测试 / 程序化配置时使用）
2. 环境变量（``LOKI_*`` / ``LOG_*`` / ``MCP_*``）
3. 工作目录下的 ``.env`` 文件
4. YAML 配置文件（``LOG_CONFIG_PATH`` / ``LOKI_CONFIG_PATH`` 环境变量
   指定，或 ``./loki-config.yaml`` / ``./.loki-config.yaml`` /
   ``~/.loki-config.yaml``）
5. 内置默认值
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
# YAML 配置加载（优先级最低的来源）
# ---------------------------------------------------------------------------
def _load_yaml_config() -> Dict[str, Any]:
    """按搜索路径顺序读取首个存在的 YAML 配置文件。

    YAML 中字段名支持三种写法（均大小写不敏感）：

    - 无前缀：     ``addr: ...``
    - 带前缀：     ``loki_addr: ...`` 或 ``log_default_limit: ...``

    若同一份文件里同时出现无前缀和带前缀两种写法，**无前缀** 写法获胜，
    保证结果是确定的（与字典遍历顺序无关）。

    显式设置了 ``LOG_CONFIG_PATH`` / ``LOKI_CONFIG_PATH`` 时只尝试该
    路径，不再回退到默认搜索路径。
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
    """Pydantic-settings 数据源，从 YAML 配置文件中读取字段值。"""

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
    """log-mcp-server 顶层配置。

    Loki 后端相关字段保留历史的 ``LOKI_*`` 环境变量前缀以保持向后兼容；
    跨后端 / 通用字段通过 :class:`AliasChoices` 用 ``LOG_*`` 前缀；
    MCP 传输相关字段使用 ``MCP_*`` 前缀。
    """

    model_config = SettingsConfigDict(
        env_prefix="LOKI_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 后端选择 ----------------------------------------------------------
    backend: str = Field(
        default="loki",
        description="启用的日志后端（目前仅支持 'loki'）",
        validation_alias=AliasChoices("backend", "LOG_BACKEND"),
    )

    # ---- MCP 传输 ----------------------------------------------------------
    mcp_transport: str = Field(
        default="stdio",
        description="MCP 传输方式：'stdio' / 'sse' / 'streamable-http'",
        validation_alias=AliasChoices("mcp_transport", "MCP_TRANSPORT"),
    )
    mcp_host: str = Field(
        default="127.0.0.1",
        description="HTTP 传输的监听地址",
        validation_alias=AliasChoices("mcp_host", "MCP_HOST"),
    )
    mcp_port: int = Field(
        default=8000,
        description="HTTP 传输的监听端口",
        validation_alias=AliasChoices("mcp_port", "MCP_PORT"),
    )
    # streamable-http 传输挂载的 URL 路径前缀。SSE / stdio 不受此
    # 配置影响（SSE 仍使用 FastMCP 默认的 ``/sse``）。抽成 env 是为了
    # 和其它 MCP 服务统一规划反代 / Ingress 路径。必须以 ``/`` 开头。
    mcp_path: str = Field(
        default="/mcp",
        description="streamable-http 传输挂载的 URL 路径前缀",
        validation_alias=AliasChoices("mcp_path", "MCP_PATH"),
    )
    log_level: str = Field(
        default="INFO",
        description="服务自身日志级别（DEBUG / INFO / WARNING / ERROR）",
        validation_alias=AliasChoices("log_level", "LOG_LEVEL"),
    )

    # ---- Loki 后端 ---------------------------------------------------------
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

    # ---- 健康检查缓存（多集群时使用）-------------------------------------
    health_check_interval: float = Field(
        default=300.0,
        description="后台健康刷新间隔（秒）",
        validation_alias=AliasChoices(
            "health_check_interval", "HEALTH_CHECK_INTERVAL"
        ),
    )
    health_check_timeout: float = Field(
        default=5.0,
        description="单个集群健康探测超时（秒）",
        validation_alias=AliasChoices(
            "health_check_timeout", "HEALTH_CHECK_TIMEOUT"
        ),
    )

    # ---- 进程级客户端租户过滤 --------------------------------------------
    # stdio 模式下作为 HTTP 请求头 X-Allowed-Tenants 的回退方案，
    # 逗号分隔。设置后服务只能看到 LOKI_TENANTS 的这个子集
    # （必须是 LOKI_TENANTS 的子集）。
    client_tenants: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("client_tenants", "LOKI_CLIENT_TENANTS"),
    )

    # ---- 通用查询 / 时间相关参数 -----------------------------------------
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

    # ---- 日志下载（download_logs 工具）-----------------------------------
    # 服务端写入下载文件的目录。
    # * stdio 模式：这就是用户本机文件系统（server 与 client 同进程），
    #   工具返回的绝对路径可直接打开。
    # * streamable-http 模式：这是服务端的本地文件系统（例如 pod 的
    #   emptyDir）；文件再通过 HTTP 路由 /<MCP前缀>/download/<token>
    #   对外提供，客户端可直接下载。
    download_dir: Path = Field(
        default=Path("./logs/downloads"),
        validation_alias=AliasChoices("download_dir", "LOG_DOWNLOAD_DIR"),
    )
    # 生成的文件（及对应下载令牌）多久后被清理。仅 HTTP 路由有意义——
    # stdio 用户本来就直接打开本地文件，TTL 长一点没影响。默认 1 小时。
    download_ttl_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices(
            "download_ttl_seconds", "LOG_DOWNLOAD_TTL_SECONDS"
        ),
    )
    # streamable-http 模式下用于渲染下载链接的可选 base URL
    # （例如 "https://logs-mcp.example.com"）。不设时服务端会按入站
    # 请求的 scheme / Host 自动推断。在反向代理改写 Host 的场景下
    # 显式设置。
    download_base_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "download_base_url", "LOG_DOWNLOAD_BASE_URL"
        ),
    )

    # ---- 数据源优先级 -----------------------------------------------------
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """把 YAML 注册为低优先级数据源（位于 env / dotenv 之后）。"""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlConfigSource(settings_cls),
            file_secret_settings,
        )

    # ---- 校验器 ------------------------------------------------------------
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

    @field_validator(
        "default_limit",
        "max_limit",
        "default_time_range_minutes",
        "download_ttl_seconds",
    )
    @classmethod
    def _validate_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Value must be a positive integer")
        return v

    @field_validator("download_base_url")
    @classmethod
    def _validate_download_base_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                "download_base_url must start with http:// or https://"
            )
        return v.rstrip("/")

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

    @field_validator("mcp_path")
    @classmethod
    def _validate_mcp_path(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("MCP path cannot be empty")
        if not v.startswith("/"):
            raise ValueError("MCP path must start with '/'")
        # 折叠多余的斜杠，去掉末尾斜杠，便于后续拼 ``<path>/download``。
        segments = [s for s in v.split("/") if s]
        return "/" + "/".join(segments)

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

    # ---- 辅助方法 ----------------------------------------------------------
    def get_tenant_list(self) -> List[str]:
        """解析竖线 ``|`` 分隔的租户列表。"""
        if not self.tenants:
            return ["fake"]
        items = [t.strip() for t in self.tenants.split("|") if t.strip()]
        return items or ["fake"]

    def get_client_tenant_list(self) -> Optional[List[str]]:
        """解析逗号分隔的 client_tenants 覆盖值。

        未设置时返回 ``None``（表示"无客户端侧过滤"）。
        与 HTTP 请求头路径共用同一个解析器，保证行为一致。
        """
        return parse_tenant_list(self.client_tenants)

    def get_loki_addrs(self) -> List[str]:
        """返回已配置的所有 Loki 地址（一个或多个）。"""
        if not self.addr:
            return []
        return [a.strip() for a in self.addr.split("|") if a.strip()]

    def get_password(self) -> Optional[str]:
        """返回明文密码（未配置时为 ``None``）。"""
        return self.password.get_secret_value() if self.password else None

    def get_bearer_token(self) -> Optional[str]:
        """返回明文 bearer token（未配置时为 ``None``）。"""
        return self.bearer_token.get_secret_value() if self.bearer_token else None

    def get_safe_config(self) -> Dict[str, Any]:
        """返回脱敏后的配置字典（密钥字段被替换为 [REDACTED]，便于日志输出）。"""
        d = self.model_dump()
        for k in ("password", "bearer_token"):
            if d.get(k):
                d[k] = "[REDACTED]"
        return d
