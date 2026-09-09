"""与后端无关的 FastMCP 工具集。

这些工具把请求委托给当前启用的 ``LogBackend``。它们是 MCP 对外暴露
的唯一接口，因此尽量保持精简和一致，对 AI 客户端使用更友好。
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import datetime
from datetime import timezone as _tz
from fnmatch import fnmatchcase
from typing import Dict, List, NamedTuple, Optional

import structlog
from mcp.server.fastmcp import Context, FastMCP

from ..auth_context import parse_tenant_list
from ..backends.base import LogBackend, LogEntry, TenantQueryResult
from ..config import LogConfig
from ..downloads import (
    SUPPORTED_FORMATS,
    DownloadRegistry,
    write_download,
)
from ..downloads.writer import build_filename
from ..utils.errors import LogMCPError, ValidationError
from ..utils.time_utils import (
    format_in_tz,
    format_short,
    get_timezone,
    resolve_time_range,
)

logger = structlog.get_logger(__name__)


_PER_TENANT_TIMEOUT_SECONDS = 60.0

_backend: Optional[LogBackend] = None
_config: Optional[LogConfig] = None
_download_registry: Optional[DownloadRegistry] = None
_download_url_path: str = "/mcp/download"


def initialize_tools(
    backend: LogBackend,
    config: LogConfig,
    download_registry: Optional[DownloadRegistry] = None,
    download_url_path: str = "/mcp/download",
) -> None:
    """注入当前启用的后端 / 配置 / 注册表（启动时调用一次）。

    Args:
        backend: 当前启用的日志后端。
        config: 生效的 :class:`LogConfig`。
        download_registry: 仅对 HTTP 传输（streamable-http / sse）
            有意义——此时注册表持有"令牌 → 文件"映射，供下载路由
            消费。stdio 模式下应当传 ``None``（工具会直接返回
            文件路径）。
        download_url_path: 下载路由挂载的 URL 路径前缀，**不**包含
            末尾的 token 段。必须与 ``main.py`` 中注册的路由保持一致。
            默认为 ``/mcp/download``，与 MCP 主路径共享前缀，便于
            任何已经覆盖 ``/mcp`` 的反向代理规则自动覆盖下载链接。
    """
    global _backend, _config, _download_registry, _download_url_path
    _backend = backend
    _config = config
    _download_registry = download_registry
    _download_url_path = download_url_path or "/mcp/download"
    logger.info(
        "Tools initialised",
        backend=backend.name,
        tenants=backend.tenants,
        download_registry_enabled=download_registry is not None,
        download_url_path=_download_url_path,
    )


def _require_state() -> tuple[LogBackend, LogConfig]:
    if _backend is None or _config is None:
        raise RuntimeError("Tools not initialised; call initialize_tools() first")
    return _backend, _config


def _get_download_registry() -> Optional[DownloadRegistry]:
    return _download_registry


# ---------------------------------------------------------------------------
# 多租户扇出相关辅助函数
# ---------------------------------------------------------------------------
def _client_filter_for(
    ctx: Optional[Context], config: LogConfig
) -> tuple[Optional[List[str]], str]:
    """读取客户端为本次调用声明的租户子集。

    返回 ``(subset, source)``：

    * 在 HTTP 传输（streamable-http / SSE）下，streamable_http server
      会通过 ``ServerMessageMetadata.request_context`` 把底层 Starlette
      ``Request`` 透传给每次工具调用，我们直接从中读取
      ``X-Allowed-Tenants`` 请求头。这条路径能穿过 ASGI middleware +
      contextvar 跨不过去的任务边界。
    * stdio 模式下没有 Starlette ``Request``，回退到
      ``LOKI_CLIENT_TENANTS`` 环境变量（在 :class:`LogConfig` 内解析）。

    返回 ``None`` 表示"客户端未声明范围"，此时日志查询/下载工具会
    直接拒绝执行。
    """
    request = None
    if ctx is not None:
        try:
            request = ctx.request_context.request
        except Exception:
            request = None

    if request is not None:
        header: Optional[str] = None
        try:
            # RFC 7230 §3.2.2：同名 header 出现多次时，等价于把它们的
            # 值用逗号拼成一个 header。Starlette 的 ``.get()`` 只会
            # 返回第一次出现的值，所以这里用 ``getlist`` 后自己拼接
            # 以保持语义正确。
            values = request.headers.getlist("x-allowed-tenants")
            if values:
                header = ",".join(values)
        except Exception:
            header = None
        return parse_tenant_list(header), "header"

    return config.get_client_tenant_list(), "env"


def _effective_tenants(
    backend: LogBackend, config: LogConfig, ctx: Optional[Context]
) -> Optional[List[str]]:
    """返回 *本次请求 / 本进程* 实际可见的租户列表。

    客户端声明的子集（HTTP 模式下来自请求头，stdio 来自
    ``LOKI_CLIENT_TENANTS`` 环境变量）会与 ``backend.tenants`` 取交集，
    从而确保畸形或恶意的客户端配置无法把可见范围扩大到服务端允许
    范围之外。

    若客户端 **未声明任何** 范围，返回 ``None``——此时日志查询/下载
    工具必须拒绝执行，迫使用户先声明要查的租户。``health_check`` 不
    走这条路径（它是诊断工具，不是日志查询）。
    """
    client, _source = _client_filter_for(ctx, config)
    if client is None:
        return None

    server_set = set(backend.tenants)
    return [t for t in client if t in server_set]


def _client_filter_required_error(backend: LogBackend) -> RuntimeError:
    return RuntimeError(
        "No tenant scope is configured for this MCP client. Log-query "
        "tools require an explicit tenant list before they can run. "
        "HTTP transports (streamable-http / sse): send the "
        "'X-Allowed-Tenants: <tenant-a>,<tenant-b>' request header. "
        "Stdio transport: set 'LOKI_CLIENT_TENANTS=<tenant-a>,<tenant-b>' "
        "in the env block of your MCP client config (e.g. mcp.json). "
        f"Tenants configured on this server: {', '.join(backend.tenants)}."
    )


def _resolve_tenants(
    backend: LogBackend,
    config: LogConfig,
    tenant: Optional[str],
    ctx: Optional[Context],
) -> List[str]:
    """返回本次实际要查询的租户列表。

    若客户端未声明租户子集，抛 ``RuntimeError``——参见
    :func:`_effective_tenants`。否则在显式给出 ``tenant`` 时校验它必须
    在生效集合内，并返回单元素列表；未给 ``tenant`` 时返回全部生效
    租户。
    """
    effective = _effective_tenants(backend, config, ctx)
    if effective is None:
        raise _client_filter_required_error(backend)
    if not effective:
        raise RuntimeError(
            "No tenants are accessible. The client filter "
            "(X-Allowed-Tenants header or LOKI_CLIENT_TENANTS env) "
            "intersected with the server tenants produced an empty "
            f"set. Server tenants: {', '.join(backend.tenants)}."
        )
    if tenant is None:
        return effective
    tenant = tenant.strip()
    if not tenant:
        raise RuntimeError("tenant cannot be empty")
    if tenant not in effective:
        if tenant in backend.tenants:
            raise RuntimeError(
                f"Forbidden tenant {tenant!r}: not in the client-allowed "
                f"set {effective}. Server tenants: "
                f"{', '.join(backend.tenants)}."
            )
        raise RuntimeError(
            f"Unknown tenant {tenant!r}. " f"Allowed tenants: {', '.join(effective)}"
        )
    return [tenant]


async def _fan_out(
    tenants: List[str],
    coro_factory,
) -> List[TenantQueryResult]:
    """对每个租户并发执行一次协程，并对单租户设置超时。

    ``coro_factory(tenant, cluster_errors, cluster_warnings)`` 在每个
    租户上都会被调一次，且每次都会得到一份新的字典，这样多集群扇出
    后端可以在不同租户之间互不干扰地记录"部分集群失败"和"成功但
    需要用户注意"的信息。
    """

    async def _wrap(tenant: str) -> TenantQueryResult:
        cluster_errors: Dict[str, str] = {}
        cluster_warnings: Dict[str, str] = {}
        try:
            data = await asyncio.wait_for(
                coro_factory(tenant, cluster_errors, cluster_warnings),
                timeout=_PER_TENANT_TIMEOUT_SECONDS,
            )
            return TenantQueryResult(
                tenant=tenant,
                data=data,
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )
        except asyncio.TimeoutError:
            return TenantQueryResult(
                tenant=tenant,
                error=f"timeout after {_PER_TENANT_TIMEOUT_SECONDS:.0f}s",
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )
        except LogMCPError as e:
            return TenantQueryResult(
                tenant=tenant,
                error=f"{type(e).__name__}: {e}",
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )
        except Exception as e:  # noqa: BLE001
            return TenantQueryResult(
                tenant=tenant,
                error=f"{type(e).__name__}: {e}",
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )

    return await asyncio.gather(*[_wrap(t) for t in tenants])


# ---------------------------------------------------------------------------
# 输出格式化辅助函数
# ---------------------------------------------------------------------------
def _format_failures(results: List[TenantQueryResult]) -> str:
    failure_lines: List[str] = []
    for r in results:
        if not r.ok:
            failure_lines.append(f"- `{r.tenant}` (tenant): {r.error}")
        for cluster_id, err in sorted((r.cluster_errors or {}).items()):
            failure_lines.append(
                f"- `{cluster_id}` (cluster, tenant=`{r.tenant}`): {err}"
            )
    if not failure_lines:
        return ""
    return "\n".join(["", "**Errors:**", *failure_lines]) + "\n"


def _format_warnings(results: List[TenantQueryResult]) -> str:
    warning_lines: List[str] = []
    for r in results:
        for cluster_id, warning in sorted((r.cluster_warnings or {}).items()):
            warning_lines.append(
                f"- `{cluster_id}` (cluster, tenant=`{r.tenant}`): {warning}"
            )
    if not warning_lines:
        return ""
    return "\n".join(["", "**Warnings:**", *warning_lines]) + "\n"


def _format_log_entries(entries: List[LogEntry], tz: str) -> str:
    out: List[str] = []
    for i, e in enumerate(entries, 1):
        labels_str = ", ".join(f"{k}={v}" for k, v in sorted(e.labels.items()))
        meta_parts = [f"Tenant: {e.tenant or '-'}"]
        if e.cluster:
            meta_parts.append(f"Cluster: {e.cluster}")
        out.append(
            f"## Entry {i} ({', '.join(meta_parts)})\n"
            f"**Time:** {format_in_tz(e.timestamp, tz)}\n"
            f"**Labels:** {{{labels_str}}}\n"
            f"**Log:** {e.line}\n"
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 返回体瘦身：公共标签抽取 / 单行截断 / 重复折叠 / compact & normal 渲染
# ---------------------------------------------------------------------------
def _label_value_repr(value: object) -> str:
    """把标签值归一化成可比较、可 hash 的字符串。

    ``| json`` 之后的标签值可能是嵌套 dict（不可 hash），所以统一用
    ``json.dumps(..., sort_keys=True, ensure_ascii=False)`` 序列化。
    """
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _split_common_labels(
    entries: List[LogEntry],
) -> tuple[Dict[str, str], set[str]]:
    """把标签拆成 "公共标签" 和 "差异标签 key 集合"。

    在整个结果集内某个 key 只有单一取值时视为公共标签，其余为差异
    标签。返回 ``(common_labels, differing_keys)``，其中 ``common_labels``
    的值用归一化后的字符串表示。
    """
    if not entries:
        return {}, set()

    values_by_key: Dict[str, set[str]] = {}
    seen_repr: Dict[str, str] = {}
    all_keys: set[str] = set()
    for e in entries:
        labels = e.labels or {}
        for k, v in labels.items():
            all_keys.add(k)
            rep = _label_value_repr(v)
            values_by_key.setdefault(k, set()).add(rep)
            seen_repr.setdefault(k, rep)

    common: Dict[str, str] = {}
    differing: set[str] = set()
    for k in all_keys:
        vals = values_by_key.get(k, set())
        # 某个 key 只在部分 entry 出现时也算差异标签（取值不统一）。
        appears_in_all = all(k in (e.labels or {}) for e in entries)
        if len(vals) == 1 and appears_in_all:
            common[k] = seen_repr[k]
        else:
            differing.add(k)
    return common, differing


def _differing_labels_str(entry: LogEntry, differing_keys: set[str]) -> str:
    """渲染单条 entry 的差异标签（仅保留 differing_keys 中的 key）。"""
    parts = [
        f"{k}={_label_value_repr(v)}"
        for k, v in sorted((entry.labels or {}).items())
        if k in differing_keys
    ]
    return ", ".join(parts)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(line: str) -> str:
    """剥离行内的 ANSI 转义序列（颜色 / 光标控制）。

    终端颜色对 AI 阅读没有价值，而级别文字（如 ``| INFO |``）本来就在
    正文里，因此这是零信息损失的处理。
    """
    return _ANSI_RE.sub("", line)


# loguru 的级别序（``SUCCESS`` 介于 INFO 与 WARNING 之间），用于 min_level
# 过滤时的比较；``WARN`` / ``FATAL`` 作为常见别名一并接受。
_LEVEL_ORDER: Dict[str, int] = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}
_LEVEL_ALIASES: Dict[str, str] = {
    "WARN": "WARNING",
    "FATAL": "CRITICAL",
}
# 永不参与折叠的"异常级别"——排查问题时这些行必须逐条完整保留。
_NEVER_FOLD_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})

# loguru 默认格式为 ``<time> | LEVEL    | module:line - message``，
# 级别夹在竖线之间（两侧可能有空格）。这里只在行首附近匹配，避免把
# 正文里出现的 "| ERROR |" 误判成本行级别。
_LEVEL_RE = re.compile(
    r"\|\s*(TRACE|DEBUG|INFO|SUCCESS|WARNING|WARN|ERROR|CRITICAL|FATAL)\s*\|",
    re.IGNORECASE,
)


def _normalise_level(name: str) -> Optional[str]:
    """把级别名归一化成 :data:`_LEVEL_ORDER` 中的键（无法识别时 None）。"""
    key = (name or "").strip().upper()
    key = _LEVEL_ALIASES.get(key, key)
    return key if key in _LEVEL_ORDER else None


def _parse_line_level(line: str) -> Optional[str]:
    """从（已剥 ANSI 的）日志正文里解析级别，解析不出时返回 ``None``。"""
    m = _LEVEL_RE.search(line)
    if m is None:
        return None
    return _normalise_level(m.group(1))


# loguru 默认格式里 logger 名紧跟在级别之后：
# ``<time> | LEVEL    | app.module:480 - message``。行号（``:480``）与
# ``-`` 分隔符都是可选的，某些自定义格式只有 ``app.module - message``。
_LOGGER_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*(?::\d+)?\s*(?:-|$)")


def _parse_line_logger(line: str) -> Optional[str]:
    """从（已剥 ANSI 的）日志正文里解析 logger 名，解析不出时返回 ``None``。

    只识别 loguru 风格的 ``| LEVEL | logger.name:行号 - 正文``：先定位
    级别所在的竖线段，再取其后、``:`` / ``-`` 之前的部分。解析不出时
    返回 ``None``——调用方必须把这类行 **保留**（不确定就不丢）。
    """
    m = _LEVEL_RE.search(line)
    if m is None:
        return None
    rest = line[m.end() :].lstrip()
    lm = _LOGGER_RE.match(rest)
    if lm is None:
        return None
    return lm.group(1)


def _matches_any_pattern(name: str, patterns: List[str]) -> bool:
    """logger 名是否命中任一黑名单模式。

    用 :func:`fnmatch.fnmatchcase` 做通配匹配（大小写敏感，跨平台行为
    一致）：``httpcore.*`` 前缀匹配、``uvicorn.*`` 同理、不含通配符的
    模式则要求精确相等。
    """
    for pattern in patterns:
        if fnmatchcase(name, pattern):
            return True
    return False


def _filter_by_exclude_loggers(
    entries: List[LogEntry], patterns: List[str]
) -> tuple[List[LogEntry], Dict[str, int]]:
    """按 logger 黑名单过滤 entries，返回 ``(保留的 entries, 被隐藏的计数)``。

    两条硬保护（排查问题时不能被噪音规则吞掉）：

    * WARNING / ERROR / CRITICAL 级别的行即使 logger 命中黑名单也保留；
    * 解析不出 logger 名的行一律保留。
    """
    if not patterns:
        return entries, {}
    kept: List[LogEntry] = []
    hidden: Dict[str, int] = {}
    for e in entries:
        stripped = _strip_ansi(e.line)
        if _parse_line_level(stripped) in _NEVER_FOLD_LEVELS:
            kept.append(e)
            continue
        name = _parse_line_logger(stripped)
        if name is None or not _matches_any_pattern(name, patterns):
            kept.append(e)
            continue
        hidden[name] = hidden.get(name, 0) + 1
    return kept, hidden


def _filter_by_min_level(
    entries: List[LogEntry], min_level: str
) -> tuple[List[LogEntry], Dict[str, int]]:
    """按最低级别过滤 entries，返回 ``(保留的 entries, 被隐藏的级别计数)``。

    解析不出级别的行 **一律保留**——排查问题时"不确定就不丢"是唯一
    安全的默认。
    """
    threshold = _LEVEL_ORDER[min_level]
    kept: List[LogEntry] = []
    hidden: Dict[str, int] = {}
    for e in entries:
        level = _parse_line_level(_strip_ansi(e.line))
        if level is not None and _LEVEL_ORDER[level] < threshold:
            hidden[level] = hidden.get(level, 0) + 1
            continue
        kept.append(e)
    return kept, hidden


# 正文开头自带的时间戳。日期部分可选，因为部分格式只打
# ``HH:MM:SS.mmm``；小数秒同时接受 ``.`` 与 ``,`` 分隔。
_LEADING_TS_RE = re.compile(
    r"^(?:(\d{4})-(\d{2})-(\d{2})[ T])?" r"(\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?\s*"
)


def _dedup_leading_timestamp(
    line: str, timestamp: datetime, tz_name: str
) -> tuple[str, bool]:
    """去掉正文开头与行首时间戳重复的那个时间戳。

    仅当正文开头是 ``YYYY-MM-DD HH:MM:SS(.mmm)`` 或 ``HH:MM:SS(.mmm)``、
    **且** 与该 entry 的 ``timestamp`` 相差不到 1 秒（允许毫秒差异）时
    才去掉；正文没写日期时用 entry 的日期补齐再比较。任何"不吻合"的
    情况都保持原样——宁可冗余也不能删错。

    返回 ``(处理后的正文, 是否发生了去重)``。
    """
    m = _LEADING_TS_RE.match(line)
    if m is None:
        return line, False
    local = timestamp.astimezone(get_timezone(tz_name))
    year, month, day, hour, minute, second, frac = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    try:
        line_dt = local.replace(
            year=int(year) if year else local.year,
            month=int(month) if month else local.month,
            day=int(day) if day else local.day,
            hour=int(hour),
            minute=int(minute),
            second=int(second),
            microsecond=micro,
        )
    except ValueError:
        return line, False
    if abs((line_dt - local).total_seconds()) >= 1.0:
        return line, False
    return line[m.end() :], True


def _sample_per_template(
    entries: List[LogEntry], per_template: int
) -> tuple[List[LogEntry], int, int]:
    """按模板分层采样：每种模板最多保留 ``per_template`` 条。

    ``limit`` 很容易被单一噪音模板占满，导致稀有模板被挤出结果；分层
    采样保证每种模板都有代表。实现上保持原始时序（只丢弃超额的行，
    不重排）。

    硬保护：WARNING / ERROR / CRITICAL 级别的行 **完全不参与采样**，
    全部保留，也不计入模板数。

    返回 ``(保留的 entries, 被隐藏条数, 参与采样的模板数)``。
    """
    seen: Dict[str, int] = {}
    kept: List[LogEntry] = []
    hidden = 0
    for e in entries:
        stripped = _strip_ansi(e.line)
        if _parse_line_level(stripped) in _NEVER_FOLD_LEVELS:
            kept.append(e)
            continue
        template = _fold_template(stripped)
        count = seen.get(template, 0) + 1
        seen[template] = count
        if count <= per_template:
            kept.append(e)
        else:
            hidden += 1
    return kept, hidden, len(seen)


def _truncate_line(line: str, max_chars: int) -> str:
    """超过 max_chars 的单行截断，并追加提示引导用户走 download_logs。"""
    if max_chars <= 0 or len(line) <= max_chars:
        return line
    dropped = len(line) - max_chars
    return (
        line[:max_chars] + f"…(+{dropped} chars truncated, full line via download_logs)"
    )


_FOLD_NUM_RE = re.compile(r"0x[0-9a-fA-F]+|[0-9a-fA-F]{16,}|\d+")

# 承载"异常语义"的 HTTP 状态码（4xx / 5xx）。折叠比较时必须保留原值，
# 否则 200 / 404 / 500 会被归一化成同一模板，导致"发生过 500"这个关键
# 事实被静默吞掉——这违背了日志排查的根本目的。
_FOLD_KEEP_STATUS_RE = re.compile(r"(?<!\d)([45]\d{2})(?!\d)")


def _fold_template(line: str) -> str:
    """把行内的数字串、十六进制、长 ID 替换成占位符，用于重复折叠比较。

    例外：HTTP 4xx/5xx 状态码保持原值参与比较，确保 200 / 404 / 500 不会
    被折叠进同一组而丢失异常信息。
    """
    # 先按 4xx/5xx 状态码切段，只对非状态码片段做数字归一化，状态码原样拼回。
    parts: List[str] = []
    pos = 0
    for m in _FOLD_KEEP_STATUS_RE.finditer(line):
        parts.append(_FOLD_NUM_RE.sub("#", line[pos : m.start()]))
        parts.append(m.group(1))
        pos = m.end()
    parts.append(_FOLD_NUM_RE.sub("#", line[pos:]))
    return "".join(parts)


# 折叠差异摘要：一组同模板行里最多展示多少个不同取值。超过则截断并标 …
_FOLD_DIFF_MAX_VALUES = 5
# 只对"看起来像标识符"的差异做摘要（纯数字/hex ID），避免把时间戳、行号
# 这类噪音也摘出来。键名形如 task_id= / askid= / trace_id: 等。
_FOLD_DIFF_KV_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_.\-]{1,40})\s*[=:]\s*([A-Za-z0-9_.\-]{1,64})"
)


def _fold_diff_summary(group: List[str]) -> str:
    """为一组被折叠的同模板行生成差异摘要。

    折叠会把 ``task_id=19049`` / ``task_id=8359`` 这类关键区分字段压成
    一条，导致排查时"另外几个 ID 去哪了"无从得知。这里把组内取值不同
    的键提取出来，形如 ``task_id=19049,19075,8359``，保证被折叠的标识
    信息依然可见。

    只在确实存在差异键时返回内容，否则返回空串（不污染输出）。
    """
    if len(group) < 2:
        return ""
    # 逐行解析 key -> 出现过的值（保持首次出现顺序）
    per_key: Dict[str, List[str]] = {}
    for line in group:
        for key, value in _FOLD_DIFF_KV_RE.findall(line):
            seen = per_key.setdefault(key, [])
            if value not in seen:
                seen.append(value)
    parts: List[str] = []
    for key, values in per_key.items():
        if len(values) < 2:
            continue  # 该键在组内取值一致，不是差异点
        shown = values[:_FOLD_DIFF_MAX_VALUES]
        text = ",".join(shown)
        if len(values) > _FOLD_DIFF_MAX_VALUES:
            text += f",…({len(values)} 种)"
        parts.append(f"{key}={text}")
    if not parts:
        return ""
    return " [" + "; ".join(parts) + "]"


def _fold_repeated_lines(
    lines: List[str], foldable: Optional[List[bool]] = None
) -> List[str]:
    """连续 ≥3 条同模板的行折叠成一条并追加 ``×N``。

    仅比较模板（数字 / hex / 长 ID 归一化后），展示时保留该组第一条
    原始行。少于 3 条的重复保持原样。

    ``foldable`` 为逐行的可折叠标记（``False`` 表示该行必须单独完整
    保留，例如 WARNING / ERROR 级别的行）；省略时视为全部可折叠。
    """
    if not lines:
        return []
    flags = foldable if foldable is not None else [True] * len(lines)
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if not flags[i]:
            out.append(lines[i])
            i += 1
            continue
        template = _fold_template(lines[i])
        j = i + 1
        while j < n and flags[j] and _fold_template(lines[j]) == template:
            j += 1
        count = j - i
        if count >= 3:
            diff = _fold_diff_summary(lines[i:j])
            out.append(f"{lines[i]} ×{count}{diff}")
        else:
            out.extend(lines[i:j])
        i = j
    return out


def _format_clock(dt: datetime, tz_name: str) -> str:
    """只输出 ``HH:MM:SS``，用于 global 折叠的组内时间范围。"""
    return dt.astimezone(get_timezone(tz_name)).strftime("%H:%M:%S")


def _fold_global_lines(
    lines: List[str],
    timestamps: List[datetime],
    foldable: List[bool],
    tz_name: str,
) -> tuple[List[str], int, int, int]:
    """全窗口按模板聚类折叠（不要求相邻）。

    - 只有同模板出现 ≥2 次才聚合；聚合行为"该组第一条原始行 + ``×N``
      + 组内首末时间"。
    - 组的输出位置按该组 **首次出现** 的位置排序，保持大致时序可读。
    - ``foldable`` 为 ``False`` 的行（异常级别）完全排除在聚类之外，
      逐条原样保留在其原本时序位置上。

    返回 ``(输出行, 参与聚合的行数, 聚合后的组数, 被排除的行数)``。
    """
    n = len(lines)
    groups: Dict[str, List[int]] = {}
    for i in range(n):
        if not foldable[i]:
            continue
        groups.setdefault(_fold_template(lines[i]), []).append(i)

    first_index_of: Dict[str, int] = {t: idxs[0] for t, idxs in groups.items()}
    template_at: Dict[int, str] = {i: t for t, i in first_index_of.items()}

    out: List[str] = []
    for i in range(n):
        if not foldable[i]:
            out.append(lines[i])
            continue
        template = template_at.get(i)
        if template is None:
            # 非本组首次出现的行，已被聚合到组首。
            continue
        idxs = groups[template]
        if len(idxs) < 2:
            out.append(lines[i])
            continue
        stamps = [timestamps[k] for k in idxs]
        first = _format_clock(min(stamps), tz_name)
        last = _format_clock(max(stamps), tz_name)
        diff = _fold_diff_summary([lines[k] for k in idxs])
        out.append(f"{lines[i]} （×{len(idxs)}, {first}~{last}）{diff}")

    foldable_count = sum(1 for f in foldable if f)
    return out, foldable_count, len(groups), n - foldable_count


def _origin_tag(entry: LogEntry) -> str:
    """多租户 / 多集群时的短格式来源标记 ``[tenant@cluster]``。"""
    tenant = entry.tenant or "-"
    if entry.cluster:
        return f"[{tenant}@{entry.cluster}]"
    return f"[{tenant}]"


# ---------------------------------------------------------------------------
# A 档「零信息损失」瘦身：公共前缀上提 / 连续空格折叠 / 正文内重复日期省略
# ---------------------------------------------------------------------------
# 三者都默认开启，因此必须绝对不丢排查信息：时分秒、级别、模块名、行号、
# 业务 ID、HTTP 状态码、异常关键字、堆栈缩进全部完好；被提取 / 省略的
# 内容都会在返回体头部标注一次，用户可自行还原。``verbosity="full"``
# 是绝对逃生舱，三者一律不作用于 full。
#
# 执行顺序（在 _render_compact / _render_normal 内）：
#   strip_ansi → (既有 min_level / exclude_loggers / sample 过滤发生在
#   调用方) → 截断(max_line_chars) → 折叠(fold) → A2 空格折叠 →
#   A3 日期省略 → A1 公共前缀上提
# 理由：
#   * 截断必须最早做完，否则 A1/A3 省掉字符后长度判断会失真；
#   * 折叠必须早于 A1/A3，避免 ``×N`` 摘要、``（×N, 时间范围）`` 干扰
#     公共前缀与日期的计算（折叠追加的内容都在行尾，不影响行首）；
#   * A1 必须最后做，因为 A2/A3 会改写行首内容。
# 三者都只作用于"日志正文"部分：多租户场景下的 ``[tenant@cluster]``
# 来源标记、normal 的时间/标签前缀都被排除在计算之外，先拆出去、算完
# 再拼回，确保多租户不出错。

# 公共前缀短于该长度就不上提——收益抵不上"用户要回头拼前缀"的认知成本。
_MIN_COMMON_PREFIX_CHARS = 8

# 只压"行首非空白字符之后"的连续空格：行首缩进（堆栈跟踪、YAML/JSON
# 片段）有结构含义，绝不能动。lookbehind 保证被压的空格前面是非空白
# 字符，因此纯空格行不会被改写。只匹配空格字符本身，tab / 换行不碰。
_INNER_SPACES_RE = re.compile(r"(?<=\S) {2,}")

# 正文开头形如 ``YYYY-MM-DD `` 且后面紧跟 ``HH:MM:SS`` 的日期前缀。
# 只有"后面确实还有时间"才认，避免把纯日期行的唯一时间信息删掉。
_LEADING_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](?=\d{2}:\d{2}:\d{2})")

# 多租户来源标记 ``[tenant@cluster] `` —— 用于把正文从渲染行里拆出来。
_ORIGIN_TAG_RE = re.compile(r"^\[[^\]]*\] ")


class RenderResult(NamedTuple):
    """compact / normal 渲染结果。

    除正文之外还带回需要在返回体 **头部** 标注一次的信息，供
    ``query_logs`` 拼接：被上提的公共前缀、被省略的公共日期、折叠汇总
    提示、以及 normal 是否真的去重过正文时间戳。
    """

    body: str
    fold_note: Optional[str] = None
    common_prefix: Optional[str] = None
    common_date: Optional[str] = None
    deduped_timestamp: bool = False


def _collapse_inner_whitespace(line: str) -> str:
    """A2：把行内连续 ≥2 个**空格**压成 1 个，**保留行首缩进**。

    loguru 的级别对齐填充（``INFO     |``）是这类连续空格的主要来源，
    而日志语义从不依赖对齐空格数，因此这是零信息损失的优化。只处理
    空格字符，tab 与换行一律不碰；行首缩进也不动——堆栈跟踪与
    YAML / JSON 片段靠缩进表达结构。
    """
    return _INNER_SPACES_RE.sub(" ", line)


def _detect_common_body_date(bodies: List[str]) -> Optional[str]:
    """A3：扫描全批正文，返回可省略的唯一日期；跨天则返回 ``None``。

    只有当所有带日期前缀的行日期 **完全相同** 时才返回该日期；一旦出现
    ≥2 个不同日期立刻整批禁用（跨天时日期是判断顺序的关键信息）。没有
    任何行带日期前缀时同样返回 ``None``。
    """
    found: Optional[str] = None
    for body in bodies:
        m = _LEADING_DATE_RE.match(body)
        if m is None:
            continue
        date = m.group(1)
        if found is None:
            found = date
        elif found != date:
            return None  # 跨天：整批不省略
    return found


def _strip_leading_date(body: str, date: str) -> str:
    """去掉正文开头的 ``YYYY-MM-DD `` 日期前缀，保留其后的时分秒。

    compact 行首没有时间，正文里的 ``HH:MM:SS(.mmm)`` 是唯一时间来源，
    因此这里只删日期、绝不触碰时间部分。
    """
    m = _LEADING_DATE_RE.match(body)
    if m is None or m.group(1) != date:
        return body
    return body[m.end() :]


def _longest_common_prefix(values: List[str]) -> str:
    """求一组字符串的最长公共前缀（空列表返回空串）。"""
    if not values:
        return ""
    prefix = values[0]
    for value in values[1:]:
        limit = min(len(prefix), len(value))
        i = 0
        while i < limit and prefix[i] == value[i]:
            i += 1
        prefix = prefix[:i]
        if not prefix:
            break
    return prefix


# A1 只允许在 **空格 / tab** 处截断公共前缀。空格是日志字段的天然
# 分隔符，在它之后切分不会把日期 / 数字 / 单词切成两半。字符级公共
# 前缀则可能落在字段中间（例如同一天内不同日的日期只差末位数字），
# 剩下的半截值既容易被误读，也会破坏 grep 可用性。


def _trim_prefix_to_safe_boundary(prefix: str) -> str:
    """把公共前缀回退到最后一个空格或 tab 处（含该分隔符）。

    找不到分隔符就返回空串——宁可放弃上提，也不产出会被误读的行。
    """
    idx = max(prefix.rfind(" "), prefix.rfind("	"))
    if idx < 0:
        return ""
    return prefix[: idx + 1]


def _hoist_common_prefix(bodies: List[str]) -> tuple[List[str], Optional[str]]:
    """A1：把所有正文的最长公共前缀上提到头部。

    返回 ``(去掉前缀后的正文列表, 被上提的前缀或 None)``。三重边界保护：

    * 只有 ≥2 行才计算（单行谈不上"公共"）；
    * 前缀长度必须 ≥ :data:`_MIN_COMMON_PREFIX_CHARS`，否则收益太小；
    * 前缀不得把任何一行完全吃掉（否则会出现空行），此时放弃上提。
    """
    if len(bodies) < 2:
        return bodies, None
    prefix = _longest_common_prefix(bodies)
    # 回退到语义安全边界，避免把日期 / 数字 / 单词切成两半。
    prefix = _trim_prefix_to_safe_boundary(prefix)
    if len(prefix) < _MIN_COMMON_PREFIX_CHARS:
        return bodies, None
    if any(len(body) == len(prefix) for body in bodies):
        return bodies, None
    return [body[len(prefix) :] for body in bodies], prefix


def _split_origin_tag(line: str, single_origin: bool) -> tuple[str, str]:
    """把渲染行拆成 ``(来源标记前缀, 日志正文)``。

    单一来源时没有标记，整行都是正文。多租户时把 ``[tenant@cluster] ``
    排除在 A1/A2/A3 的计算之外，避免不同来源互相影响。
    """
    if single_origin:
        return "", line
    m = _ORIGIN_TAG_RE.match(line)
    if m is None:
        return "", line
    return m.group(0), line[m.end() :]


def _slim_body_lines(
    lines: List[str],
    *,
    single_origin: bool,
    collapse_whitespace: bool,
    hoist_common_date: bool,
    hoist_common_prefix: bool,
) -> tuple[List[str], Optional[str], Optional[str]]:
    """对已折叠的渲染行依次施加 A2 → A3 → A1。

    返回 ``(处理后的行, 被上提的公共前缀, 被省略的公共日期)``。
    """
    tags: List[str] = []
    bodies: List[str] = []
    for line in lines:
        tag, body = _split_origin_tag(line, single_origin)
        tags.append(tag)
        bodies.append(body)

    # A2：行内连续空格折叠（保留行首缩进）
    if collapse_whitespace:
        bodies = [_collapse_inner_whitespace(b) for b in bodies]

    # A3：正文内重复日期省略（跨天自动禁用；时分秒完整保留）
    common_date: Optional[str] = None
    if hoist_common_date:
        common_date = _detect_common_body_date(bodies)
        if common_date is not None:
            bodies = [_strip_leading_date(b, common_date) for b in bodies]

    # A1：公共前缀上提（必须最后算，前两步会改写行首）
    common_prefix: Optional[str] = None
    if hoist_common_prefix:
        bodies, common_prefix = _hoist_common_prefix(bodies)

    return [tag + body for tag, body in zip(tags, bodies)], common_prefix, common_date


def _render_compact(
    entries: List[LogEntry],
    *,
    single_origin: bool,
    max_line_chars: int,
    fold_repeats: bool,
    strip_ansi: bool = False,
    fold_scope: str = "adjacent",
    timezone_name: str = "UTC",
    collapse_whitespace: bool = False,
    hoist_common_date: bool = False,
    hoist_common_prefix: bool = False,
) -> RenderResult:
    """compact：只输出日志正文，一行一条，无 Entry 头 / Time / Labels。

    返回 :class:`RenderResult`：``fold_note`` 仅在 ``fold_scope="global"``
    且实际发生聚合时才有值；``common_prefix`` / ``common_date`` 分别对应
    A1 公共前缀上提与 A3 正文内日期省略，有值时调用方需在头部标注一次。
    """
    lines: List[str] = []
    foldable: List[bool] = []
    timestamps: List[datetime] = []
    for e in entries:
        raw = _strip_ansi(e.line) if strip_ansi else e.line
        # 折叠保护判定始终基于剥了 ANSI 的正文，避免颜色码干扰级别解析。
        level = _parse_line_level(raw if strip_ansi else _strip_ansi(e.line))
        line = _truncate_line(raw, max_line_chars)
        if single_origin:
            lines.append(line)
        else:
            lines.append(f"{_origin_tag(e)} {line}")
        foldable.append(level not in _NEVER_FOLD_LEVELS)
        timestamps.append(e.timestamp)

    note: Optional[str] = None
    if fold_repeats and fold_scope == "global":
        folded, participating, group_count, excluded = _fold_global_lines(
            lines, timestamps, foldable, timezone_name
        )
        if group_count and participating > group_count:
            excluded_note = f"，{excluded} 条异常行未参与聚合" if excluded else ""
            note = (
                f"\n> ℹ️ fold_scope=global 将 {participating} 条聚合为 "
                f"{group_count} 组{excluded_note}。用 fold_scope=adjacent "
                "查看逐条时序。\n"
            )
        lines = folded
    elif fold_repeats:
        lines = _fold_repeated_lines(lines, foldable)

    # A2 → A3 → A1 都在折叠之后、对最终要输出的行施加。
    lines, common_prefix, common_date = _slim_body_lines(
        lines,
        single_origin=single_origin,
        collapse_whitespace=collapse_whitespace,
        hoist_common_date=hoist_common_date,
        hoist_common_prefix=hoist_common_prefix,
    )
    return RenderResult(
        body="\n".join(lines),
        fold_note=note,
        common_prefix=common_prefix,
        common_date=common_date,
    )


def _render_normal(
    entries: List[LogEntry],
    tz: str,
    *,
    single_origin: bool,
    differing_keys: set[str],
    max_line_chars: int,
    strip_ansi: bool = False,
    dedup_timestamp: bool = False,
    collapse_whitespace: bool = False,
    hoist_common_prefix: bool = False,
) -> RenderResult:
    """normal：短格式时间 + 差异标签 + 正文；公共标签由头部单独输出。

    返回 :class:`RenderResult`：``deduped_timestamp`` 表示是否真的发生过
    正文内时间戳去重（只在删过东西时才提示）；``common_prefix`` 对应 A1
    公共前缀上提。A1/A2 只作用于 **正文**，行首的时间 / 差异标签 /
    来源标记不参与计算。A3（正文内日期省略）不在 normal 生效——normal
    行首已有 ``MM-DD`` 短日期，交由既有 ``dedup_timestamp`` 处理。
    """
    prefixes: List[str] = []
    bodies: List[str] = []
    deduped_any = False
    for e in entries:
        raw = _strip_ansi(e.line) if strip_ansi else e.line
        if dedup_timestamp:
            raw, did = _dedup_leading_timestamp(raw, e.timestamp, tz)
            deduped_any = deduped_any or did
        line = _truncate_line(raw, max_line_chars)
        ts = format_short(e.timestamp, tz)
        diff = _differing_labels_str(e, differing_keys)
        prefix_parts: List[str] = []
        if not single_origin:
            prefix_parts.append(_origin_tag(e))
        prefix_parts.append(ts)
        if diff:
            prefix_parts.append(f"{{{diff}}}")
        prefixes.append(" ".join(prefix_parts))
        bodies.append(line)

    # A2 → A1（normal 不做 A3）。正文与行首前缀分开处理，前缀原样保留。
    if collapse_whitespace:
        bodies = [_collapse_inner_whitespace(b) for b in bodies]
    common_prefix: Optional[str] = None
    if hoist_common_prefix:
        bodies, common_prefix = _hoist_common_prefix(bodies)

    lines = [f"{p}  {b}" for p, b in zip(prefixes, bodies)]
    return RenderResult(
        body="\n".join(lines),
        common_prefix=common_prefix,
        deduped_timestamp=deduped_any,
    )


def _common_labels_str(common: Dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(common.items()))


def _slim_header(rendered: RenderResult) -> str:
    """把 A1 / A3 提取出的内容渲染成头部标注（每项最多一行）。

    风格与既有 ``**Common Labels:**`` 保持一致，让用户能一眼看到"少了
    什么、怎么还原"——这是三项默认开启的前提。
    """
    parts: List[str] = []
    if rendered.common_prefix:
        parts.append(f"**Common Prefix:** `{rendered.common_prefix}`\n")
    if rendered.common_date:
        parts.append(f"**Date:** `{rendered.common_date}`" "（正文内重复日期已省略）\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# count_logs 的分布画像（group_by）
# ---------------------------------------------------------------------------
# 支持的分组维度。每项映射到"可尝试下推到后端的标签名"；``None`` 表示
# 该维度只能在服务端按样本统计（例如模板需要先做数字归一化）。
_GROUP_BY_DIMENSIONS: Dict[str, Optional[str]] = {
    "level": "detected_level",
    "logger": "logger",
    "template": None,
}
# 走抽样估算时最多拉多少条样本。只用于统计，不回正文，所以可以比
# query_logs 的默认 limit 大一些，但也要有上限避免拖慢探量。
_GROUP_BY_SAMPLE_CAP = 1000
# template 维度输出的模板数上限（避免把 Top N 表格撑成几百行）。
_GROUP_BY_TEMPLATE_TOP_N = 20


def _group_entries_by_dimension(
    entries: List[LogEntry], dimension: str
) -> Dict[str, int]:
    """在服务端按 ``dimension`` 统计样本条数。

    解析不出级别 / logger 的行归入 ``(unparsed)``，这样"有多少行没能
    识别"本身也是可见的信息，不会被静默吞掉。
    """
    counts: Dict[str, int] = {}
    for e in entries:
        stripped = _strip_ansi(e.line)
        if dimension == "level":
            key = _parse_line_level(stripped) or "(unparsed)"
        elif dimension == "logger":
            key = _parse_line_logger(stripped) or "(unparsed)"
        else:  # template
            key = _truncate_line(_fold_template(stripped), 160)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _render_group_table(
    counts: Dict[str, int], *, dimension: str, total: int
) -> List[str]:
    """把分组计数渲染成 Markdown 表格（按条数降序，含占比）。"""
    if not counts:
        return ["_No groups found_"]
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    truncated = 0
    if dimension == "template" and len(ordered) > _GROUP_BY_TEMPLATE_TOP_N:
        truncated = len(ordered) - _GROUP_BY_TEMPLATE_TOP_N
        ordered = ordered[:_GROUP_BY_TEMPLATE_TOP_N]
    header = {"level": "Level", "logger": "Logger", "template": "Template"}[dimension]
    lines = [f"| {header} | Count | Share |", "|---|---:|---:|"]
    for key, count in ordered:
        share = f"{(count / total * 100):.1f}%" if total else "-"
        # 竖线会破坏 Markdown 表格，转义掉。
        safe = key.replace("|", r"\|")
        lines.append(f"| `{safe}` | {count} | {share} |")
    if truncated:
        lines.append(
            f"| _…+{truncated} more template(s) omitted_ | | |",
        )
    return lines


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------
def register_tools(mcp: FastMCP) -> None:
    """把全部工具注册到给定的 FastMCP 实例上。"""

    # ----- health_check -------------------------------------------------
    @mcp.tool()
    async def health_check(ctx: Context) -> str:
        """检查日志后端健康状态。

        返回后端聚合状态、各 Loki 实例状态、当前时间，以及本会话生效
        的客户端租户范围（Allowed Tenants）和过滤来源。

        建议作为查询前的第一步调用。如果 Filter Source 显示 unset，
        说明客户端未声明可见租户，其它查询/下载工具都会拒绝执行；这时
        应让用户在 MCP 客户端配置中添加 X-Allowed-Tenants（HTTP 模式）
        或 LOKI_CLIENT_TENANTS（stdio 模式）。
        """
        backend, config = _require_state()
        logger.info("Tool: health_check")
        info = await backend.health_check()
        status = info.get("status", "unknown")
        status_text = {
            "healthy": "Healthy",
            "degraded": "Degraded (some clusters unhealthy)",
            "unhealthy": "Unhealthy",
        }.get(status, status)

        clusters = info.get("clusters") or []
        clusters_md = ""
        if clusters:
            lines = [
                "",
                "## Clusters",
                "",
                "| Cluster | Address | Status | Detail |",
                "|---|---|---|---|",
            ]
            for c in clusters:
                detail = c.get("error") or c.get("version") or ""
                lines.append(
                    f"| `{c.get('id', '-')}` | `{c.get('server_addr', '-')}` "
                    f"| {c.get('status', '-')} | {detail} |"
                )
            clusters_md = "\n".join(lines) + "\n"

        client, source = _client_filter_for(ctx, config)
        if client is None:
            effective: Optional[List[str]] = None
        else:
            server_set = set(backend.tenants)
            effective = [t for t in client if t in server_set]

        if client is not None and source == "header":
            filter_source = "request header X-Allowed-Tenants"
        elif client is not None and source == "env":
            filter_source = "env LOKI_CLIENT_TENANTS"
        elif source == "header":
            filter_source = (
                "(unset — log-query tools are disabled until the MCP "
                "client sends an 'X-Allowed-Tenants' request header)"
            )
        else:
            filter_source = (
                "(unset — log-query tools are disabled until "
                "'LOKI_CLIENT_TENANTS' is set in the MCP client env "
                "block, e.g. mcp.json)"
            )

        if effective is None:
            allowed_display = "(unset — log-query tools disabled)"
        elif not effective:
            allowed_display = "(empty — intersection with server tenants is empty)"
        else:
            allowed_display = ", ".join(effective)

        report = (
            f"# Log Backend Health Check\n\n"
            f"**Backend:** `{info.get('backend', backend.name)}`\n"
            f"**Status:** {status_text}\n"
            f"**Configured Clusters:** {len(clusters)}\n"
            f"**Server Tenants:** {', '.join(backend.tenants) or '-'}\n"
            f"**Allowed Tenants (this session):** {allowed_display}\n"
            f"**Filter Source:** {filter_source}\n"
            f"**Timezone:** {info.get('timezone', config.timezone)}\n"
            f"**Current Time:** {info.get('current_time', '-')}\n"
            f"{clusters_md}\n## Details\n"
            f"```json\n{json.dumps(info, indent=2, default=str)}\n```\n"
        )
        return report

    # ----- query_logs ---------------------------------------------------
    @mcp.tool()
    async def query_logs(
        query: str,
        ctx: Context,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
        direction: str = "backward",
        tenant: Optional[str] = None,
        instance: Optional[str] = None,
        verbosity: Optional[str] = None,
        strip_ansi: Optional[bool] = None,
        min_level: Optional[str] = None,
        fold_scope: Optional[str] = None,
        exclude_loggers: Optional[List[str]] = None,
        dedup_timestamp: Optional[bool] = None,
        sample_per_template: Optional[int] = None,
        hoist_common_prefix: Optional[bool] = None,
        collapse_whitespace: Optional[bool] = None,
        hoist_common_date: Optional[bool] = None,
    ) -> str:
        """按 LogQL 查询指定租户（或全部可见的租户）的日志。

        推荐工作流（多租户场景）：
        1. get_labels()：发现各租户下有哪些标签名。
        2. get_label_values(label="<标签>")：查看各租户下该标签的值，
           定位目标值归属的租户。
        3. query_logs(tenant="<id>", query='{<label>="<value>"}')：
           查询指定租户的日志。

        参数：
          query     必填。LogQL 日志选择器，如 {job="nginx"} |= "error"。
                    不支持指标表达式（rate、count_over_time 等）。
          start     起始时间（RFC3339 / ISO 8601）。省略时默认为30分钟前。
          end       结束时间（RFC3339 / ISO 8601）。省略时默认为当前时间。
          limit     每个租户的返回条数上限。省略时使用 LOG_DEFAULT_LIMIT；
                    除非用户明确要求"只看前 N 条"或"我要更多"，否则不要显式传值。
          direction backward（默认，最新在前）或 forward（最早在前）。
          tenant    指定租户 ID。省略时查询所有可见租户。
          instance  指定 Loki 实例 ID（可从 health_check 输出查到）。
                    省略时按所有健康实例并发查询。
          verbosity 返回体详略档位，省略时用服务端默认（LOG_DEFAULT_VERBOSITY，
                    默认 compact）：
                      * compact —— 只输出日志正文，一行一条，无 Entry 头 /
                        Time / Labels / 编号；token 开销最低，常规排查首选。
                      * normal  —— 正文 + 短格式时间 + 差异标签，公共标签在
                        头部只输出一次。
                      * full    —— 完整格式（Entry 头 + 纳秒 isoformat
                        时间 + 全量标签），用于需要逐条精确元数据的场景。
                    非法取值会报错。若要把大量日志拉到本地分析，请改用
                    download_logs 而非提高 verbosity。
          strip_ansi 是否剥离正文中的 ANSI 颜色码。省略时用服务端默认
                    （LOG_STRIP_ANSI，默认 true）。这是**零信息损失**的
                    优化（级别文字本来就在正文里），一般无需关心；只有
                    需要原样字节时才传 false。full 模式恒不剥离。
          min_level 最低日志级别过滤，**默认 None 表示不过滤**——排查
                    问题必须先看到完整上下文，所以不要习惯性地传这个
                    参数。取值（大小写不敏感）：TRACE < DEBUG < INFO <
                    SUCCESS < WARNING < ERROR < CRITICAL（WARN 是
                    WARNING 的别名）。传值后会**隐藏**低于该级别的行，
                    输出里会报出被隐藏的条数；解析不出级别的行一律保留。
                    仅在用户明确只关心"有没有报错 / 只要概览"时使用。
          fold_scope compact 模式下重复行折叠的作用域，省略时用服务端
                    默认（LOG_FOLD_SCOPE，默认 adjacent）：
                      * adjacent —— 只折叠连续同模板行，不打乱时序，
                        语义最安全（默认）。
                      * global   —— 全窗口按模板聚类（不要求相邻），
                        省 token 最多，但属于**有损概览**：同模板行会
                        被汇总到首次出现的位置。仅在用户只需要"这段
                        时间大致发生了什么"时使用。
                    两种作用域都不会折叠 WARNING / ERROR / CRITICAL 行。
          exclude_loggers 噪音源 logger 黑名单，**默认 None 表示不排除任何
                    东西**（省略时取服务端 LOG_DEFAULT_EXCLUDE_LOGGERS，
                    其默认也是空）。传入 logger 名列表可屏蔽库级 trace /
                    access 日志（例如 ["httpcore.*",
                    "uvicorn.protocols.http.h11_impl"]），这类噪音源往往
                    占据大部分正文字符。支持 fnmatch 通配（``httpcore.*``
                    前缀匹配、
                    精确名亦可）。**会隐藏信息**：仅在只需业务侧概览时
                    使用；WARNING / ERROR / CRITICAL 行即使命中黑名单也
                    一定保留，解析不出 logger 的行也一律保留，被隐藏的
                    条数会在输出中报出。full 模式不生效。
          dedup_timestamp 是否去掉正文开头与行首重复的时间戳（**仅 normal
                    模式生效**；compact 行首没有时间，正文里的时间戳是
                    唯一时间来源，因此恒不去重，full 同样不处理）。省略时
                    用服务端默认（LOG_DEDUP_TIMESTAMP，**默认 false**）。
                    它会改写日志正文，而正文是排查依据，所以不默认开启；
                    需要多省 14% 正文时再显式传 true（只在时间确实吻合
                    时才去掉，不吻合保持原样）。
          sample_per_template 分层采样：每种日志模板最多保留 N 条，**默认
                    None 表示不采样**。用于 limit 被单一噪音模板占满、
                    稀有模板被挤出结果的场景（一批日志里的唯一模板数通常
                    远少于条数）。**会隐藏信息**：仅在只需"有哪几种日志"的概览
                    时使用；WARNING / ERROR / CRITICAL 行完全不参与采样、
                    全部保留，结果保持时间顺序，被隐藏的条数会报出。
                    full 模式不生效。
          hoist_common_prefix / collapse_whitespace / hoist_common_date
                    三项 **零信息损失** 的正文瘦身，**默认全部开启**（省略
                    时分别取 LOG_HOIST_COMMON_PREFIX / LOG_COLLAPSE_WHITESPACE
                    / LOG_HOIST_COMMON_DATE，默认均为 true）；每项可单独
                    传 false 关闭。时分秒、级别、模块名、行号、业务 ID、
                    HTTP 状态码、异常关键字、堆栈缩进 **全部完整保留**，
                    被提取 / 省略的内容都会在返回体头部标注一次，可自行
                    还原。三项均 **不作用于 full 模式**（full 是逃生舱）。
                      * hoist_common_prefix —— 所有输出行的最长公共前缀
                        （≥8 字符时）提到头部 `**Common Prefix:**` 只出现
                        一次，行内去掉。compact 与 normal 均生效；只有
                        1 行、前缀过短、或前缀会把某行吃空时自动不上提。
                      * collapse_whitespace —— 行内连续 ≥2 个**空格**压成
                        1 个（loguru 级别对齐填充是主要来源）。只处理空格，
                        tab 与换行不碰；**行首缩进原样保留**，堆栈跟踪与
                        YAML / JSON 片段的结构不受影响。compact 与 normal
                        均生效。
                      * hoist_common_date —— **仅 compact**：省略正文开头
                        重复的 `YYYY-MM-DD ` 日期，
                        `HH:MM:SS(.mmm)` 一定保留（compact 行首没有时间，
                        它是唯一时间源），省略的日期在头部 `**Date:**`
                        标注。**跨天自动整批禁用**：只要批次里出现 ≥2 个
                        不同日期就完全保持原样。normal 行首已有 MM-DD
                        短日期，改由 dedup_timestamp 处理。

        提醒：默认路径（不传 min_level / exclude_loggers /
        sample_per_template，fold_scope 用 adjacent）保证语义完整——不过滤
        级别、不排除任何 logger、不采样、不打乱时序、不丢任何一条日志。
        min_level、exclude_loggers、sample_per_template 与
        fold_scope=global 都会隐藏信息，仅在只需概览时使用；想先看分布
        再决定拉哪些正文，用 count_logs(group_by=...)，那是最省 token 的
        诊断入口。

        客户端必须先在 MCP 配置中声明可见租户（X-Allowed-Tenants 或
        LOKI_CLIENT_TENANTS），否则本工具直接拒绝。
        """
        backend, config = _require_state()

        if verbosity is None:
            verbosity = config.default_verbosity
        verbosity = verbosity.strip().lower()
        if verbosity not in ("compact", "normal", "full"):
            raise RuntimeError(
                f"Invalid verbosity {verbosity!r}; must be 'compact', "
                "'normal' or 'full'."
            )

        # full 是绝对逃生舱：不剥 ANSI、不折叠、不过滤，保持原始字节。
        effective_strip_ansi = (
            config.strip_ansi if strip_ansi is None else bool(strip_ansi)
        )
        if verbosity == "full":
            effective_strip_ansi = False

        effective_min_level: Optional[str] = None
        if min_level is not None:
            effective_min_level = _normalise_level(min_level)
            if effective_min_level is None:
                raise RuntimeError(
                    f"Invalid min_level {min_level!r}; must be one of: "
                    "TRACE, DEBUG, INFO, SUCCESS, WARNING, ERROR, "
                    "CRITICAL (WARN / FATAL accepted as aliases)."
                )

        # exclude_loggers 默认为 None → 取服务端配置（其默认为空列表，
        # 即开箱不排除任何 logger）。显式传空列表同样表示"不排除"。
        if exclude_loggers is None:
            effective_exclude_loggers = list(config.default_exclude_loggers)
        else:
            # MCP 客户端偶尔会把列表参数传成一个逗号分隔字符串，这里
            # 宽容地接受这种写法，而不是直接报错。
            if isinstance(exclude_loggers, str):  # type: ignore[unreachable]
                exclude_loggers = [  # type: ignore[unreachable]
                    p for p in exclude_loggers.split(",")
                ]
            effective_exclude_loggers = [
                str(p).strip() for p in exclude_loggers if str(p).strip()
            ]

        effective_dedup_timestamp = (
            config.dedup_timestamp if dedup_timestamp is None else bool(dedup_timestamp)
        )

        # A 档零信息损失瘦身：三项默认开启，可分别关闭；full 一律不生效。
        effective_hoist_prefix = (
            config.hoist_common_prefix
            if hoist_common_prefix is None
            else bool(hoist_common_prefix)
        )
        effective_collapse_ws = (
            config.collapse_whitespace
            if collapse_whitespace is None
            else bool(collapse_whitespace)
        )
        effective_hoist_date = (
            config.hoist_common_date
            if hoist_common_date is None
            else bool(hoist_common_date)
        )
        if verbosity == "full":
            effective_hoist_prefix = False
            effective_collapse_ws = False
            effective_hoist_date = False

        if sample_per_template is not None:
            if not isinstance(sample_per_template, int) or isinstance(
                sample_per_template, bool
            ):
                raise RuntimeError("sample_per_template must be a positive integer")
            if sample_per_template <= 0:
                raise RuntimeError("sample_per_template must be a positive integer")

        effective_fold_scope = (
            config.fold_scope if fold_scope is None else fold_scope.strip().lower()
        )
        if effective_fold_scope not in ("adjacent", "global"):
            raise RuntimeError(
                f"Invalid fold_scope {effective_fold_scope!r}; must be "
                "'adjacent' or 'global'."
            )

        try:
            start_dt, end_dt = resolve_time_range(
                start, end, config.default_time_range_minutes
            )
        except ValidationError as e:
            raise RuntimeError(str(e)) from e

        if direction not in ("forward", "backward"):
            raise RuntimeError(
                f"Invalid direction {direction!r}; must be 'forward' or 'backward'."
            )

        if limit is not None:
            if not isinstance(limit, int) or limit <= 0:
                raise RuntimeError("limit must be a positive integer")
            if limit > config.max_limit:
                raise RuntimeError(
                    f"limit {limit} exceeds maximum {config.max_limit} "
                    f"(LOG_MAX_LIMIT)"
                )
        effective_limit = limit if limit is not None else config.default_limit

        tenants = _resolve_tenants(backend, config, tenant, ctx)

        logger.info(
            "Tool: query_logs",
            tenants=tenants,
            query=query,
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
            limit=effective_limit,
            direction=direction,
        )

        async def run_for_tenant(
            t: str,
            cluster_errors: Dict[str, str],
            cluster_warnings: Dict[str, str],
        ) -> List[LogEntry]:
            return await backend.query_logs(
                query=query,
                tenant=t,
                start=start_dt,
                end=end_dt,
                limit=effective_limit,
                direction=direction,
                instance=instance,
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )

        results = await _fan_out(tenants, run_for_tenant)
        all_entries: List[LogEntry] = []
        for r in results:
            if r.ok and r.data:
                all_entries.extend(r.data)

        successful = [r.tenant for r in results if r.ok]
        failed_tenants = [r for r in results if not r.ok]
        any_cluster_failure = any(r.cluster_errors for r in results)
        truncated = any(
            r.ok and r.data is not None and len(r.data) >= effective_limit
            for r in results
        )

        # min_level / exclude_loggers / sample_per_template 都是"会隐藏
        # 内容"的可选能力；full 模式是绝对逃生舱，一律不参与。
        hidden_by_level: Dict[str, int] = {}
        if effective_min_level is not None and verbosity != "full":
            all_entries, hidden_by_level = _filter_by_min_level(
                all_entries, effective_min_level
            )

        hidden_by_logger: Dict[str, int] = {}
        if effective_exclude_loggers and verbosity != "full":
            all_entries, hidden_by_logger = _filter_by_exclude_loggers(
                all_entries, effective_exclude_loggers
            )

        sampled_hidden = 0
        sampled_templates = 0
        if sample_per_template is not None and verbosity != "full":
            all_entries, sampled_hidden, sampled_templates = _sample_per_template(
                all_entries, sample_per_template
            )

        def _min_level_note() -> str:
            if not hidden_by_level:
                return ""
            breakdown = " / ".join(
                f"{lvl} {cnt}"
                for lvl, cnt in sorted(
                    hidden_by_level.items(), key=lambda kv: _LEVEL_ORDER[kv[0]]
                )
            )
            total_hidden = sum(hidden_by_level.values())
            return (
                f"\n> ℹ️ min_level={effective_min_level} 已隐藏 "
                f"{total_hidden} 条（{breakdown}）。用 min_level=DEBUG "
                "或省略该参数查看全部。\n"
            )

        def _exclude_loggers_note() -> str:
            if not hidden_by_logger:
                return ""
            # 按条数降序列出贡献最大的噪音源，最多 5 个，避免提示本身太长。
            top = sorted(hidden_by_logger.items(), key=lambda kv: (-kv[1], kv[0]))
            breakdown = " / ".join(f"{name} {cnt}" for name, cnt in top[:5])
            if len(top) > 5:
                breakdown += f" / …+{len(top) - 5} more"
            total_hidden = sum(hidden_by_logger.values())
            return (
                f"\n> ℹ️ exclude_loggers 已隐藏 {total_hidden} 条"
                f"（{breakdown}）。异常级别行不受影响。省略该参数可查看"
                "全部。\n"
            )

        def _sample_note() -> str:
            if not sampled_hidden:
                return ""
            return (
                f"\n> ℹ️ sample_per_template={sample_per_template} 已隐藏 "
                f"{sampled_hidden} 条（{sampled_templates} 个模板，异常行"
                "全部保留）。省略该参数或用 download_logs 查看全部。\n"
            )

        tenant_set = {e.tenant for e in all_entries if e.tenant is not None}
        cluster_set = {e.cluster for e in all_entries if e.cluster is not None}
        single_origin = len(tenant_set) <= 1 and len(cluster_set) <= 1

        if verbosity == "full":
            header = (
                f"# Log Query Results\n\n"
                f"**Backend:** `{backend.name}`\n"
                f"**Query:** `{query}`\n"
                f"**Time Range:** "
                f"`{format_in_tz(start_dt, config.timezone)}` to "
                f"`{format_in_tz(end_dt, config.timezone)}`\n"
                f"**Limit:** {effective_limit} per tenant\n"
                f"**Direction:** {direction}\n"
                f"**Tenants Queried:** `{', '.join(tenants)}`\n"
                f"**Successful Tenants:** `{', '.join(successful) or '-'}`\n"
                f"**Instance:** `{instance or '*all healthy*'}`\n"
                f"**Total Entries:** {len(all_entries)}\n"
            )
        else:
            # compact / normal：头部压到 2~3 行核心信息。Query 截断到
            # ~120 字符，时间用短格式。
            query_display = query if len(query) <= 120 else query[:117] + "..."
            header = (
                f"# Log Query Results\n\n"
                f"**Query:** `{query_display}`\n"
                f"**Time Range:** "
                f"`{format_short(start_dt, config.timezone)}` to "
                f"`{format_short(end_dt, config.timezone)}` "
                f"| dir: {direction} | tenants: "
                f"`{', '.join(successful) or '-'}` "
                f"| Total Entries: {len(all_entries)}\n"
            )

        def _truncation_note() -> str:
            if not truncated:
                return ""
            # backward 时最早覆盖到的时间戳是 all_entries[-1]；空列表兜底。
            if all_entries:
                edge = all_entries[-1].timestamp
                edge_str = format_short(edge, config.timezone)
                coverage = (
                    f" Results only cover back to `{edge_str}`."
                    if direction == "backward"
                    else f" Results only cover up to `{edge_str}`."
                )
            else:
                coverage = ""
            return (
                "\n> ⚠️ Result reached the per-tenant limit "
                f"({effective_limit}); some entries may have been "
                "truncated." + coverage + " Narrow the time range or use "
                "download_logs to capture everything.\n"
            )

        if not all_entries and failed_tenants and not successful:
            return (
                header
                + _format_failures(results)
                + _format_warnings(results)
                + "\nAll tenant queries failed.\n"
            )
        if not all_entries:
            tail = _format_failures(results) + _format_warnings(results)
            note = ""
            if not failed_tenants and not any_cluster_failure:
                note = "\nNo log entries found.\n"
            elif not failed_tenants and any_cluster_failure:
                note = (
                    "\nNo log entries found in the surviving clusters; see "
                    "errors above.\n"
                )
            return (
                header
                + _min_level_note()
                + _exclude_loggers_note()
                + _sample_note()
                + tail
                + note
            )

        common_header = ""
        fold_note = ""
        dedup_note = ""
        slim_header = ""
        trailing = "\n"  # compact / normal body 不以换行结尾，补一个
        if verbosity == "full":
            body = _format_log_entries(all_entries, config.timezone)
            trailing = ""  # full 的 body 自带尾部换行
        elif verbosity == "compact":
            rendered = _render_compact(
                all_entries,
                single_origin=single_origin,
                max_line_chars=config.max_line_chars,
                fold_repeats=config.fold_repeats,
                strip_ansi=effective_strip_ansi,
                fold_scope=effective_fold_scope,
                timezone_name=config.timezone,
                collapse_whitespace=effective_collapse_ws,
                hoist_common_date=effective_hoist_date,
                hoist_common_prefix=effective_hoist_prefix,
            )
            body = rendered.body
            fold_note = rendered.fold_note or ""
            slim_header = _slim_header(rendered)
        else:  # normal
            common, differing = _split_common_labels(all_entries)
            if common:
                common_header = (
                    f"**Common Labels:** " f"{{{_common_labels_str(common)}}}\n"
                )
            rendered = _render_normal(
                all_entries,
                config.timezone,
                single_origin=single_origin,
                differing_keys=differing,
                max_line_chars=config.max_line_chars,
                strip_ansi=effective_strip_ansi,
                dedup_timestamp=effective_dedup_timestamp,
                collapse_whitespace=effective_collapse_ws,
                hoist_common_prefix=effective_hoist_prefix,
            )
            body = rendered.body
            slim_header = _slim_header(rendered)
            if rendered.deduped_timestamp:
                dedup_note = (
                    "\n> ℹ️ 正文内与行首重复的时间戳已省略"
                    "（dedup_timestamp=false 可保留）。\n"
                )

        return (
            header
            + common_header
            + slim_header
            + _min_level_note()
            + _exclude_loggers_note()
            + _sample_note()
            + dedup_note
            + _truncation_note()
            + "\n"
            + body
            + trailing
            + fold_note
            + _format_failures(results)
            + _format_warnings(results)
        )

    # ----- get_labels ---------------------------------------------------
    @mcp.tool()
    async def get_labels(
        ctx: Context,
        start: Optional[str] = None,
        end: Optional[str] = None,
        tenant: Optional[str] = None,
        instance: Optional[str] = None,
    ) -> str:
        """列出某租户（或全部可见租户）下的标签名。

        通常作为日志查询的第一步使用：先看有哪些标签可用，再用
        get_label_values 定位目标值归属的租户。

        参数：
          start    可选时间范围起点（RFC3339）。与 end 同时给出时，
                   只返回该时间窗内出现过的标签。省略时默认为30分钟前。
          end      可选时间范围终点（RFC3339），省略时默认为当前时间。
          tenant   指定租户 ID。省略时查询全部可见的租户。
          instance 指定 Loki 实例 ID。省略时按所有健康实例并发查询。
        """
        return await _list_keys(
            label=None,
            start=start,
            end=end,
            heading="Available Labels",
            tenant=tenant,
            instance=instance,
            ctx=ctx,
        )

    # ----- get_label_values ---------------------------------------------
    @mcp.tool()
    async def get_label_values(
        label: str,
        ctx: Context,
        start: Optional[str] = None,
        end: Optional[str] = None,
        tenant: Optional[str] = None,
        instance: Optional[str] = None,
    ) -> str:
        """列出某个标签的所有取值。

        用于在 query_logs 之前确认目标值归属于哪个租户。

        参数：
          label    必填。标签名。
          start    可选时间范围起点（RFC3339）。与 end 同时给出时，
                   只返回该时间窗内出现过的标签。省略时默认为30分钟前。
          end      可选时间范围终点（RFC3339），省略时默认为当前时间。
          tenant   指定租户 ID。省略时查询全部可见的租户。
          instance 指定 Loki 实例 ID。省略时按所有健康实例并发查询。
        """
        if not label or not label.strip():
            raise RuntimeError("Label name cannot be empty")
        return await _list_keys(
            label=label,
            start=start,
            end=end,
            heading=f"Values for Label `{label}`",
            tenant=tenant,
            instance=instance,
            ctx=ctx,
        )

    # ----- download_logs ------------------------------------------------
    @mcp.tool()
    async def download_logs(
        query: str,
        ctx: Context,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
        direction: str = "backward",
        tenant: Optional[str] = None,
        instance: Optional[str] = None,
        fmt: Optional[str] = None,
    ) -> str:
        """按 LogQL 查询日志并写到文件，让用户离线下载到本地分析。

        适用场景：用户希望把日志拉到本地用 grep / jq / Excel 等方式
        处理，不需要 AI 在对话里复述日志内容。

        参数：
          query     必填。LogQL 日志选择器，与 query_logs 一致。
          start     起始时间（RFC3339 / ISO 8601）。强烈建议显式给出，
                    避免一次拉过多数据。
          end       结束时间（RFC3339 / ISO 8601）。
          limit     每个租户的返回条数上限。省略时使用 LOG_MAX_LIMIT。
          direction backward（默认，最新在前）或 forward（最早在前）。
          tenant    指定租户 ID。省略时查询所有客户端可见的租户。
          instance  指定 Loki 实例 ID。
          fmt       输出格式，可选 txt / jsonl / csv。省略时使用
                    LOG_DEFAULT_DOWNLOAD_FORMAT（默认 txt，已瘦身：
                    公共标签在文件头输出一次、行内只留差异标签）。
                    需要结构化后处理（jq / 入库）时显式传 jsonl / csv。

        返回：
          命中 0 条时不生成文件，只返回空结果提示。
          HTTP 模式（streamable-http / sse）：有日志时返回完整下载
          URL，用户在本机用浏览器或 curl -O 拉取；链接默认 60 分钟
          过期，且成功下载一次后立即失效（一次性链接）。
          stdio 模式：有日志时返回服务端绝对路径（即用户本机路径），
          直接打开即可。
        """
        backend, config = _require_state()
        registry = _get_download_registry()

        fmt = fmt if fmt is not None else config.default_download_format
        if fmt not in SUPPORTED_FORMATS:
            raise RuntimeError(
                f"Unsupported fmt {fmt!r}. Choose one of: "
                f"{', '.join(SUPPORTED_FORMATS)}."
            )

        try:
            start_dt, end_dt = resolve_time_range(
                start, end, config.default_time_range_minutes
            )
        except ValidationError as e:
            raise RuntimeError(str(e)) from e

        if direction not in ("forward", "backward"):
            raise RuntimeError(
                f"Invalid direction {direction!r}; must be 'forward' or 'backward'."
            )

        if limit is not None:
            if not isinstance(limit, int) or limit <= 0:
                raise RuntimeError("limit must be a positive integer")
            if limit > config.max_limit:
                raise RuntimeError(
                    f"limit {limit} exceeds maximum {config.max_limit} "
                    f"(LOG_MAX_LIMIT)"
                )
        # 下载默认 limit == max_limit，单次尽可能多拉；如果用户要更少
        # 数据，再显式传 limit。
        effective_limit = limit if limit is not None else config.max_limit

        tenants = _resolve_tenants(backend, config, tenant, ctx)

        logger.info(
            "Tool: download_logs",
            tenants=tenants,
            query=query,
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
            limit=effective_limit,
            direction=direction,
            fmt=fmt,
        )

        async def run_for_tenant(
            t: str,
            cluster_errors: Dict[str, str],
            cluster_warnings: Dict[str, str],
        ) -> List[LogEntry]:
            return await backend.query_logs(
                query=query,
                tenant=t,
                start=start_dt,
                end=end_dt,
                limit=effective_limit,
                direction=direction,
                instance=instance,
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )

        results = await _fan_out(tenants, run_for_tenant)
        all_entries: List[LogEntry] = []
        for r in results:
            if r.ok and r.data:
                all_entries.extend(r.data)

        successful = [r.tenant for r in results if r.ok]
        failed_tenants = [r for r in results if not r.ok]
        limit_reached = any(
            r.ok and r.data is not None and len(r.data) >= effective_limit
            for r in results
        )

        if not all_entries and failed_tenants and not successful:
            return (
                "# Download Failed\n\n"
                "All tenant queries failed; nothing was written.\n"
                + _format_failures(results)
            )

        if not all_entries:
            return (
                f"# Log Download Empty\n\n"
                f"**Backend:** `{backend.name}`\n"
                f"**Query:** `{query}`\n"
                f"**Time Range:** "
                f"`{format_in_tz(start_dt, config.timezone)}` to "
                f"`{format_in_tz(end_dt, config.timezone)}`\n"
                f"**Tenants Queried:** `{', '.join(tenants)}`\n"
                f"**Successful Tenants:** `{', '.join(successful) or '-'}`\n"
                f"**Instance:** `{instance or '*all healthy*'}`\n"
                f"**Format:** `{fmt}`\n"
                f"**Entries:** 0\n\n"
                "Query succeeded, but no log entries matched. "
                "No download file was created.\n"
                f"{_format_failures(results)}"
                f"{_format_warnings(results)}"
            )

        # 文件名用 tenant 名；多租户时用 "all"。
        tenant_label = tenant or (tenants[0] if len(tenants) == 1 else "all")
        try:
            config.download_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(
                f"Cannot create LOG_DOWNLOAD_DIR " f"{str(config.download_dir)!r}: {e}"
            ) from e

        now_utc = datetime.now(tz=_tz.utc)
        # 两个文件名：
        # * ``filename`` 是用户侧看到的名字（HTTP Content-Disposition）
        # * ``on_disk_name`` 多加一段随机 hex，避免同一秒内的并发下载
        #   在磁盘上互相覆盖；用户看不到这一段，因为 registry 用前者作
        #   为下载文件名。
        filename = build_filename(tenant_label=tenant_label, fmt=fmt, now=now_utc)
        on_disk_name = build_filename(
            tenant_label=tenant_label,
            fmt=fmt,
            now=now_utc,
            suffix=secrets.token_hex(4),
        )
        target_path = (config.download_dir / on_disk_name).resolve()

        # 纵深防御：目标路径必须在 download_dir 之内（防 symlink 逃逸）。
        download_root = config.download_dir.resolve()
        try:
            target_path.relative_to(download_root)
        except ValueError as e:  # pragma: no cover — symlink 异常时才触发
            raise RuntimeError(
                f"Refusing to write outside LOG_DOWNLOAD_DIR: {target_path}"
            ) from e

        result = write_download(
            all_entries,
            target_path=target_path,
            fmt=fmt,
            timezone=config.timezone,
        )

        # 拼用户侧可见的"取件方式"：HTTP 模式用 URL，stdio 用绝对路径。
        delivery: str
        if registry is not None:
            entry = await registry.register(
                path=result.path,
                fmt=fmt,
                download_filename=filename,
            )
            url = _make_download_url(ctx, config, entry.token)
            if url is None:
                # 走到这里说明 HTTP 模式但无法推断 base URL，正常配置下
                # 几乎不会发生。退化方案：把 token 显式给出来，便于
                # 运维侧排查。
                delivery = (
                    f"**Token:** `{entry.token}` — set "
                    "`LOG_DOWNLOAD_BASE_URL` so the server can render "
                    "a complete URL."
                )
            else:
                delivery = (
                    f"**Download URL:** {url}\n"
                    f"_Link expires in {registry.ttl_seconds // 60} "
                    "minutes._"
                )
        else:
            delivery = f"**Path:** `{str(result.path)}`"

        size_kb = result.byte_size / 1024
        truncated_note = ""
        if limit_reached:
            truncated_note = (
                "\n> ⚠️ Result reached the per-tenant limit "
                f"({effective_limit}); some entries may have been "
                "truncated. Narrow the time range and download again "
                "to capture everything.\n"
            )

        report = (
            f"# Log Download Ready\n\n"
            f"**Backend:** `{backend.name}`\n"
            f"**Query:** `{query}`\n"
            f"**Time Range:** "
            f"`{format_in_tz(start_dt, config.timezone)}` to "
            f"`{format_in_tz(end_dt, config.timezone)}`\n"
            f"**Tenants Queried:** `{', '.join(tenants)}`\n"
            f"**Successful Tenants:** `{', '.join(successful) or '-'}`\n"
            f"**Instance:** `{instance or '*all healthy*'}`\n"
            f"**Format:** `{fmt}`\n"
            f"**Entries:** {result.entry_count}\n"
            f"**Size:** {size_kb:.1f} KB\n"
            f"{delivery}\n"
            f"{truncated_note}"
            f"{_format_failures(results)}"
            f"{_format_warnings(results)}"
        )
        return report

    # ----- count_logs ---------------------------------------------------
    @mcp.tool()
    async def count_logs(
        query: str,
        ctx: Context,
        start: Optional[str] = None,
        end: Optional[str] = None,
        tenant: Optional[str] = None,
        instance: Optional[str] = None,
        group_by: Optional[str] = None,
    ) -> str:
        """探量 / 分布画像：不返回日志正文，只回条数或构成分布。

        典型用法：在 query_logs / download_logs 之前先探一下量，判断
        该缩小时间范围、还是直接 query_logs 看、还是走 download_logs
        拉到本地。只回一个整数，几乎不占用返回体 token。

        参数：
          query    必填。LogQL 日志选择器（与 query_logs 一致，不含
                   聚合表达式）。
          start    起始时间（RFC3339 / ISO 8601）。省略时默认为30分钟前。
          end      结束时间（RFC3339 / ISO 8601）。省略时默认为当前时间。
          tenant   指定租户 ID。省略时对所有可见租户分别计数。
          instance 指定 Loki 实例 ID。省略时按所有健康实例求和。
          group_by 分布画像维度，**默认 None 表示只回总条数**。取值：
                     * level    —— 按日志级别分布，优先下推到 Loki
                       （``sum by (detected_level) (count_over_time(...))``）。
                     * logger   —— 按 logger / 模块名分布，用于定位噪音源
                       （单个库级 logger 常占据大部分正文字符）。
                     * template —— 按归一化后的日志模板分布（Top 20），
                       用于判断"这段日志一共有几种事件"。
                   无法下推到后端的维度会退化为**抽样估算**（拉最多
                   1000 条样本在服务端解析），输出里会明确标注是估算值，
                   不要当成全量精确数。

        **强烈建议**：在 query_logs / download_logs 拉正文之前，先用
        group_by 看一眼分布——这是本服务最省 token 的诊断入口，一次调用
        就能知道日志由哪些级别 / 模块 / 事件构成，从而精确决定要拉哪一
        部分正文（配合 query_logs 的 min_level / exclude_loggers）。

        客户端必须先声明可见租户（X-Allowed-Tenants 或
        LOKI_CLIENT_TENANTS），否则本工具直接拒绝。
        """
        backend, config = _require_state()

        try:
            start_dt, end_dt = resolve_time_range(
                start, end, config.default_time_range_minutes
            )
        except ValidationError as e:
            raise RuntimeError(str(e)) from e

        dimension: Optional[str] = None
        if group_by is not None:
            dimension = group_by.strip().lower()
            if dimension not in _GROUP_BY_DIMENSIONS:
                raise RuntimeError(
                    f"Invalid group_by {group_by!r}; must be one of: "
                    f"{', '.join(sorted(_GROUP_BY_DIMENSIONS))}."
                )

        tenants = _resolve_tenants(backend, config, tenant, ctx)

        logger.info(
            "Tool: count_logs",
            tenants=tenants,
            query=query,
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
            group_by=dimension,
        )

        if dimension is not None:
            return await _count_logs_grouped_report(
                backend=backend,
                config=config,
                query=query,
                tenants=tenants,
                start_dt=start_dt,
                end_dt=end_dt,
                instance=instance,
                dimension=dimension,
            )

        async def run_for_tenant(
            t: str,
            cluster_errors: Dict[str, str],
            cluster_warnings: Dict[str, str],
        ) -> int:
            del cluster_warnings
            return await backend.count_logs(
                query=query,
                tenant=t,
                start=start_dt,
                end=end_dt,
                instance=instance,
                cluster_errors=cluster_errors,
            )

        results = await _fan_out(tenants, run_for_tenant)
        successful = [r.tenant for r in results if r.ok]
        total = sum(int(r.data) for r in results if r.ok and r.data is not None)

        per_tenant_lines: List[str] = []
        for r in results:
            if r.ok:
                per_tenant_lines.append(f"- `{r.tenant}`: {int(r.data or 0)}")
            else:
                per_tenant_lines.append(f"- `{r.tenant}`: _error_")

        query_display = query if len(query) <= 120 else query[:117] + "..."
        report = (
            f"# Log Count\n\n"
            f"**Query:** `{query_display}`\n"
            f"**Time Range:** "
            f"`{format_short(start_dt, config.timezone)}` to "
            f"`{format_short(end_dt, config.timezone)}`\n"
            f"**Instance:** `{instance or '*all healthy*'}`\n"
            f"**Successful Tenants:** `{', '.join(successful) or '-'}`\n\n"
            + "\n".join(per_tenant_lines)
            + f"\n\n**Total:** {total}\n"
            + _format_failures(results)
        )
        return report

    logger.info("All tools registered", tool_count=6)


# ---------------------------------------------------------------------------
# count_logs(group_by=...) 的实现
# ---------------------------------------------------------------------------
async def _count_logs_grouped_report(
    *,
    backend: LogBackend,
    config: LogConfig,
    query: str,
    tenants: List[str],
    start_dt: datetime,
    end_dt: datetime,
    instance: Optional[str],
    dimension: str,
) -> str:
    """渲染 ``count_logs(group_by=...)`` 的分布画像报告。

    先尝试把分组下推到后端（精确、全量）；后端返回 ``None`` 表示这个
    维度无法下推，退化为"拉最多 :data:`_GROUP_BY_SAMPLE_CAP` 条样本在
    服务端解析"的估算，并在输出里明确标注是估算值。
    """
    push_down_label = _GROUP_BY_DIMENSIONS[dimension]

    pushed_down = False
    results: List[TenantQueryResult] = []

    if push_down_label is not None:

        async def run_grouped(
            t: str,
            cluster_errors: Dict[str, str],
            cluster_warnings: Dict[str, str],
        ) -> Optional[Dict[str, int]]:
            del cluster_warnings
            return await backend.count_logs_grouped(
                query=query,
                tenant=t,
                start=start_dt,
                end=end_dt,
                by_label=push_down_label,
                instance=instance,
                cluster_errors=cluster_errors,
            )

        results = await _fan_out(tenants, run_grouped)
        # 有任一租户成功拿到（非 None）分组结果就算下推成功；空字典也
        # 算成功（= 该窗口确实没有数据）。
        pushed_down = any(r.ok and r.data is not None for r in results)

    sample_size = 0
    if not pushed_down:
        sample_limit = min(_GROUP_BY_SAMPLE_CAP, config.max_limit)

        async def run_sample(
            t: str,
            cluster_errors: Dict[str, str],
            cluster_warnings: Dict[str, str],
        ) -> Dict[str, int]:
            entries = await backend.query_logs(
                query=query,
                tenant=t,
                start=start_dt,
                end=end_dt,
                limit=sample_limit,
                direction="backward",
                instance=instance,
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )
            counts = _group_entries_by_dimension(entries, dimension)
            # 用一个不可能与真实分组键冲突的内部键回传样本条数。
            counts["__sample_size__"] = len(entries)
            return counts

        results = await _fan_out(tenants, run_sample)

    merged: Dict[str, int] = {}
    per_tenant_lines: List[str] = []
    for r in results:
        if not r.ok:
            per_tenant_lines.append(f"- `{r.tenant}`: _error_")
            continue
        data = r.data or {}
        tenant_total = 0
        for key, count in data.items():
            if key == "__sample_size__":
                sample_size += int(count)
                continue
            merged[key] = merged.get(key, 0) + int(count)
            tenant_total += int(count)
        per_tenant_lines.append(f"- `{r.tenant}`: {tenant_total}")

    successful = [r.tenant for r in results if r.ok]
    total = sum(merged.values())

    if pushed_down:
        basis = (
            f"**Basis:** exact push-down "
            f"(`sum by ({push_down_label})` over the full window)\n"
        )
    else:
        reason = (
            "backend has no matching label"
            if push_down_label is not None
            else "template grouping cannot be pushed down"
        )
        basis = (
            f"**Basis:** ⚠️ **estimated from a {sample_size}-entry sample** "
            f"({reason}); percentages describe the sample, not the full "
            f"window. Counts are NOT exact totals — use count_logs without "
            f"group_by for an exact total.\n"
        )

    query_display = query if len(query) <= 120 else query[:117] + "..."
    table = _render_group_table(merged, dimension=dimension, total=total)
    return (
        f"# Log Count by `{dimension}`\n\n"
        f"**Query:** `{query_display}`\n"
        f"**Time Range:** "
        f"`{format_short(start_dt, config.timezone)}` to "
        f"`{format_short(end_dt, config.timezone)}`\n"
        f"**Instance:** `{instance or '*all healthy*'}`\n"
        f"**Successful Tenants:** `{', '.join(successful) or '-'}`\n"
        f"{basis}\n"
        + "\n".join(table)
        + f"\n\n**Total:** {total}\n"
        + "\n".join(["", "**Per Tenant:**", *per_tenant_lines])
        + "\n"
        + _format_failures(results)
    )


def _make_download_url(
    ctx: Optional[Context], config: LogConfig, token: str
) -> Optional[str]:
    """渲染下载路由的绝对 URL。

    下载路由挂在 **与 MCP 主端点相同的路径前缀** 下（默认
    ``/mcp/download/<token>``），任何把 ``/mcp`` 转发到本服务的反向
    代理规则都会自动覆盖下载链接。

    选择 base URL 的优先级：

    1. 配置项 ``LOG_DOWNLOAD_BASE_URL``（推荐在反向代理改写 Host 的
       场景下显式配置）；下载路径会自动拼接到末尾。
    2. 当前请求的 scheme + Host 请求头（仅 HTTP 传输有该信息）。
    3. ``None``——交由调用方退化为直接展示 token / 路径。
    """
    path = _download_url_path.rstrip("/")
    if config.download_base_url:
        return f"{config.download_base_url}{path}/{token}"

    request = None
    if ctx is not None:
        try:
            request = ctx.request_context.request
        except Exception:
            request = None
    if request is None:
        return None

    try:
        # 优先读 X-Forwarded-Proto / X-Forwarded-Host：在 TLS 终止
        # 反向代理后面，这样生成的链接才会是 ``https://...`` 而不是
        # ``http://...``（混合内容 / 重定向问题的高发场景）。这两个
        # 请求头不存在时，退化到直接读请求自身的 scheme + Host。
        fwd_proto = request.headers.get("x-forwarded-proto")
        scheme = fwd_proto.split(",")[0].strip() if fwd_proto else request.url.scheme
        fwd_host = request.headers.get("x-forwarded-host")
        host = (
            fwd_host.split(",")[0].strip() if fwd_host else request.headers.get("host")
        )
    except Exception:
        return None
    if not host:
        return None
    return f"{scheme}://{host}{path}/{token}"


# ---------------------------------------------------------------------------
# get_labels / get_label_values 共用的列表辅助函数
# ---------------------------------------------------------------------------
async def _list_keys(
    *,
    label: Optional[str],
    start: Optional[str],
    end: Optional[str],
    heading: str,
    tenant: Optional[str] = None,
    instance: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> str:
    backend, config = _require_state()

    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None
    if start or end:
        try:
            start_dt, end_dt = resolve_time_range(
                start, end, config.default_time_range_minutes
            )
        except ValidationError as e:
            raise RuntimeError(str(e)) from e

    tenants = _resolve_tenants(backend, config, tenant, ctx)
    logger.info(
        "Tool: list keys",
        kind="label_values" if label else "labels",
        tenants=tenants,
        label=label,
        start=start_dt.isoformat() if start_dt else None,
        end=end_dt.isoformat() if end_dt else None,
    )

    async def run_for_tenant(
        tenant: str,
        cluster_errors: Dict[str, str],
        cluster_warnings: Dict[str, str],
    ) -> List[str]:
        del cluster_warnings
        if label is None:
            return await backend.get_labels(
                tenant,
                start=start_dt,
                end=end_dt,
                instance=instance,
                cluster_errors=cluster_errors,
            )
        return await backend.get_label_values(
            tenant,
            label,
            start=start_dt,
            end=end_dt,
            instance=instance,
            cluster_errors=cluster_errors,
        )

    results = await _fan_out(tenants, run_for_tenant)
    successful = [r.tenant for r in results if r.ok]

    parts = [
        f"# {heading}\n",
        f"**Backend:** `{backend.name}`",
        f"**Tenants Queried:** `{', '.join(tenants)}`",
        f"**Successful Tenants:** `{', '.join(successful) or '-'}`",
        f"**Instance:** `{instance or '*all healthy*'}`",
    ]
    if start_dt and end_dt:
        parts.append(
            f"**Time Range:** `{format_in_tz(start_dt, config.timezone)}` to "
            f"`{format_in_tz(end_dt, config.timezone)}`"
        )
    parts.append("")

    ok_results = [r for r in results if r.ok]
    error_results = [r for r in results if not r.ok]
    multi_tenant = len(tenants) > 1

    # 收集每个 value 出现在哪些租户里，以便多租户时合并去重。
    tenants_by_value: Dict[str, List[str]] = {}
    unique: set[str] = set()
    for r in ok_results:
        for name in r.data or []:
            unique.add(name)
            tenants_by_value.setdefault(name, [])
            if r.tenant not in tenants_by_value[name]:
                tenants_by_value[name].append(r.tenant)

    if not multi_tenant:
        # 单租户：按租户分节的简洁输出。
        for r in results:
            parts.append(f"## Tenant: `{r.tenant}`")
            if not r.ok:
                parts.append(f"_Error: {r.error}_\n")
                continue
            items = r.data or []
            if not items:
                parts.append("_No values found_\n" if label else "_No labels found_\n")
            else:
                parts.append(f"Found {len(items)} item(s):\n")
                for i, name in enumerate(items, 1):
                    parts.append(f"{i}. `{name}`")
                parts.append("")
    else:
        # 多租户：同一 value 只出现一次，附上归属租户列表，避免按租户
        # 分节重复刷屏。
        if unique:
            parts.append(f"Found {len(unique)} unique item(s):\n")
            for i, name in enumerate(sorted(unique), 1):
                owners = tenants_by_value.get(name) or []
                if len(owners) == 1:
                    parts.append(f"{i}. `{name}` (tenant: {owners[0]})")
                else:
                    parts.append(
                        f"{i}. `{name}` (tenants: {', '.join(sorted(owners))})"
                    )
            parts.append("")
        else:
            parts.append("_No values found_\n" if label else "_No labels found_\n")
        for r in error_results:
            parts.append(f"## Tenant: `{r.tenant}`")
            parts.append(f"_Error: {r.error}_\n")

    parts.append(f"**Total Unique:** {len(unique)}")
    cluster_errors_md = _format_failures(results)
    return "\n".join(parts) + "\n" + cluster_errors_md
