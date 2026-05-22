"""把日志条目序列化为可下载文件。

支持三种格式：

* ``jsonl`` —— 每行一个 JSON 对象，最忠实（标签作为嵌套对象保留），
  也最方便用 ``jq`` 等工具后处理。

  ``line`` 字段会做 **智能解析**：当原始日志行本身就是 JSON 对象/数组
  时，会把它作为嵌套结构嵌入，而不是保留成不透明字符串。这样可以
  避免 JSON-in-JSON 渲染成字符串后产生的多级转义
  （``\\\\"foo\\\\"``），人读不友好，机读还得 ``jq fromjson``。
  不能解析为 JSON 的纯文本日志行原样保留为字符串。

* ``csv``  —— 兼容 RFC 4180。列名：``time, tenant, cluster, labels,
  line``。``labels`` 列被渲染为 JSON 字符串，避免标签内的逗号撑破行。
* ``txt``  —— 单行可读格式：``[time] {tenant}/{cluster} {labels} line``

写入器刻意保持同步 + 纯 Python，不引入第三方依赖、不引入异步 I/O。
它在 ``download_logs`` 工具把数据完整读到内存之后才被调用，对预期
规模（≤ ``LOG_MAX_LIMIT`` 条）来说，短暂阻塞事件循环写文件是可
接受的。
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from ..backends.base import LogEntry
from ..utils.time_utils import format_in_tz

SUPPORTED_FORMATS: tuple[str, ...] = ("jsonl", "csv", "txt")


@dataclass(frozen=True)
class DownloadResult:
    """写文件后的结果信息。"""

    path: Path
    """文件在服务端文件系统上的绝对路径。"""

    entry_count: int
    """已写入的日志条数。"""

    byte_size: int
    """写入后文件的字节大小。"""

    fmt: str
    """格式名，取值为 :data:`SUPPORTED_FORMATS` 之一。"""


# 文件名清洗：保守地只保留 ``A-Za-z0-9._-``，其余字符统一折叠为 ``_``，
# 再去掉首尾的 ``._-``，避免出现危险路径片段。
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitise_segment(text: str, *, fallback: str = "x") -> str:
    cleaned = _UNSAFE.sub("_", text).strip("._-")
    return cleaned or fallback


def build_filename(
    *,
    tenant_label: str,
    fmt: str,
    now: datetime,
    suffix: Optional[str] = None,
) -> str:
    """构造一个文件系统安全且不易冲突的文件名。

    格式：``logs-<UTC时间戳>-<tenant>[-<suffix>].<fmt>``。

    Args:
        suffix: 可选附加段（例如随机 hex），用来防止同一秒内并发的两
            次下载在磁盘上互相覆盖。和 ``tenant_label`` 用同样的清洗
            规则。HTTP ``Content-Disposition`` 里展示给用户的名字
            (干净版) 应当传 ``None``。
    """
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported format: {fmt!r}")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    safe_tenant = _sanitise_segment(tenant_label, fallback="all")
    if suffix:
        safe_suffix = _sanitise_segment(suffix, fallback="x")
        return f"logs-{stamp}-{safe_tenant}-{safe_suffix}.{fmt}"
    return f"logs-{stamp}-{safe_tenant}.{fmt}"


def write_download(
    entries: Iterable[LogEntry],
    *,
    target_path: Path,
    fmt: str,
    timezone: str,
) -> DownloadResult:
    """将 ``entries`` 按 ``fmt`` 序列化写入 ``target_path``。

    Args:
        entries: 已经从后端拉取好的日志条目。会被消费一次。
        target_path: 绝对路径，父目录需提前存在。
        fmt: :data:`SUPPORTED_FORMATS` 之一。
        timezone: 用于渲染时间列的 IANA 时区名。底层 ``datetime``
            始终带 UTC 时区信息。

    Returns:
        :class:`DownloadResult`，描述刚生成的文件。
    """
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported format: {fmt!r}")

    # 提前物化一次，这样即便入参是 generator 也能给出准确的
    # entry_count；``LOG_MAX_LIMIT`` 已经为这个列表设了上限，
    # 内存占用是有界的。
    entry_list: List[LogEntry] = list(entries)

    if fmt == "jsonl":
        _write_jsonl(entry_list, target_path, timezone)
    elif fmt == "csv":
        _write_csv(entry_list, target_path, timezone)
    elif fmt == "txt":
        _write_txt(entry_list, target_path, timezone)
    else:  # pragma: no cover — guarded above
        raise ValueError(fmt)

    return DownloadResult(
        path=target_path.resolve(),
        entry_count=len(entry_list),
        byte_size=target_path.stat().st_size,
        fmt=fmt,
    )


# ---------------------------------------------------------------------------
# 各格式的写入器
# ---------------------------------------------------------------------------
def _maybe_parse_json(line: str):
    """对日志行做尽力而为的 JSON 解析，让 jsonl 输出保持可读。

    很多应用日志（结构化日志框架、``logfmt``→JSON 的 sidecar 等）每行
    本身就是一个 JSON 对象。如果我们再把它当作 **字符串** 包裹在自己的
    JSON 信封里，每一个内部引号都要被再转义一次，就会产生臭名昭著的
    ``\\\\\"`` 转义链——人读不懂、机读还得 ``jq fromjson``。

    当某一行能干净地解析成 JSON 对象或数组时，我们直接把解析后的值
    嵌进去，省掉一层转义。纯文本行（``"GET /index.html 200"`` 之类）
    则原样保留。

    返回值：解析出的对象/数组，或原始字符串。
    """
    if not isinstance(line, str):
        return line
    s = line.strip()
    if not s or s[0] not in "{[":
        return line
    try:
        value = json.loads(s)
    except (ValueError, TypeError):
        return line
    if isinstance(value, (dict, list)):
        return value
    return line


def _write_jsonl(
    entries: List[LogEntry], path: Path, timezone: str
) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            row = {
                "time": format_in_tz(e.timestamp, timezone),
                "tenant": e.tenant,
                "cluster": e.cluster,
                "labels": e.labels,
                "line": _maybe_parse_json(e.line),
            }
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")


def _write_csv(
    entries: List[LogEntry], path: Path, timezone: str
) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["time", "tenant", "cluster", "labels", "line"])
        for e in entries:
            writer.writerow(
                [
                    format_in_tz(e.timestamp, timezone),
                    e.tenant or "",
                    e.cluster or "",
                    json.dumps(e.labels, ensure_ascii=False, sort_keys=True),
                    e.line,
                ]
            )


def _write_txt(
    entries: List[LogEntry], path: Path, timezone: str
) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            label_str = ", ".join(
                f"{k}={v}" for k, v in sorted(e.labels.items())
            )
            origin = e.tenant or "-"
            if e.cluster:
                origin = f"{origin}/{e.cluster}"
            f.write(
                f"[{format_in_tz(e.timestamp, timezone)}] "
                f"{origin} {{{label_str}}} {e.line}\n"
            )
