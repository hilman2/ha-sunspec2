"""Typed exception hierarchy for the SunSpec 2 integration.

Phase 3 splits the bare ``except Exception`` catch in the coordinator into
four categories so that:

- the Diagnostics dump can group errors by kind,
- the Repairs panel only fires for actionable, persistent failures,
- retry / backoff decisions can depend on the category.

The mapping from pysunspec2's exception classes to ours lives at the
``api.py`` boundary; the coordinator only ever sees the categorised types.
"""

from __future__ import annotations

from typing import ClassVar


class SunSpecError(Exception):
    """Base class for all SunSpec 2 integration errors."""

    category: ClassVar[str] = "unknown"


class TransportError(SunSpecError):
    """TCP socket, Modbus framing, CRC, connection refused.

    Something between us and the inverter is physically broken. Repairs
    panel issue is created after three consecutive failures of this kind.
    """

    category = "transport"


class ProtocolError(SunSpecError):
    """No SunSpec base address, scan terminated, unknown model layout.

    The device is reachable but does not speak SunSpec correctly. Repairs
    panel issue is created on the first occurrence because this is always
    a configuration or hardware-incompatibility problem, never a
    transient state.
    """

    category = "protocol"


class DeviceError(SunSpecError):
    """Modbus exception code from the device, or implausible point value.

    Device responded but the response is wrong (out-of-range, exception
    code, OverflowError on a calculated value). Repairs panel issue is
    created after three consecutive failures of this kind.
    """

    category = "device"


class TransientError(SunSpecError):
    """One-shot timeout that should retry with backoff.

    Never escalates to a Repairs issue. Recorded in the diagnostics
    buffer for forensic value only.
    """

    category = "transient"


CATEGORIES: tuple[str, ...] = ("transport", "protocol", "device", "transient")
"""All Phase-3 error categories, in declaration order.

Single source of truth - import from here whenever you need to iterate
over categories (coordinator init, diagnostics dump, tests).
"""
