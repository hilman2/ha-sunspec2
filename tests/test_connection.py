"""SharedConnection: this integration's side of Home Assistant's shared Modbus connection.

``async_get_unit`` itself is Home Assistant's and is exercised for real
here, so the two entries that share a link are shared by the code that
shares them. Only the unit's I/O is faked.
"""

from unittest.mock import patch

import pytest
from homeassistant.components.modbus.connection import DATA_MODBUS_CONNECTIONS
from homeassistant.exceptions import HomeAssistantError
from modbus_connection import ModbusConnectionError
from modbus_connection import ModbusExceptionError
from modbus_connection import ModbusSerialParams
from modbus_connection import ModbusTcpParams
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2.connection import PROBE_ADDRESS
from custom_components.sunspec2.connection import SharedConnection
from custom_components.sunspec2.const import DOMAIN

from .const import MOCK_CONFIG
from .fake_unit import FakeUnit

PARAMS = ModbusTcpParams(host="gateway", port=502)


def _entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG)
    entry.add_to_hass(hass)
    return entry


async def test_two_devices_behind_one_endpoint_share_one_connection(hass):
    """The SolarEdge-style gateway: one TCP session, however many unit ids."""
    first = SharedConnection(hass, PARAMS, _entry(hass))
    second = SharedConnection(hass, PARAMS, _entry(hass))

    first.for_unit(1)
    second.for_unit(2)

    shared = hass.data[DATA_MODBUS_CONNECTIONS]
    assert len(shared) == 1
    assert next(iter(shared.values())).consumers == 2


async def test_a_unit_is_taken_once(hass):
    """Asking again for the same unit id must not take a second hold on the link."""
    connection = SharedConnection(hass, PARAMS, _entry(hass))

    assert connection.for_unit(1) is connection.for_unit(1)

    assert next(iter(hass.data[DATA_MODBUS_CONNECTIONS].values())).consumers == 1


async def test_other_link_settings_on_the_same_serial_port_are_refused(hass):
    """One port cannot run at two baud rates, and the second entry is told so."""
    port = "/dev/ttyUSB0"
    SharedConnection(hass, ModbusSerialParams(device=port, baudrate=9600), _entry(hass)).for_unit(1)
    other = SharedConnection(
        hass, ModbusSerialParams(device=port, baudrate=19200, parity="E"), _entry(hass)
    )

    with pytest.raises(HomeAssistantError, match="already in use"):
        other.for_unit(2)


async def test_without_an_entry_close_lets_go_of_the_holds(hass):
    """The config flow's probe: it closes the link it opened and nothing else's."""
    held_by_entry = SharedConnection(hass, PARAMS, _entry(hass))
    held_by_entry.for_unit(1)
    probe = SharedConnection(hass, PARAMS)
    await probe.async_for_unit(2)
    shared = next(iter(hass.data[DATA_MODBUS_CONNECTIONS].values()))
    assert shared.consumers == 2

    await probe.close()

    # The entry still holds the link, so it stays registered.
    assert shared.consumers == 1
    assert len(hass.data[DATA_MODBUS_CONNECTIONS]) == 1


async def test_a_probe_alone_closes_the_connection(hass):
    probe = SharedConnection(hass, PARAMS)
    await probe.async_for_unit(1)

    await probe.close()

    assert hass.data[DATA_MODBUS_CONNECTIONS] == {}


async def test_an_entrys_holds_are_left_to_its_unload(hass):
    """close() on an entry's connection must not pull the link from under the entry."""
    connection = SharedConnection(hass, PARAMS, _entry(hass))
    connection.for_unit(1)

    await connection.close()

    assert next(iter(hass.data[DATA_MODBUS_CONNECTIONS].values())).consumers == 1


async def test_the_timeout_is_asked_for_on_each_unit(hass):
    unit = FakeUnit()
    with patch("custom_components.sunspec2.connection.async_get_unit", return_value=unit):
        SharedConnection(hass, PARAMS, _entry(hass), timeout=60).for_unit(1)

    assert unit.timeouts == [60]


async def test_connect_reads_once_so_a_refused_connect_fails_there(hass):
    unit = FakeUnit()
    unit.fail_next = ModbusConnectionError("connection refused")
    with patch("custom_components.sunspec2.connection.async_get_unit", return_value=unit):
        connection = SharedConnection(hass, PARAMS, _entry(hass))
        connection.for_unit(1)

        with pytest.raises(ModbusConnectionError):
            await connection.connect()


async def test_an_exception_response_to_the_probe_means_the_link_is_up(hass):
    unit = FakeUnit()  # no registers: the read is answered with an illegal address
    unit.fail_next = ModbusExceptionError(2)
    with patch("custom_components.sunspec2.connection.async_get_unit", return_value=unit):
        connection = SharedConnection(hass, PARAMS, _entry(hass))
        connection.for_unit(1)

        await connection.connect()

    assert unit.reads == [(PROBE_ADDRESS, 1)]


async def test_disconnect_drops_the_link_through_a_unit(hass):
    unit = FakeUnit()
    with patch("custom_components.sunspec2.connection.async_get_unit", return_value=unit):
        connection = SharedConnection(hass, PARAMS, _entry(hass))
        connection.for_unit(1)

        await connection.disconnect()

    assert unit.disconnects == 1


async def test_a_probe_has_to_open_its_unit_asynchronously(hass):
    """Temporary holds come from an async context manager, so for_unit alone cannot take one."""
    probe = SharedConnection(hass, PARAMS)

    with pytest.raises(RuntimeError, match="async_for_unit"):
        probe.for_unit(1)

    await probe.async_for_unit(1)
    assert probe.for_unit(1) is await probe.async_for_unit(1)
    await probe.close()


async def test_an_entrys_unit_can_be_taken_either_way(hass):
    connection = SharedConnection(hass, PARAMS, _entry(hass))

    assert await connection.async_for_unit(1) is connection.for_unit(1)
    assert next(iter(hass.data[DATA_MODBUS_CONNECTIONS].values())).consumers == 1
