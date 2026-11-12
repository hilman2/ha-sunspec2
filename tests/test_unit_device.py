"""The api client over a modbus-connection unit, end to end against a register image.

Every test here drives the real chain: SunSpecApiClient, the fork's
unit device, its async scan and model reads, and the register packing,
with only the connection faked (tests/fake_unit.py).
"""

import logging

import pytest
from modbus_connection import IllegalDataAddressError
from modbus_connection import ModbusConnectionError
from modbus_connection import ModbusExceptionError
from modbus_connection import ModbusProtocolError
from modbus_connection import ModbusTimeoutError

from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.errors import ProtocolError
from custom_components.sunspec2.errors import TransientError
from custom_components.sunspec2.errors import TransportError
from custom_components.sunspec2.pysunspec2.modbus.modbus import FUNC_READ_INPUT
from custom_components.sunspec2.pysunspec2.modbus.modbus import REQ_COUNT_MAX
from custom_components.sunspec2.pysunspec2.modbus.modbus import ModbusClientError
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


@pytest.mark.parametrize(
    ("sf", "value", "raw"),
    [(0, -100, 65436), (-1, -12.5, 65411), (-1, 0, 0), (-2, 12.34, 1234), (-2, 100, 10000)],
)
async def test_percent_setpoint_reaches_the_registers(hass, sf, value, raw):
    unit = FakeUnit(register_image("./tests/test_data/inverter.json"))
    api = _api(hass, FakeConnection({1: unit}), scan_delay=0)
    await api.async_get_models()
    model = api._client.models[704][0]
    sf_address = model.model_addr + model.points["WSetPct_SF"].offset
    address = model.model_addr + model.points["WSetPct"].offset
    unit.holding[sf_address] = sf & 0xFFFF
    await api.async_get_data(704)

    await api.async_write_points(704, [("WSetPct", value)])

    assert unit.writes == [(address, [raw])]
    assert (await api.async_get_data(704)).getValue("WSetPct") == value


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


async def test_a_chain_ending_in_blank_memory_stops_the_scan(hass):
    """An overlong last model must not send the scan walking through zeros (#109).

    The APsystems ECU-R declares its last model longer than its data, so
    the header the walk lands on is blank memory (id 0, length 0) and the
    0xFFFF marker sits inside the declared body. Read as headers, those
    zeros are an endless run of models of length 0: one read every two
    registers up to 65535, and a transport that refuses what lies past it.
    The fake answers every address from the end marker up, so only the
    stop at id 0 ends the walk.
    """
    image = register_image(FRONIUS)
    # Model 160 repeats a group, so its length is checked against its
    # body. Cut the last model off instead, and stretch the one before it.
    headers = []
    addr = 40002
    while image[addr] != 0xFFFF:
        headers.append(addr)
        addr += image[addr + 1] + 2
    expected = sorted(image[header] for header in headers[:-1])
    marker = headers[-1]
    last_header = headers[-2]
    image = {addr: word for addr, word in image.items() if addr < marker}
    image[marker] = 0xFFFF
    image[marker + 1] = 0
    image[last_header + 1] += 24
    for addr in range(marker + 2, 65536):
        image[addr] = 0
    unit = FakeUnit(image)
    api = _api(hass, FakeConnection({1: unit}))

    assert await api.async_get_models() == expected
    assert max(addr for addr, _ in unit.reads) < marker + 100


APSYSTEMS = "./tests/test_data/inverter_apsystems.json"

#: The 24 registers behind the ECU-R's header at 40184, from the dump in #109.
ECU_R_CONTROLS = [65535, 65535, 1, 300, 65535, 65535, 65535, 1, 32768, 65535, 65535, 65535]
ECU_R_CONTROLS += [65535, 32768, 32768, 32768] + [65535] * 8


def _ecu_r_image(manufacturer="APsystems"):
    """A micro inverter behind an ECU-R: its controls labelled 114, 48 registers declared."""
    image = register_image(APSYSTEMS)
    header = max(addr for addr, word in image.items() if word == 0xFFFF)
    image[header] = 114
    image[header + 1] = 48
    for index, word in enumerate(ECU_R_CONTROLS):
        image[header + 2 + index] = word
    image[header + 2 + 24] = 0xFFFF
    for addr in range(header + 2 + 25, header + 2 + 60):
        image[addr] = 0
    # Mn is the first string of model 1, behind its id and length.
    for index in range(16):
        char = manufacturer[index * 2 : index * 2 + 2].ljust(2, "\0")
        image[40004 + index] = int.from_bytes(char.encode(), "big")
    return image, header


async def test_the_ecu_r_controls_are_read_as_model_123(hass):
    """Header 114 of 48 registers on an APsystems unit holds model 123 (#109)."""
    image, header = _ecu_r_image()
    unit = FakeUnit(image)
    api = _api(hass, FakeConnection({1: unit}))

    assert await api.async_get_models() == [1, 103, 123]
    controls = await api.async_get_data(123)
    assert controls.getValue("WMaxLimPct") == 30.0
    assert controls.getValue("WMaxLim_Ena") == 1

    # The cache keeps what the device holds, so it validates against it.
    assert (114, header, 48) in api._model_structure

    # A reconnect restores the layout from the cache and relabels again.
    api._client = None
    assert await api.async_get_models() == [1, 103, 123]

    await api.async_write_points(123, [("WMaxLimPct", 50.0)])
    assert unit.writes == [(header + 2 + 3, [500])]


async def test_a_114_of_48_registers_from_another_maker_stays_114(hass):
    image, _ = _ecu_r_image(manufacturer="Acme")
    api = _api(hass, FakeConnection({1: FakeUnit(image)}))

    assert await api.async_get_models() == [1, 103, 114]


# ---------- errors, by the category the coordinator acts on ------------------


async def test_a_malformed_answer_while_reading_a_model_is_a_transport_error(hass):
    """The library has no finer class for it, and neither a retry nor the device is to blame."""
    unit = FakeUnit(register_image(FRONIUS))
    api = _api(hass, FakeConnection({1: unit}), scan_delay=0)
    await api.async_get_models()

    unit.fail_next = ModbusProtocolError("Invalid register count: expected 68, got 3")
    with pytest.raises(
        TransportError,
        match="Modbus transport error while reading model 1: Invalid register count",
    ):
        await api.async_get_data(1)


async def test_a_modbus_exception_code_the_library_has_no_class_for_is_still_a_device_error(hass):
    unit = FakeUnit(register_image(FRONIUS))
    api = _api(hass, FakeConnection({1: unit}), scan_delay=0)
    await api.async_get_models()

    unit.fail_next = ModbusExceptionError.from_code(0x30)
    with pytest.raises(DeviceError, match="register 40000: Modbus exception 48$"):
        await api.async_read_block(40000, 2)


@pytest.mark.parametrize(
    ("failure", "category", "message"),
    [
        (ModbusTimeoutError("no answer"), TransientError, "Modbus read timeout at register 40000"),
        (
            ModbusConnectionError("connection lost"),
            TransportError,
            "Modbus read failed at register 40000: connection lost",
        ),
    ],
)
async def test_a_failed_raw_block_read_lands_in_its_category(hass, failure, category, message):
    unit = FakeUnit(register_image(FRONIUS))
    api = _api(hass, FakeConnection({1: unit}), scan_delay=0)
    await api.async_get_models()

    unit.fail_next = failure
    with pytest.raises(category, match=message):
        await api.async_read_block(40000, 2)


@pytest.mark.parametrize(
    ("failure", "category", "message"),
    [
        (ModbusTimeoutError("no answer"), TransientError, "Modbus write timeout at register 40148"),
        (
            IllegalDataAddressError(),
            DeviceError,
            "Modbus exception writing register 40148: Modbus exception 2",
        ),
        (
            ModbusConnectionError("connection lost"),
            TransportError,
            "Modbus write failed at register 40148: connection lost",
        ),
    ],
)
async def test_a_failed_raw_block_write_lands_in_its_category(hass, failure, category, message):
    unit = FakeUnit(register_image(FRONIUS))
    api = _api(hass, FakeConnection({1: unit}), scan_delay=0)
    await api.async_get_models()

    unit.fail_next = failure
    with pytest.raises(category, match=message):
        await api.async_write_block(40148, b"\xff\xff\xf4\x48")

    assert unit.writes == []


# ---------- writes -----------------------------------------------------------


async def test_a_float_register_keeps_the_fraction_of_the_value_written(hass):
    """Only a whole-number register gets its value rounded to an int.

    No fixture has a writable float point, and pysunspec2 does not
    enforce the access of a point, so the AC current of the float
    inverter model 113 stands in for one.
    """
    unit = FakeUnit(register_image("./tests/test_data/inverter_kostal.json"))
    api = _api(hass, FakeConnection({1: unit}), scan_delay=0)
    await api.async_get_models()
    model = api._client.models[113][0]

    await api.async_write_points(113, [("A", 12.5)])

    # 12.5 as an IEEE 754 single is 0x41480000; rounded it would be 12, 0x41400000.
    assert unit.writes == [(model.model_addr + model.points["A"].offset, [0x4148, 0x0000])]


class _FunctionCodeUnit(FakeUnit):
    """Notes the function code each write request went out with."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[int, int, list[int]]] = []

    async def write_register(self, address: int, value: int) -> None:
        self.requests.append((6, address, [value]))

    async def write_registers(self, address: int, values: list[int]) -> None:
        self.requests.append((16, address, list(values)))


@pytest.mark.parametrize(("single_register_write", "function_code"), [(True, 6), (False, 16)])
async def test_one_register_goes_out_with_function_code_6_only_when_asked_to(
    single_register_write, function_code
):
    unit = _FunctionCodeUnit()
    device = SunSpecModbusClientDeviceUnit(
        FakeConnection({1: unit}), slave_id=1, single_register_write=single_register_write
    )

    await device.async_write(40010, b"\x00\x07")
    await device.async_write(40020, b"\x00\x07\x00\x08")

    assert unit.requests == [(function_code, 40010, [7]), (16, 40020, [7, 8])]


# ---------- the device's own interface ---------------------------------------


class _InputRegisterUnit(FakeUnit):
    """A unit whose input registers hold something else than its holding registers."""

    def __init__(self, holding: dict[int, int], inputs: dict[int, int]) -> None:
        super().__init__(holding)
        self.inputs = inputs

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        return [self.inputs[register] for register in range(address, address + count)]


async def test_a_read_goes_to_the_input_registers_when_the_caller_names_them():
    unit = _InputRegisterUnit({30000: 0x1111, 30001: 0x2222}, {30000: 0x1234, 30001: 0x5678})
    device = SunSpecModbusClientDeviceUnit(FakeConnection({1: unit}), slave_id=1)

    assert await device.async_read(30000, 2) == b"\x11\x11\x22\x22"
    assert await device.async_read(30000, 2, FUNC_READ_INPUT) == b"\x12\x34\x56\x78"


@pytest.mark.parametrize(
    ("call", "twin"),
    [
        (lambda device: device.connect(), "async_connect"),
        (lambda device: device.disconnect(), "async_disconnect"),
        (lambda device: device.read(40000, 3), "async_read"),
        (lambda device: device.write(40000, b"\x00\x01"), "async_write"),
    ],
)
def test_the_sync_methods_refuse_and_name_their_async_twin(call, twin):
    """Upstream's entry points block a thread on the socket, and here there is neither."""
    unit = FakeUnit(register_image(FRONIUS))
    connection = FakeConnection({1: unit})
    device = SunSpecModbusClientDeviceUnit(connection, slave_id=1)

    with pytest.raises(ModbusClientError, match=f"use {twin}"):
        call(device)

    assert (connection.connects, connection.disconnects) == (0, 0)
    assert (unit.reads, unit.writes) == ([], [])


async def test_closing_the_device_closes_the_connection_where_a_disconnect_only_drops_the_link():
    connection = FakeConnection()
    device = SunSpecModbusClientDeviceUnit(connection, slave_id=1)
    await device.async_connect()

    await device.async_disconnect()
    assert (connection.disconnects, connection.closed) == (1, False)

    await device.async_close()
    assert connection.closed


# ---------- the cached layout ------------------------------------------------


class _RefusingOnce(FakeUnit):
    """Answers the first read that starts at ``refused`` with Modbus exception 2."""

    def __init__(self, holding: dict[int, int], refused: int) -> None:
        super().__init__(holding)
        self.refused: int | None = refused

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        if address == self.refused:
            self.refused = None
            self.reads.append((address, count))
            raise IllegalDataAddressError()
        return await super().read_holding_registers(address, count)


async def test_a_layout_whose_models_cannot_be_rebuilt_is_scanned_afresh(hass, caplog):
    """A cache that validates but does not rebuild must cost a scan, not the connect.

    Model 705 sizes its curves from two counts behind its header, and
    the rebuild reads them. The device refuses that one read, after
    the three validating reads went through.
    """
    image = register_image("./tests/test_data/inverter.json")
    first = _api(hass, FakeConnection({1: FakeUnit(image)}), scan_delay=0)
    models = await first.async_get_models()
    payload = first.export_model_structure()
    counts_of_705 = next(addr for model_id, addr, _ in payload["models"] if model_id == 705) + 2

    unit = _RefusingOnce(image, refused=counts_of_705)
    api = _api(hass, FakeConnection({1: unit}), scan_delay=0)
    assert api.import_model_structure(payload) is True

    with caplog.at_level(logging.DEBUG):
        assert await api.async_get_models() == models

    assert "Rebuilding models from cache failed, falling back to scan" in caplog.text
    # The base address is read twice: to validate the cache, then by the scan.
    assert unit.reads.count((40000, 3)) == 2
    assert api.export_model_structure()["models"] == payload["models"]
    assert (await api.async_get_data(705)).getValue("NCrv") == 3
