"""带 TTL 自动清理的"令牌 → 文件"映射表。

:class:`DownloadRegistry` 是一份很小的内存账本，承担两件事：

1. **为 streamable-http 的 ``/download/<token>`` 路由生成不可猜测的
   URL 令牌**。整个授权路径上，"知道令牌"是文件与网络之间唯一的护栏，
   因此必须用密码学安全随机数生成（这里用 :func:`secrets.token_urlsafe`）。
2. **回收过期文件**，避免服务端积累陈旧下载。注册新条目时顺便清理一次
   （便宜、不需要后台任务），启动时也会调用一次。

注册表刻意只在本进程内有效，多副本间不互通——如果请求打到副本 B 而
令牌是副本 A 发的，下载会 404，让用户重试即可。要让多副本共享令牌
就得引入外部存储（Redis / DB），对一个 MCP 特性而言过度设计。

线程 / 事件循环安全：所有变更操作都在 :class:`asyncio.Lock` 保护下
进行。注册表在 FastMCP lifespan 内创建，每个服务进程仅一个实例。
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DownloadEntry:
    """已注册下载的不可变快照。"""

    token: str
    path: Path
    """文件在服务端文件系统上的绝对路径。"""

    media_type: str
    """例如 ``application/x-ndjson``。"""

    download_filename: str
    """建议给 HTTP ``Content-Disposition`` 用的文件名。"""

    expires_at: float
    """Unix 时间戳（秒）。"""


_FORMAT_MEDIA_TYPE: Dict[str, str] = {
    "jsonl": "application/x-ndjson",
    "csv": "text/csv; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}


def media_type_for(fmt: str) -> str:
    """返回某下载格式对应的标准 MIME 类型。"""
    return _FORMAT_MEDIA_TYPE.get(fmt, "application/octet-stream")


class DownloadRegistry:
    """内存中的"令牌 → 文件"注册表，带 TTL 自动驱逐。"""

    def __init__(self, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._items: Dict[str, DownloadEntry] = {}
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    async def register(
        self,
        *,
        path: Path,
        fmt: str,
        download_filename: str,
    ) -> DownloadEntry:
        """登记一个刚刚写好的文件，返回对应的条目。

        令牌是 URL 安全随机串，约 256 bit 熵。注册时顺便机会性地
        清理一次过期条目。
        """
        token = secrets.token_urlsafe(32)
        entry = DownloadEntry(
            token=token,
            path=path.resolve(),
            media_type=media_type_for(fmt),
            download_filename=download_filename,
            expires_at=time.time() + self._ttl,
        )
        async with self._lock:
            self._items[token] = entry
            self._evict_expired_locked()
        logger.debug(
            "Registered download",
            token_prefix=token[:8],
            path=str(entry.path),
            expires_in=self._ttl,
        )
        return entry

    async def get(self, token: str) -> Optional[DownloadEntry]:
        """按令牌查找条目。若已过期或不存在，返回 ``None``。"""
        async with self._lock:
            entry = self._items.get(token)
            if entry is None:
                return None
            if entry.expires_at < time.time():
                self._items.pop(token, None)
                self._unlink_quietly(entry.path)
                return None
            return entry

    async def discard(self, token: str) -> None:
        """丢弃条目并删除对应文件（单次使用语义）。"""
        async with self._lock:
            entry = self._items.pop(token, None)
        if entry is not None:
            self._unlink_quietly(entry.path)

    async def cleanup_expired(self) -> int:
        """清理所有过期条目并删除对应文件，返回被清理的数量。"""
        async with self._lock:
            return self._evict_expired_locked()

    # ------------------------------------------------------------------
    # 内部辅助方法（调用前必须持有 ``self._lock``）
    # ------------------------------------------------------------------
    def _evict_expired_locked(self) -> int:
        now = time.time()
        expired = [t for t, e in self._items.items() if e.expires_at < now]
        for token in expired:
            entry = self._items.pop(token, None)
            if entry is not None:
                self._unlink_quietly(entry.path)
        return len(expired)

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover — best-effort
            logger.warning(
                "Failed to unlink download file",
                path=str(path),
                error=str(exc),
            )
