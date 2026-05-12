"""Backend factory.

Picks the appropriate concrete ``LogBackend`` for the given ``LogConfig``.
For the Loki backend, ``LOKI_ADDR`` may contain multiple pipe-separated
addresses, in which case we transparently wrap them in a
``FanoutBackend`` with an associated ``HealthCache``.
"""
from __future__ import annotations

from typing import Optional

from ..config import LogConfig
from ..utils.errors import ConfigError
from .base import LogBackend
from .fanout import FanoutBackend
from .health_cache import HealthCache


def create_backend(config: LogConfig) -> tuple[LogBackend, Optional[HealthCache]]:
    """Create the active backend and an optional ``HealthCache``.

    Returns ``(backend, health_cache)``.  ``health_cache`` is ``None``
    for single-cluster deployments (no fan-out needed).
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
