"""Typed exception hierarchy for the SunSpec 2 integration.

Every failure of the read or write path falls into one of four
categories, so that:

- the Diagnostics dump can group errors by kind,
- the Repairs panel only fires for actionable, persistent failures,
- retry decisions can depend on the category.

The mapping from pysunspec2's exception classes to these classes lives
at the ``api.py`` boundary. The Repairs threshold of each category is in
``SunSpecDataUpdateCoordinator._record_error``.
"""

from __future__ import annotations

from typing import ClassVar

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN


class SunSpecError(Exception):
    """Base class for all SunSpec 2 integration errors."""

    category: ClassVar[str] = "unknown"


class TransportError(SunSpecError):
    """Connection refused or closed, Modbus framing, CRC.

    Something between Home Assistant and the inverter is broken.
    """

    category = "transport"


class ProtocolError(SunSpecError):
    """No SunSpec base address, scan terminated, unknown model layout.

    The device is reachable but does not speak SunSpec correctly. That
    is a configuration or hardware-incompatibility problem, never a
    transient state.
    """

    category = "protocol"


class DeviceError(SunSpecError):
    """Modbus exception code from the device, or implausible point value.

    The device responded but the response is wrong (out-of-range,
    exception code, OverflowError on a calculated value).
    """

    category = "device"


class TransientError(SunSpecError):
    """A failure expected to pass by the next attempt, such as a read timeout.

    It never raises a Repairs issue and is recorded in the diagnostics
    buffer only.
    """

    category = "transient"


CATEGORIES: tuple[str, ...] = ("transport", "protocol", "device", "transient")
"""All Phase-3 error categories, in declaration order.

Single source of truth - import from here whenever you need to iterate
over categories (coordinator init, diagnostics dump, tests).
"""


def user_error(key: str, **placeholders: object) -> HomeAssistantError:
    """An error for the person who triggered an action, in their language.

    Args:
        key: The message, as ``exceptions.<key>`` in translations/*.json.
        placeholders: The values for the ``{name}`` fields of that message.

    Returns:
        The error to raise. The frontend shows it in the user's
        language, the log in English.
    """
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={name: str(value) for name, value in placeholders.items()},
    )
