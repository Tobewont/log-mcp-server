"""后端工厂。

按 ``LogConfig`` 选择合适的具体 ``LogBackend`` 实现。Loki 后端下，
``LOKI_ADDR`` 可以是用 ``|`` 分隔的多个地址，此时会被透明地封装为
``FanoutBackend``，并配套一个 ``HealthCache``。
"""
from __future__ import annotations

from typing import Optional

from ..config import LogConfig
from ..utils.errors import ConfigError
from .base import LogBackend
from .fanout import FanoutBackend
from .health_cache import HealthCache


def create_backend(config: LogConfig) -> tuple[LogBackend, Optional[HealthCache]]:
    """构造启用的后端实例，以及可选的 ``HealthCache``。

    返回值 ``(backend, health_cache)``。单集群部署不需要扇出，
    ``health_cache`` 为 ``None``。
    """
    if config.backend == "loki":
        from .loki import LokiBackend

        addrs = config.get_loki_addrs()
        if not addrs:
            raise ConfigError("LOKI_ADDR must be set")
        if len(addrs) == 1:
            return LokiBackend(config, addr=addrs[0]), None

        sub_backends = [LokiBackend(config, addr=a) for a in addrs]
        cache = HealthCache(
            sub_backends,
            interval=config.health_check_interval,
            probe_timeout=config.health_check_timeout,
        )
        return FanoutBackend(sub_backends, health_cache=cache), cache

    raise ConfigError(f"Unsupported backend: {config.backend!r}")
