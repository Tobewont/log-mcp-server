"""请求级客户端租户过滤的小工具。

MCP 客户端告诉服务端"本会话允许访问哪些租户"，目前有两种等价的途径：

* HTTP 传输（streamable-http / SSE）：通过 ``X-Allowed-Tenants``
  请求头（逗号分隔）。streamable-http server 会把底层 Starlette
  ``Request`` 经由 ``ServerMessageMetadata`` 透传给每次工具调用，
  因此工具直接从 ``ctx.request_context.request`` 读取请求头即可。
  （这条路径能跨过 worker 任务边界，而 ASGI middleware + contextvar
  的做法跨不过去。）

* stdio 传输：通过服务端进程的 ``LOKI_CLIENT_TENANTS`` 环境变量
  （由 MCP 客户端配置注入，例如 ``mcp.json`` 的 ``env`` 块）。stdio
  没有 Starlette ``Request``，因此回退到进程级配置。

两种方式的值都必须是服务端 ``LOKI_TENANTS`` 的 **逗号分隔子集**。
未声明范围时，日志查询/下载工具会直接拒绝执行；``health_check``
不受此限制，仍然可用，方便排查"为什么查不到"。

这是过滤，不是身份认证：能修改 MCP 客户端配置的人也能改这个
请求头 / 环境变量，所以它的目的是防止 AI / 用户误操作，而不是承担
安全边界。
"""
from __future__ import annotations

from typing import List, Optional


def parse_tenant_list(value: Optional[str]) -> Optional[List[str]]:
    """解析逗号分隔的租户列表，忽略空白与空 token。

    遇到 ``None``、空字符串、纯空白字符串或全是空 token 的输入
    （例如 ``",,,"``）时统一返回 ``None``，表示"未声明"，
    而不是"禁止所有"——畸形或缺失的请求头不应该把请求悄悄打断。
    """
    if value is None:
        return None
    items = [t.strip() for t in value.split(",") if t.strip()]
    if not items:
        return None
    return items
