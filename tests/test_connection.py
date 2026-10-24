"""SharedConnection: this integration's side of Home Assistant's shared Modbus connection.

``async_get_unit`` itself is Home Assistant's and is exercised for real
here, so the two entries that share a link are shared by the code that
shares them. Only the unit's I/O is faked.
"""

import asyncio
import struct
from typing import cast
from unittest.mock import patch

import pytest
from homeassistant.components.modbus.connection import DATA_MODBUS_CONNECTIONS
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from modbus_connection import ModbusConnectionError
from modbus_connection import ModbusExceptionError
from modbus_connection import ModbusSerialParams
from modbus_connection import ModbusTcpParams
from modbus_connection import ModbusTimeoutError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.config_flow import SunSpecFlowHandler
from custom_components.sunspec2.connection import SharedConnection
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.errors import TransportError

from . import create_mock_sunspec_config_entry
from .const import MOCK_CONFIG
from .fake_unit import FakeUnit
from .fake_unit import register_image
from .test_config_flow import _open_manual_step

PARAMS = ModbusTcpParams(host="gateway", port=502)


@pytest.fixture
async def tcp_device(hass, socket_enabled):
    """A device that silently ignores reads outside its SunSpec register map."""
    registers = register_image("./tests/test_data/inverter_fronius.json")
    writers = set()
    reads = []
    closed = asyncio.Event()

    async def serve(reader, writer):
        writers.add(writer)
        try:
            while True:
                header = await reader.readexactly(7)
                transaction, protocol, length, unit_id = struct.unpack(">HHHB", header)
                body = await reader.readexactly(length - 1)
                function, address, count = struct.unpack(">BHH", body)
                assert function == 3
                reads.append((unit_id, address, count))
                if any(address + offset not in registers for offset in range(count)):
                    continue
                values = [registers[address + offset] for offset in range(count)]
                response = struct.pack(">BB", function, count * 2) + struct.pack(
                    f">{count}H", *values
                )
                writer.write(
                    struct.pack(">HHHB", transaction, protocol, len(response) + 1, unit_id)
                )
                writer.write(response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            writers.discard(writer)
            if not writers:
                closed.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, registers, reads, writers, closed
    finally:
        for shared in hass.data.get(DATA_MODBUS_CONNECTIONS, {}).values():
            await shared.connection.close()
        server.close()
        await server.wait_closed()
        for writer in list(writers):
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in list(writers)))


def _probe_flow(hass):
    flow = SunSpecFlowHandler()
    flow.hass = hass
    return flow


def _fast_client(*args, **kwargs):
    kwargs["scan_delay"] = 0
    return SunSpecApiClient(*args, **kwargs)


@pytest.mark.parametrize("base", [0, 50000])
async def test_probe_finds_other_bases_when_40000_is_silent(hass, tcp_device, base):
    port, registers, reads, _, _ = tcp_device
    shifted = register_image("./tests/test_data/inverter_fronius.json", base=base)
    registers.clear()
    registers.update(shifted)
    params = ModbusTcpParams(host="127.0.0.1", port=port)
    holder = SharedConnection(hass, params, _entry(hass))
    holder.for_unit(1).require_timeout(0.05)
    flow = _probe_flow(hass)
    try:
        with patch("custom_components.sunspec2.config_flow.SunSpecApiClient", _fast_client):
            async with asyncio.timeout(1):
                assert await flow._test_connection_tcp("127.0.0.1", port, 2)
        assert flow.client._base_addr == base
        assert any(address == base for _, address, _ in reads)
    finally:
        await flow._close_probe_client()


@pytest.mark.parametrize("probe_unit", [1, 2])
async def test_closing_probe_preserves_another_holders_timeout(hass, tcp_device, probe_unit):
    port, _, _, _, _ = tcp_device
    params = ModbusTcpParams(host="127.0.0.1", port=port)
    holder = SharedConnection(hass, params, _entry(hass))
    unit = holder.for_unit(1)
    unit.require_timeout(0.05)
    flow = _probe_flow(hass)
    try:
        with patch("custom_components.sunspec2.config_flow.SunSpecApiClient", _fast_client):
            assert await flow._test_connection_tcp("127.0.0.1", port, probe_unit)
    finally:
        await flow._close_probe_client()

    async with asyncio.timeout(0.5):
        with pytest.raises(ModbusTimeoutError):
            await unit.read_holding_registers(65000, 1)


async def test_refused_connection_is_a_transport_error(hass, socket_enabled, unused_tcp_port):
    api = SunSpecApiClient(
        host="127.0.0.1",
        port=unused_tcp_port,
        unit_id=1,
        hass=hass,
        scan_delay=0,
        entry=_entry(hass),
    )
    try:
        with pytest.raises(TransportError):
            await api.async_get_models()
    finally:
        await api.async_shutdown()


@pytest.mark.parametrize("transport", ["tcp", "rtu"])
async def test_probe_has_a_total_timeout_budget(hass, transport):
    flow = _probe_flow(hass)

    async def never_answers(*args):
        await asyncio.Event().wait()

    try:
        with (
            patch("custom_components.sunspec2.config_flow.SETUP_TIMEOUT", 0.01),
            patch.object(SunSpecApiClient, "async_get_device_info", never_answers),
        ):
            async with asyncio.timeout(0.5):
                if transport == "tcp":
                    result = await flow._test_connection_tcp("test", 502, 1)
                else:
                    result = await flow._test_connection_rtu("/dev/ttyTEST", 9600, "N", 1)
        assert result is False
        assert flow._errors == {"base": "timeout"}
    finally:
        await flow._close_probe_client()


@pytest.mark.parametrize("other_holder", [False, True])
async def test_cancelling_setup_releases_its_temporary_hold(hass, tcp_device, other_holder):
    port, _, _, writers, closed = tcp_device
    params = ModbusTcpParams(host="127.0.0.1", port=port)
    unit = None
    if other_holder:
        holder = SharedConnection(hass, params, _entry(hass))
        unit = holder.for_unit(1)
    clients = []

    def make_client(*args, **kwargs):
        client = _fast_client(*args, **kwargs)
        clients.append(client)
        return client

    try:
        with patch("custom_components.sunspec2.config_flow.SunSpecApiClient", make_client):
            result = await _open_manual_step(hass)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={"host": "127.0.0.1", "port": port, "unit_id": 2}
            )
        assert result["step_id"] == "settings"
        assert writers

        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()

        shared = hass.data[DATA_MODBUS_CONNECTIONS]
        if other_holder:
            assert next(iter(shared.values())).consumers == 1
            assert await unit.read_holding_registers(40000, 2) == [0x5375, 0x6E53]
        else:
            assert not shared
            async with asyncio.timeout(1):
                await closed.wait()
            assert not writers
    finally:
        for client in clients:
            await client.async_shutdown()


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


async def test_connect_does_not_probe_a_fixed_address(hass):
    unit = FakeUnit()
    with patch("custom_components.sunspec2.connection.async_get_unit", return_value=unit):
        connection = SharedConnection(hass, PARAMS, _entry(hass))
        connection.for_unit(1)
        await connection.connect()
    assert unit.reads == []
    assert unit.timeouts == []


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
