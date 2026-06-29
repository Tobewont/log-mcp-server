#!/usr/bin/env python3
"""log-mcp-server 程序入口。

后端（及其底层的 ``httpx.AsyncClient``）在 FastMCP 自己的事件循环内通过
``lifespan`` 上下文打开和关闭。这样可以避免跨事件循环复用 HTTP client
（绑定到一个已经关闭的 loop 上）的隐患。

在 streamable-http / SSE 模式下还会额外挂载一条非 MCP 的 HTTP 路由
``GET /<MCP前缀>/download/<token>``，由 ``download_logs`` 工具写文件、
该路由对外提供下载，客户端可以直接把日志拉到自己的机器上，而不用把
内容塞进 LLM 上下文消耗 token。
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from mcp.server.fastmcp import FastMCP

from .backends.factory import create_backend
from .config import LogConfig
from .downloads import DownloadRegistry
from .tools import initialize_tools, register_tools
from .utils.logging import setup_logging

logger = setup_logging(__name__)


def _build_lifespan(
    config: LogConfig,
    registry: Optional[DownloadRegistry],
    download_url_path: str,
):
    """构造一个 FastMCP lifespan，负责启动/关闭后端。"""

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        backend, health_cache = create_backend(config)
        logger.info(
            "Opening backend",
            backend=backend.name,
            tenants=backend.tenants,
            health_cache="enabled" if health_cache else "disabled",
            download_registry="enabled" if registry else "disabled",
            download_url_path=download_url_path,
        )
        async with backend:
            if health_cache is not None:
                await health_cache.start()
            if registry is not None:
                # 清理上次运行残留的过期文件。
                await registry.cleanup_expired()
            initialize_tools(
                backend,
                config,
                download_registry=registry,
                download_url_path=download_url_path,
            )
            logger.info("Backend ready", backend=backend.name)
            try:
                yield
            finally:
                if health_cache is not None:
                    await health_cache.stop()
                logger.info("Closing backend", backend=backend.name)

    return lifespan


def _build_server(
    config: LogConfig,
    registry: Optional[DownloadRegistry] = None,
    download_url_path: str = "/mcp/download",
    *,
    streamable_http_path: str = "/mcp",
) -> FastMCP:
    is_debug = config.log_level == "DEBUG"
    mcp = FastMCP(
        name="log-mcp-server",
        instructions=(
            "A log MCP server with multi-tenant and multi-cluster support. "
            "Unhealthy clusters are automatically skipped. "
            "Recommended workflow for multi-tenant: "
            "1) Call get_labels() to discover available label names per tenant. "
            "2) Call get_label_values(label='<relevant label>') to find which "
            "tenant owns the target value. "
            "3) Call query_logs(tenant='<id>', query='...') to query only "
            "that tenant. "
            "Labels are user-defined (e.g. app, job, env, namespace, etc.). "
            "This avoids slow fan-out across all tenants and yields fast, "
            "precise results. "
            "If the user explicitly names a Loki instance (e.g. "
            "'loki.example.com' or 'loki:3100'), pass it as the optional "
            "'instance' argument so the query is restricted to that single "
            "cluster instead of fanning out. "
            "IMPORTANT: log-query tools (query_logs, get_labels, "
            "get_label_values, download_logs) require the MCP client to declare an "
            "explicit tenant subset, either via the X-Allowed-Tenants "
            "request header (HTTP) or the LOKI_CLIENT_TENANTS env var "
            "(stdio). When this is unset the tools refuse to run with a "
            "clear error — call health_check first to inspect the active "
            "scope; if it shows '(unset)', tell the user to add a tenant "
            "list to their MCP client config and stop. Never try a tenant "
            "outside the allowed subset; it will be rejected as Forbidden. "
            "Performance: prefer querying a single tenant via 'tenant=' "
            "whenever possible; the more tenants in 'Allowed Tenants', "
            "the slower and noisier the default fan-out becomes — if you "
            "see 3+ tenants and notice unrelated results / errors, "
            "advise the user to narrow their client config. "
            "When the user asks to 'download', 'export', 'save to file', "
            "'拉到本地' / '下载日志' or otherwise wants the raw logs "
            "outside the chat, call download_logs (not query_logs). "
            "If no entries match, it returns an empty-result message "
            "and creates no file. Otherwise it writes the logs to a file "
            "and returns either an absolute path (stdio) or a one-shot "
            "download URL (streamable-http / sse) — surface that exactly "
            "as-is to the user so they can fetch it on their own machine; "
            "do NOT paste the log contents back into the conversation."
        ),
        debug=is_debug,
        log_level=config.log_level,
        host=config.mcp_host,
        port=config.mcp_port,
        streamable_http_path=streamable_http_path,
        lifespan=_build_lifespan(config, registry, download_url_path),
    )
    register_tools(mcp)
    return mcp


def _select_transport_from_argv(argv: list[str]) -> Optional[str]:
    """可选的 CLI 传输方式覆盖。

    接受位置参数 ``stdio`` / ``sse`` / ``streamable-http``。返回
    ``None`` 表示回退到配置。
    """
    for arg in argv[1:]:
        if arg in ("stdio", "sse", "streamable-http"):
            return arg
    return None


def cli_main() -> None:
    """命令行入口。

    传输方式选择优先级（从高到低）：

    1. CLI 位置参数（``stdio`` / ``sse`` / ``streamable-http``）。
    2. ``MCP_TRANSPORT`` 环境变量 / 配置字段（默认 ``stdio``）。
    """
    try:
        config = LogConfig()
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to load configuration", error=str(e), exc_info=True)
        sys.exit(1)

    logger.info(
        "Configuration loaded",
        backend=config.backend,
        addrs=config.get_loki_addrs(),
        tenants=config.get_tenant_list(),
        mcp_transport=config.mcp_transport,
        mcp_host=config.mcp_host,
        mcp_port=config.mcp_port,
        log_level=config.log_level,
    )

    transport = _select_transport_from_argv(sys.argv) or config.mcp_transport

    # 只有 HTTP 类传输需要下载注册表 / HTTP 路由。stdio 模式直接把
    # 文件写到用户机器上，不需要 URL。
    registry: Optional[DownloadRegistry] = None
    download_url_path = "/mcp/download"
    streamable_http_path = config.mcp_path
    if transport in ("streamable-http", "sse"):
        registry = DownloadRegistry(ttl_seconds=config.download_ttl_seconds)
        # 下载路由挂在 MCP 主端点同前缀下：streamable-http 跟随
        # ``MCP_PATH``（默认 ``/mcp``），SSE 仍使用 FastMCP 默认的
        # ``/sse``，保持与旧行为一致。
        if transport == "streamable-http":
            download_url_path = f"{config.mcp_path.rstrip('/')}/download"
        else:
            streamable_http_path = "/mcp"
            download_url_path = "/sse/download"

    mcp = _build_server(
        config,
        registry=registry,
        download_url_path=download_url_path,
        streamable_http_path=streamable_http_path,
    )

    logger.info(
        "Starting FastMCP",
        transport=transport,
        download_url_path=download_url_path if registry else None,
    )
    try:
        if transport in ("streamable-http", "sse"):
            assert registry is not None
            _run_http(
                mcp, config, registry, transport, download_url_path
            )
        else:
            mcp.run(transport=transport)
    except KeyboardInterrupt:
        logger.info("Server interrupted by user")
    except Exception as e:  # noqa: BLE001
        logger.error("Server error", error=str(e), exc_info=True)
        raise


def _run_http(
    mcp: FastMCP,
    config: LogConfig,
    registry: DownloadRegistry,
    transport: str,
    download_url_path: str,
) -> None:
    """启动 FastMCP 的 HTTP 应用，并额外挂载 ``/download/<token>`` 路由。

    不能直接调用 ``mcp.run("streamable-http")``，因为那会启动它自己的
    uvicorn，且不留任何挂载非 MCP 路由的钩子。这里改为向 FastMCP 取
    底层 Starlette 应用，然后直接给它注册下载路由。MCP lifespan 仍然
    会正常执行（我们没有再包一层父应用，只是多注册了一条路由），所以
    后端 / 健康缓存 / 工具初始化都和原来一样。
    """
    import anyio
    import uvicorn
    from starlette.background import BackgroundTask
    from starlette.responses import FileResponse, PlainTextResponse
    from starlette.routing import Route

    if transport == "streamable-http":
        app = mcp.streamable_http_app()
    else:
        app = mcp.sse_app()

    async def download_route(request):  # noqa: ANN001 — Starlette handler
        token = request.path_params["token"]
        entry = await registry.get(token)
        if entry is None:
            return PlainTextResponse(
                "Download token not found or expired.", status_code=404
            )
        if not entry.path.exists():
            await registry.discard(token)
            return PlainTextResponse(
                "Download file is missing on the server.", status_code=410
            )
        return FileResponse(
            path=str(entry.path),
            media_type=entry.media_type,
            filename=entry.download_filename,
            background=BackgroundTask(registry.discard, token),
        )

    # 把下载路由插到最前面，确保它优先于 MCP 的 catch-all 路由命中。
    # Starlette 是 first-match 路由策略。
    #
    # 路由挂在 **和 MCP 同样的前缀** 下（默认 ``/mcp``）。这是有意为之：
    # 已经把 ``/mcp`` 转发到本服务的反向代理 / Ingress 规则会自动覆盖
    # 下载路由，无需再加一条规则。如果换成同级前缀（``/download``），
    # 运维就得给每个集群再加一条规则，否则反向代理后会神秘地 404。
    route_path = f"{download_url_path.rstrip('/')}/{{token:str}}"
    app.router.routes.insert(
        0,
        Route(route_path, download_route, methods=["GET"]),
    )
    logger.info(
        "Mounted download route", path=route_path, transport=transport
    )

    uvicorn_config = uvicorn.Config(
        app,
        host=config.mcp_host,
        port=config.mcp_port,
        log_level=config.log_level.lower(),
    )
    server = uvicorn.Server(uvicorn_config)
    anyio.run(server.serve)


if __name__ == "__main__":
    cli_main()
