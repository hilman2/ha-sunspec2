"""Context-binding logger adapter for the SunSpec 2 integration.

Every log record produced via this adapter gets a prefix of the form
``[host:port#unit_id]`` (and ``[host:port#unit_id m=<model>]`` when a
model id is bound), so multi-device installs can be triaged from a
single log stream.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from collections.abc import MutableMapping
from typing import Any

from .const import CONF_FRONIUS_WEB_TOKEN


class SunSpecLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    """Bind host, port, unit_id (and optional model_id) to every record."""

    @property
    def bound(self) -> Mapping[str, Any]:
        """The device context this adapter was built with.

        At minimum ``host``, ``port`` and ``unit_id``. Read the context
        through this property rather than through ``extra``:
        ``logging.LoggerAdapter`` types ``extra`` as optional, while
        every adapter :func:`get_adapter` hands out carries a dict.
        """
        return self.extra or {}

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        bound = self.bound
        host = bound.get("host", "?")
        port = bound.get("port", "?")
        unit_id = bound.get("unit_id", "?")
        model_id = bound.get("model_id")
        prefix = f"[{host}:{port}#{unit_id}"
        if model_id is not None:
            prefix += f" m={model_id}"
        prefix += "]"
        return f"{prefix} {msg}", kwargs


# What a module logs through: the context-binding adapter in normal
# operation, a plain logger for a caller without device context and in
# tests. logging.LoggerAdapter is not a Logger subclass, so the two
# travel together only as a union.
type LoggerLike = logging.Logger | SunSpecLoggerAdapter


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


def redact_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The options of a config entry in a form that is safe to log.

    The stored Fronius web token is the Digest secret of the customer
    login. Whoever has it can sign in to the inverter's web interface
    without knowing the password, so it must not reach a log file.

    Args:
        options: The options of a config entry, stored or about to be.

    Returns:
        A copy with the web token replaced by ``"***"``.
    """
    return {
        key: "***" if key == CONF_FRONIUS_WEB_TOKEN else value for key, value in options.items()
    }
