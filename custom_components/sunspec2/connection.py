"""The Modbus link of one api client: units on Home Assistant's shared connection.

Home Assistant's ``modbus`` integration hands out ``ModbusUnit`` objects
over one connection per physical endpoint (``async_get_unit``). Two
config entries behind the same gateway, or a second integration talking
to the same inverter, get units on the same TCP session, and their
requests queue behind one lock instead of fighting for a slot. The
connection is opened by the first request and closed when the last
config entry holding a unit on it unloads.

``SharedConnection`` is the shape the embedded pysunspec2 device
(``pysunspec2/modbus/unit_device.py``) was written against, a
``ModbusConnection`` with ``for_unit``, ``connect``, ``disconnect``,
``close`` and ``connected``, so the fork needs no change. What differs
is who owns the link: a unit has no ``connect`` or ``close`` of its own,
so ``connect`` defers to the first register read and ``close`` hands the holds back
only where nobody else will.
"""

from __future__ import annotations

import contextlib

from homeassistant.components.modbus.connection import ModbusParams
from homeassistant.components.modbus.connection import async_get_temporary_unit
from homeassistant.components.modbus.connection import async_get_unit
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from modbus_connection import ModbusUnit

#: Per endpoint, the timeout each holder asked the link for, keyed by the
#: config entry and unit id, until it withdraws it. A raise above all the
#: other holders is the only one that needs a reconnect. Keyed by entry
#: rather than by object: a setup that fails with ConfigEntryNotReady never
#: unloads, so its connection never withdraws, and the next attempt must
#: replace that record instead of being held back by it.
_ASKED_TIMEOUTS: dict[tuple[str, str, int] | tuple[str, str], dict[tuple[str, int], float]] = {}


class SharedConnection:
    """Units for one device on the connection Home Assistant shares for its endpoint.

    With a config entry the holds are the entry's, and its unload lets go
    of them. Without one, the config flow's probe, they are temporary
    holds that ``close`` lets go of; those are taken with
    ``async_for_unit`` because Home Assistant hands them out through an
    async context manager.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        params: ModbusParams,
        entry: ConfigEntry | None = None,
    ) -> None:
        self._hass = hass
        self._params = params
        self._entry = entry
        self._temporary = contextlib.AsyncExitStack() if entry is None else None
        self._units: dict[int, ModbusUnit] = {}
        # The timeout each unit asked the link for, so asking again for the
        # same one does not drop the link a second time.
        self._timeouts: dict[int, float] = {}

    @property
    def connected(self) -> bool:
        return any(unit.connected for unit in self._units.values())

    def for_unit(self, unit_id: int) -> ModbusUnit:
        """The unit for ``unit_id``, taken once and kept for the life of the entry.

        Without a config entry the unit has to be taken with
        ``async_for_unit`` first.

        Raises ``HomeAssistantError`` if the device is already in use
        over different link settings.
        """
        unit = self._units.get(unit_id)
        if unit is None:
            if self._entry is None:
                raise RuntimeError(f"unit {unit_id} was not opened with async_for_unit")
            unit = async_get_unit(self._hass, self._entry, self._params, unit_id)
            self._units[unit_id] = unit
        return unit

    async def async_for_unit(self, unit_id: int) -> ModbusUnit:
        """``for_unit`` for either owner: the one way to take a unit without an entry."""
        if self._temporary is not None and unit_id not in self._units:
            unit = await self._temporary.enter_async_context(
                async_get_temporary_unit(self._hass, self._params, unit_id)
            )
            self._units[unit_id] = unit
            return unit
        return self.for_unit(unit_id)

    async def async_require_timeout(self, unit_id: int, seconds: float) -> None:
        """Have the link wait at least ``seconds`` for an answer to any request.

        The link applies a raised timeout from its next connect on, and
        modbus-connection drops a live link for that in a task of its own,
        which cuts whatever request is in flight when it runs. The link is
        therefore dropped here and awaited, so the caller's next request
        reconnects with the new timeout instead of being cut.
        """
        if self._timeouts.get(unit_id) == seconds:
            return
        self._timeouts[unit_id] = seconds
        unit = self.for_unit(unit_id)
        connected = unit.connected
        unit.require_timeout(seconds)
        # The link waits for the highest timeout any unit asked for, so a
        # second micro inverter behind the same gateway asking for the same
        # minute changes nothing and must not cost every holder a reconnect.
        asked = _ASKED_TIMEOUTS.setdefault(self._params.endpoint, {})
        key = self._holder_key(unit_id)
        raised = seconds > max(
            (asked_for for holder, asked_for in asked.items() if holder != key), default=0.0
        )
        asked[key] = seconds
        if connected and raised:
            await unit.disconnect()

    def withdraw_timeout(self, unit_id: int) -> None:
        """Hand back a timeout asked for with ``async_require_timeout``.

        modbus-connection keeps the requirement per unit id, not per holder,
        so this also withdraws what another holder of the same unit id on the
        same link asked for. The link falls back to its default after its
        next connect.
        """
        _ASKED_TIMEOUTS.get(self._params.endpoint, {}).pop(self._holder_key(unit_id), None)
        if self._timeouts.pop(unit_id, None) is not None and unit_id in self._units:
            self._units[unit_id].require_timeout(None)

    def _holder_key(self, unit_id: int) -> tuple[str, int]:
        """Who asked for a timeout: the config entry, or this object for a probe."""
        holder = self._entry.entry_id if self._entry is not None else f"probe-{id(self)}"
        return (holder, unit_id)

    async def connect(self) -> None:
        """Let discovery or cached-layout validation open the link on its first read."""

    async def disconnect(self) -> None:
        """Drop the link; every holder reconnects on its next request."""
        if self._units:
            await next(iter(self._units.values())).disconnect()

    async def close(self) -> None:
        """Let go of what this object holds itself.

        A config entry's holds are released by its unload, in the order
        Home Assistant runs it, so nothing is done for them here.
        """
        if self._temporary is not None:
            await self._temporary.aclose()
            self._temporary = contextlib.AsyncExitStack()
            self._units.clear()
