"""日志下载支持：文件写入器 + 令牌注册表 + HTTP 路由。

:class:`DownloadRegistry` 是唯一一份共享状态——它持有"短随机 URL 令牌
→ 本地文件路径 + 过期时间"的映射。HTTP 下载路由（streamable-http
模式下由 ``main.py`` 挂载）从中读取；``download_logs`` 工具向其写入。

stdio 模式下不会暴露 HTTP 路由，但工具仍然走相同流程（行为统一），
只是把绝对路径直接返回给客户端。
"""

from .registry import DownloadEntry, DownloadRegistry
from .writer import (
    SUPPORTED_FORMATS,
    DownloadResult,
    write_download,
)

__all__ = [
    "DownloadEntry",
    "DownloadRegistry",
    "DownloadResult",
    "SUPPORTED_FORMATS",
    "write_download",
]
