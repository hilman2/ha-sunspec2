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
so ``connect`` here is a first read and ``close`` hands the holds back
only where nobody else will.
"""

from __future__ import annotations

import contextlib

from homeassistant.components.modbus.connection import ModbusParams
from homeassistant.components.modbus.connection import async_get_temporary_unit
from homeassistant.components.modbus.connection import async_get_unit
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from modbus_connection import ModbusExceptionError
from modbus_connection import ModbusUnit

# What the first request of a session reads. 40000 is where SunSpec puts
# its marker unless the device says otherwise, but the address is not the
# point: any answer, an exception response included, proves the link is
# up, and the scan that follows finds the real base address.
PROBE_ADDRESS = 40000


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
        timeout: float | None = None,
    ) -> None:
        self._hass = hass
        self._params = params
        self._entry = entry
        self._timeout = timeout
        self._temporary = contextlib.AsyncExitStack() if entry is None else None
        self._units: dict[int, ModbusUnit] = {}

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
            unit = self._adopt(
                unit_id, async_get_unit(self._hass, self._entry, self._params, unit_id)
            )
        return unit

    async def async_for_unit(self, unit_id: int) -> ModbusUnit:
        """``for_unit`` for either owner: the one way to take a unit without an entry."""
        if self._temporary is not None and unit_id not in self._units:
            unit = await self._temporary.enter_async_context(
                async_get_temporary_unit(self._hass, self._params, unit_id)
            )
            return self._adopt(unit_id, unit)
        return self.for_unit(unit_id)

    def _adopt(self, unit_id: int, unit: ModbusUnit) -> ModbusUnit:
        if self._timeout is not None:
            # Only ever raises the shared link's timeout: the config
            # flow's longer walk of a whole model tree asks for it and a
            # running entry's steady-state value changes nothing.
            unit.require_timeout(self._timeout)
        self._units[unit_id] = unit
        return unit

    async def connect(self) -> None:
        """Open the link by reading once, so a refused connect fails here.

        A unit connects on its first request and has no ``connect`` of its
        own. Left to the scan, a refused connect would surface from the
        middle of it as a protocol error. An exception response is an
        answer: the link is up and the scan decides what the device means.
        """
        unit = next(iter(self._units.values()))
        with contextlib.suppress(ModbusExceptionError):
            await unit.read_holding_registers(PROBE_ADDRESS, 1)

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
