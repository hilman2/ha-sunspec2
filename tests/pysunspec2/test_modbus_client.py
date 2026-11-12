"""The fork's Modbus client layer: points, groups, models and the scan.

Upstream's test_modbus_client.py drove this layer through a mocked socket.
The socket and serial clients are gone from the fork, so the tests here
drive it through the one device that is left, SunSpecModbusClientDeviceUnit,
over the fake connection in tests/fake_unit.py. What a test asserts is what
went over that connection: the registers asked for and the registers sent.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

from unittest.mock import call

import pytest
from modbus_connection import ModbusProtocolError

from custom_components.sunspec2.pysunspec2 import mb
from custom_components.sunspec2.pysunspec2.modbus.client import SunSpecModbusClientError
from custom_components.sunspec2.pysunspec2.modbus.client import SunSpecModbusClientModel
from custom_components.sunspec2.pysunspec2.modbus.client import SunSpecModbusValueError
from custom_components.sunspec2.pysunspec2.modbus.unit_device import SunSpecModbusClientDeviceUnit

from ..fake_unit import FakeConnection
from ..fake_unit import FakeUnit
from ..fake_unit import register_image

INVERTER = "./tests/test_data/inverter.json"
FRONIUS = "./tests/test_data/inverter_fronius.json"


def _device(image):
    """A unit device over ``image``, not scanned yet, with its unit and connection."""
    unit = FakeUnit(image)
    connection = FakeConnection({1: unit})
    return SunSpecModbusClientDeviceUnit(connection, slave_id=1), unit, connection


async def _scanned(path=INVERTER, **scan):
    """A unit device over the register image of ``path``, scanned without a connect."""
    device, unit, connection = _device(register_image(path))
    await device.async_scan(connect=False, **scan)
    return device, unit, connection


def _model_ids(device):
    return [key for key in device.models if isinstance(key, int)]


# ---------- the scan ---------------------------------------------------------


async def test_a_scan_reads_the_model_bodies_unless_told_to_stop_at_the_headers():
    full, _, _ = await _scanned()
    headers_only, _, _ = await _scanned(full_model_read=False)

    assert _model_ids(headers_only) == _model_ids(full)
    assert full.models[1][0].points["Mn"].cvalue == "SunSpecTest"
    assert headers_only.models[1][0].points["Mn"].cvalue is None


async def test_a_scan_asked_to_connect_opens_the_link_and_drops_it_when_done():
    device, _, connection = _device(register_image(FRONIUS))

    await device.async_scan(full_model_read=False)

    assert _model_ids(device) == [1, 160, 123, 124]
    assert (connection.connects, connection.disconnects) == (1, 1)


async def test_a_scan_on_a_link_the_caller_holds_leaves_the_link_alone():
    _, _, connection = await _scanned(FRONIUS, full_model_read=False)

    assert (connection.connects, connection.disconnects) == (0, 0)


@pytest.mark.parametrize("connect", [True, False])
async def test_the_scan_delay_is_slept_after_the_connect_and_after_every_model(mocker, connect):
    """The base address that answers is not paced: the walk goes straight on to its first model."""
    sleep = mocker.patch("custom_components.sunspec2.pysunspec2.modbus.client.asyncio.sleep")
    device, _, _ = _device(register_image(FRONIUS))

    await device.async_scan(connect=connect, delay=0.25, full_model_read=False)

    models = 4
    assert sleep.await_args_list == [call(0.25)] * (models + (1 if connect else 0))


async def test_a_progress_callback_that_declines_stops_the_scan():
    device, _, _ = _device(register_image(INVERTER))
    seen = []

    def progress(message):
        seen.append(message)
        return len(seen) < 2

    with pytest.raises(SunSpecModbusClientError, match="Device scan terminated"):
        await device.async_scan(connect=False, progress=progress, full_model_read=False)

    assert seen == ["Scanning model 1", "Scanning model 304"]
    # The model the callback declined at was not added.
    assert _model_ids(device) == [1]


async def test_an_error_at_one_base_address_does_not_keep_the_scan_from_the_next():
    """A malformed answer is neither a timeout nor a Modbus exception, and still only one address."""
    device, unit, _ = _device(register_image(FRONIUS, base=0))
    unit.fail_next = ModbusProtocolError("Invalid register count: expected 3, got 1")

    await device.async_scan(connect=False, full_model_read=False)

    assert device.base_addr == 0
    assert unit.reads[:2] == [(40000, 3), (0, 3)]
    assert _model_ids(device) == [1, 160, 123, 124]


async def test_a_length_that_points_past_the_register_space_ends_the_chain():
    """The header behind such a model has no address, so there is nothing to ask for."""
    image = register_image(FRONIUS)
    header = 40002
    while image[header + image[header + 1] + 2] != mb.SUNS_END_MODEL_ID:
        header += image[header + 1] + 2
    image[header + 1] = 0xFFF0
    device, unit, _ = _device(image)

    await device.async_scan(connect=False, full_model_read=False)

    assert _model_ids(device) == [1, 160, 123, 124]
    assert device.models[124][0].model_len == 0xFFF0
    assert unit.reads[-1] == (header + 1, 1)


# ---------- models, groups and points ----------------------------------------


async def test_a_model_handed_too_few_registers_for_its_group_counts_says_so():
    """Model 705 sizes its curves from NCrv and NPt, five registers behind the header."""
    device, unit, _ = await _scanned(full_model_read=False)
    address = device.models[705][0].model_addr
    header = mb.u16_to_data(705) + mb.u16_to_data(unit.holding[address + 1])

    with pytest.raises(
        SunSpecModbusClientError, match=r"Model 705 needs \S+ more register.*async_model_data"
    ):
        SunSpecModbusClientModel(
            model_id=705, model_addr=address, model_len=unit.holding[address + 1], data=header
        )

    unit.reads.clear()
    data = await device.async_model_data(705, address, header)
    assert unit.reads == [(address + 2, 5)]
    model = SunSpecModbusClientModel(
        model_id=705, model_addr=address, model_len=unit.holding[address + 1], data=data
    )
    assert len(model.groups["Crv"]) == 3


async def test_a_repeating_group_read_fetches_that_group_alone():
    device, unit, _ = await _scanned(FRONIUS, full_model_read=False)
    model = device.models[160][0]
    battery = model.groups["module"][3]
    unit.reads.clear()

    await battery.async_read()

    assert unit.reads == [(model.model_addr + battery.offset, battery.len)]
    assert battery.points["IDStr"].cvalue == "ST DISCHA"
    assert model.groups["module"][2].points["IDStr"].cvalue is None


async def test_a_point_read_fetches_only_its_own_registers():
    device, unit, _ = await _scanned(full_model_read=False)
    model = device.models[1][0]
    serial = model.points["SN"]
    unit.reads.clear()

    await serial.async_read()

    assert unit.reads == [(model.model_addr + serial.offset, 16)]
    assert serial.cvalue == "sn-123456789"
    assert model.points["Mn"].cvalue is None


async def test_a_point_write_sends_its_registers_and_leaves_the_point_clean():
    device, unit, _ = await _scanned()
    model = device.models[1][0]
    address = model.points["DA"]
    address.value = 7
    assert address.dirty

    await address.async_write()

    assert unit.writes == [(model.model_addr + address.offset, [7])]
    assert not address.dirty


async def test_a_point_write_of_a_value_the_register_cannot_hold_sends_nothing():
    device, unit, _ = await _scanned()
    address = device.models[1][0].points["DA"]
    address.value = "seven"

    with pytest.raises(SunSpecModbusValueError, match="Point value error for DA seven"):
        await address.async_write()

    assert unit.writes == []


async def test_a_model_write_of_a_value_the_register_cannot_hold_sends_nothing():
    device, unit, _ = await _scanned()
    model = device.models[1][0]
    model.points["DA"].value = "seven"

    with pytest.raises(SunSpecModbusValueError, match="Point value error for DA seven"):
        await model.async_write()

    assert unit.writes == []


async def test_a_model_write_reaches_the_last_points_of_a_nested_repeating_group():
    """The last curve point of model 705 has no clean point behind it to trigger the frame."""
    device, unit, _ = await _scanned()
    model = device.models[705][0]
    last = model.groups["Crv"][2].groups["Pt"][3]
    last.points["V"].value = 10500
    last.points["Var"].value = -2500

    await model.async_write()

    # Both registers are adjacent, so they leave as one frame.
    assert unit.writes == [(model.model_addr + last.points["V"].offset, [10500, 0x10000 - 2500])]
    assert not last.points["Var"].dirty
