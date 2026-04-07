"""Context-binding logger adapter for the SunSpec 2 integration.

Every log record produced via this adapter gets a prefix of the form
``[host:port#unit_id]`` (and ``[host:port#unit_id m=<model>]`` when a
model id is bound), so multi-device installs can be triaged from a
single log stream.
"""

from __future__ import annotations

import logging
from typing import Any


class SunSpecLoggerAdapter(logging.LoggerAdapter):
    """Bind host, port, unit_id (and optional model_id) to every record."""

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        host = self.extra.get("host", "?")
        port = self.extra.get("port", "?")
        unit_id = self.extra.get("unit_id", "?")
        model_id = self.extra.get("model_id")
        prefix = f"[{host}:{port}#{unit_id}"
        if model_id is not None:
            prefix += f" m={model_id}"
        prefix += "]"
        return f"{prefix} {msg}", kwargs


def get_adapter(
    host: str,
    port: int,
    unit_id: int,
    model_id: int | None = None,
) -> SunSpecLoggerAdapter:
    """Create an adapter bound to the given device coordinates.

    The underlying logger is always ``custom_components.sunspec2`` so that
    HA's per-integration log level filter works without surprises.
    """
    base = logging.getLogger("custom_components.sunspec2")
    extra: dict[str, Any] = {"host": host, "port": port, "unit_id": unit_id}
    if model_id is not None:
        extra["model_id"] = model_id
    return SunSpecLoggerAdapter(base, extra)
