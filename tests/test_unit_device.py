"""The api client over a modbus-connection unit, end to end against a register image.

Every test here drives the real chain: SunSpecApiClient, the fork's
unit device, its async scan and model reads, and the register packing,
with only the connection faked (tests/fake_unit.py).
"""

import pytest
from modbus_connection import ModbusConnectionError
from modbus_connection import ModbusTimeoutError

from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.errors import ProtocolError
from custom_components.sunspec2.errors import TransientError
from custom_components.sunspec2.errors import TransportError
from custom_components.sunspec2.pysunspec2.modbus.modbus import REQ_COUNT_MAX
from custom_components.sunspec2.pysunspec2.modbus.unit_device import SunSpecModbusClientDeviceUnit

from .fake_unit import FakeConnection
from .fake_unit import FakeUnit
from .fake_unit import register_image

FRONIUS = "./tests/test_data/inverter_fronius.json"


def _api(hass, connection, **kwargs):
    """An api client whose connection object is the fake, the way _get_connection would build it."""
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass, **kwargs)
    api._connection = connection
    return api


async def test_the_unit_device_reads_what_the_json_device_holds(hass):
    """Scan, model reads and decoding through the unit: values equal the file client's."""
    image = register_image(FRONIUS)
    unit = FakeUnit(image)
    connection = FakeConnection({1: unit})
    api = _api(hass, connection)

    assert await api.async_get_models() == [1, 123, 124, 160]
    common = await api.async_get_data(1)
    storage = await api.async_get_data(124)
    mppt = await api.async_get_data(160)
    limits = await api.async_get_data(123)

    assert common.getValue("Mn") == "Fronius"
    assert common.getValue("Md") == "Symo GEN24 10.0 Plus"
    assert storage.getValue("WChaMax") == 5000
    assert storage.getValue("ChaState") == 55.0
    assert mppt.getValue("N") == 4
    assert mppt.getValue("module:3:IDStr") == "ST DISCHA"
    assert mppt.getValue("module:3:DCW") == 2000
    assert limits.getValue("WMaxLimPct") == 100.0
    assert isinstance(api._client, SunSpecModbusClientDeviceUnit)
    # One eager connect, no request longer than the Modbus maximum.
    assert connection.connects == 1
    assert max(count for _, count in unit.reads) <= REQ_COUNT_MAX
    # The layout is cached from the scan.
    assert api._base_addr == 40000
    assert [model_id for model_id, _, _ in api._model_structure] == [1, 160, 123, 124]


async def test_a_write_lands_in_the_registers_scaled(hass):
    """60 % at scale factor -2 is register value 6000, sent with function code 16."""
    unit = FakeUnit(register_image(FRONIUS))
    api = _api(hass, FakeConnection({1: unit}))
    await api.async_get_models()

    await api.async_write_points(123, [("WMaxLimPct", 60)])

    model = api._client.models[123][0]
    address = model.model_addr + model.points["WMaxLimPct"].offset
    assert unit.writes == [(address, [6000])]
    assert unit.holding[address] == 6000
    assert (await api.async_get_data(123)).getValue("WMaxLimPct") == 60.0


async def test_raw_blocks_read_and_write_another_unit_on_the_same_connection(hass):
    """SMA's registers sit 123 unit ids below the SunSpec ones; the offset picks the unit."""
    connection = FakeConnection({1: FakeUnit(register_image(FRONIUS))})
    other = connection.for_unit(3)
    other.holding.update({30050: 0x0000, 30051: 0x1F41})
    api = _api(hass, connection)
    await api.async_get_models()

    assert await api.async_read_block(30050, 2, unit_id_offset=2) == b"\x00\x00\x1f\x41"
    await api.async_write_block(40148, b"\xff\xff\xf4\x48", unit_id_offset=2)
    assert other.holding[40148] == 0xFFFF and other.holding[40149] == 0xF448
    with pytest.raises(DeviceError, match="Modbus exception 2"):
        await api.async_read_block(50000, 1, unit_id_offset=2)


async def test_a_refused_connect_is_a_transport_error_and_leaves_no_link_behind(hass):
    connection = FakeConnection(refuse=True)
    api = _api(hass, connection)

    with pytest.raises(TransportError):
        await api.async_get_models()

    assert api._client is None
    assert connection.disconnects == 1


async def test_a_device_without_sunspec_is_a_protocol_error(hass):
    connection = FakeConnection({1: FakeUnit({40000: 0, 40001: 0, 0: 0, 1: 0, 2: 0})})
    api = _api(hass, connection)

    with pytest.raises(ProtocolError):
        await api.async_get_models()

    assert connection.connects == 1
    assert connection.disconnects == 1


async def test_a_timeout_is_transient(hass):
    unit = FakeUnit(register_image(FRONIUS))
    api = _api(hass, FakeConnection({1: unit}))
    await api.async_get_models()

    unit.fail_next = ModbusTimeoutError("no answer")
    with pytest.raises(TransientError):
        await api.async_get_data(1)


async def test_a_dropped_link_costs_one_reconnect_inside_the_cycle(hass):
    """The link goes mid-cycle: the client is rebuilt from the cached layout, once."""
    unit = FakeUnit(register_image(FRONIUS))
    connection = FakeConnection({1: unit})
    api = _api(hass, connection)
    await api.async_get_models()

    unit.fail_next = ModbusConnectionError("connection lost")
    wrapper = await api.async_get_data(1)

    assert wrapper.getValue("Mn") == "Fronius"
    assert connection.disconnects == 1
    assert connection.connects == 2
    # Rebuilt from the cache: the three validating reads, not a scan.
    assert api._model_structure is not None


async def test_a_persisted_layout_is_validated_and_rebuilt_without_a_scan(hass):
    image = register_image(FRONIUS)
    first = _api(hass, FakeConnection({1: FakeUnit(image)}))
    await first.async_get_models()
    payload = first.export_model_structure()

    unit = FakeUnit(image)
    api = _api(hass, FakeConnection({1: unit}))
    assert api.import_model_structure(payload) is True

    assert await api.async_get_models() == [1, 123, 124, 160]
    last_id, last_addr, last_len = payload["models"][-1]
    assert unit.reads[:3] == [(40000, 3), (last_addr, 2), (last_addr + 2 + last_len, 1)]
    assert (await api.async_get_data(1)).getValue("SN") == "sn-fronius-1"


async def test_close_drops_the_link_and_shutdown_the_connection(hass):
    connection = FakeConnection({1: FakeUnit(register_image(FRONIUS))})
    api = _api(hass, connection)
    await api.async_get_models()

    await api.async_close()
    assert api._client is None
    assert connection.disconnects == 1
    assert not connection.closed
    # The layout survives a close.
    assert api._model_structure is not None

    await api.async_get_models()
    assert connection.connects == 2

    await api.async_shutdown()
    assert connection.closed


async def test_the_capture_wraps_the_unit_reads(hass):
    unit = FakeUnit(register_image(FRONIUS))
    api = _api(hass, FakeConnection({1: unit}), capture_enabled=True)
    await api.async_get_models()

    captured = api._captured_reads
    assert captured
    assert captured[0]["addr"] == 40000
    assert captured[0]["count"] == 3
    assert captured[0]["hex"] == "53756e530001"
