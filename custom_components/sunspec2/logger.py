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


class SunSpecLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    """Bind host, port, unit_id (and optional model_id) to every record."""

    @property
    def bound(self) -> Mapping[str, Any]:
        """The device context this adapter was built with.

        ``logging.LoggerAdapter`` declares ``extra`` as optional because
        the base class accepts None. Every adapter :func:`get_adapter`
        hands out carries a dict, so read the context through here and
        the caller does not have to guard against a None that cannot
        occur.

        Returns:
            Mapping[str, Any]: The bound keys, at minimum ``host``,
                ``port`` and ``unit_id``.
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


# What a module logs through. Normal operation hands these helpers the
# context-binding adapter; a caller with no device context (and a test)
# hands them a plain logger. logging.LoggerAdapter is not a Logger
# subclass, so the two only travel together as a union.
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
