"""The SolarEdge profile: the raw register blocks, their reader, and the battery entities.

The entity tests run against tests/test_data/inverter_solaredge.json
plus the register images in tests/solaredge_registers.py.
"""

import struct
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.const import CONF_WRITE_BETA_ENABLED
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.errors import TransientError
from custom_components.sunspec2.errors import TransportError
from custom_components.sunspec2.raw_blocks import REPROBE_CYCLES
from custom_components.sunspec2.raw_blocks import RawBlockReader
from custom_components.sunspec2.raw_blocks import decode_block
from custom_components.sunspec2.raw_blocks import decode_field
from custom_components.sunspec2.raw_blocks import encode_value
from custom_components.sunspec2.vendor_blocks import RawBlockNumber
from custom_components.sunspec2.vendor_blocks import RawBlockSelect
from custom_components.sunspec2.vendor_blocks import RawBlockSensor
from custom_components.sunspec2.vendor_blocks import RawKeepAliveSwitch
from custom_components.sunspec2.vendor_blocks import RawWriteCountSensor
from custom_components.sunspec2.vendors import profile_for
from custom_components.sunspec2.vendors.profile import RawBlock
from custom_components.sunspec2.vendors.profile import RawField
from custom_components.sunspec2.vendors.solaredge import BATTERY_BASE
from custom_components.sunspec2.vendors.solaredge import REMOTE_COMMAND_REARM_SECONDS
from custom_components.sunspec2.vendors.solaredge import SOLAREDGE
from custom_components.sunspec2.vendors.solaredge import site_limit_mode
from custom_components.sunspec2.vendors.solaredge import status_vendor4

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .solaredge_registers import battery_3_registers

# ---------- decoding ---------------------------------------------------------


def test_a_float32_with_the_low_word_first_decodes_like_solaredge_writes_it():
    """5000.0 is 0x459C4000; SolarEdge sends the 0x4000 word first."""
    assert decode_field(RawField("x", 0, "float32"), b"\x40\x00\x45\x9c", "little") == 5000.0
    assert decode_field(RawField("x", 0, "float32"), b"\x45\x9c\x40\x00", "big") == 5000.0


def test_integers_in_both_word_orders():
    assert decode_field(RawField("x", 0, "uint32"), b"\x00\x02\x00\x01", "little") == 0x00010002
    assert decode_field(RawField("x", 0, "uint32"), b"\x00\x01\x00\x02", "big") == 0x00010002
    assert decode_field(RawField("x", 0, "int16"), b"\xff\xfe", "big") == -2
    assert decode_field(RawField("x", 0, "int32"), b"\xff\xfe\xff\xff", "little") == -2
    assert (
        decode_field(RawField("x", 0, "uint64"), b"\x00\x04\x00\x03\x00\x02\x00\x01", "little")
        == 0x0001000200030004
    )


def test_not_implemented_sentinels_decode_to_none():
    assert decode_field(RawField("x", 0, "uint16"), b"\xff\xff", "big") is None
    assert decode_field(RawField("x", 0, "int16"), b"\x80\x00", "big") is None
    assert decode_field(RawField("x", 0, "uint32"), b"\xff\xff\xff\xff", "little") is None
    assert decode_field(RawField("x", 0, "int32"), b"\x00\x00\x80\x00", "little") is None
    assert decode_field(RawField("x", 0, "uint64"), b"\xff" * 8, "little") is None
    nan = struct.pack(">f", float("nan"))
    assert decode_field(RawField("x", 0, "float32"), nan, "big") is None
    # The float extremes mean "no limit set", not a reading.
    assert decode_field(RawField("x", 0, "float32"), b"\xff\x7f\xff\xff", "big") is None


def test_strings_lose_padding_and_control_characters():
    raw = b"Home Battery 48V\x00\x00\x01\x00" + bytes(12)
    assert decode_field(RawField("x", 0, "string", 16), raw, "little") == "Home Battery 48V"


@pytest.mark.parametrize(
    ("kind", "value"),
    [("uint16", 3), ("int16", -2), ("uint32", 86400), ("float32", -512.0), ("uint64", 123456)],
)
@pytest.mark.parametrize("word_order", ["big", "little"])
def test_encoding_and_decoding_are_inverse(kind, value, word_order):
    data = encode_value(kind, value, word_order)
    assert decode_field(RawField("x", 0, kind), data, word_order) == value


def test_a_short_answer_is_a_device_error():
    block = RawBlock("b", 100, 2, (RawField("x", 0, "uint32"),))
    with pytest.raises(DeviceError):
        decode_block(block, b"\x00\x01")


# ---------- the profile ------------------------------------------------------


def test_solaredge_is_matched_with_or_without_the_trailing_space():
    assert profile_for("SolarEdge ") is SOLAREDGE
    assert profile_for("SolarEdge") is SOLAREDGE
    assert profile_for("Fronius") is not SOLAREDGE


def test_the_vendor_status_prints_like_setapp():
    assert status_vendor4(0x180000BF) == "18xBF"
    assert status_vendor4(0) == "0x0"
    assert status_vendor4(None) is None


def test_the_site_limit_mode_is_one_of_three_bits():
    assert site_limit_mode(1) == "export_control_export_import_meter"
    assert site_limit_mode(2 | 1024) == "export_control_consumption_meter"
    assert site_limit_mode(4 | 2048) == "production_control"
    assert site_limit_mode(0) == "disabled"
    assert site_limit_mode(3) is None
    assert site_limit_mode(None) is None


def test_the_battery_data_block_is_gated_on_the_rated_energy():
    blocks = {block.key: block for block in SOLAREDGE.raw_blocks}
    assert blocks["battery_1"].gate == ("battery_1_info", "rated_energy")
    assert blocks["battery_1"].address == BATTERY_BASE[1] + 68
    assert blocks["battery_3_info"].address == 0xE400


# ---------- the reader -------------------------------------------------------

BLOCK = RawBlock("one", 100, 2, (RawField("value", 0, "uint32"),), word_order="little")
GATED = RawBlock("two", 200, 1, (RawField("flag", 0, "uint16"),), gate=("one", "value"))


def _reader(read):
    api = Mock()
    api.async_read_block = AsyncMock(side_effect=read)
    return RawBlockReader(api, Mock()), api


async def test_an_absent_block_is_left_alone_for_a_while():
    reader, api = _reader(DeviceError("Modbus exception 2"))
    assert await reader.async_read((BLOCK,)) == {}
    # The cycles until the reprobe, none of them a read.
    for _ in range(REPROBE_CYCLES - 1):
        assert await reader.async_read((BLOCK,)) == {}
    assert api.async_read_block.await_count == 1
    api.async_read_block.side_effect = None
    api.async_read_block.return_value = b"\x00\x07\x00\x00"
    assert await reader.async_read((BLOCK,)) == {"one": {"value": 7}}


async def test_a_silent_block_costs_one_timeout_and_then_waits():
    reader, api = _reader(TransientError("Modbus read timeout at register 100"))
    assert await reader.async_read((BLOCK,)) == {}
    assert await reader.async_read((BLOCK,)) == {}
    assert api.async_read_block.await_count == 1


async def test_a_connection_failure_keeps_the_last_reading():
    reader, api = _reader(None)
    api.async_read_block.return_value = b"\x00\x07\x00\x00"
    assert await reader.async_read((BLOCK,)) == {"one": {"value": 7}}
    api.async_read_block.side_effect = TransportError("closed")
    assert await reader.async_read((BLOCK,)) == {"one": {"value": 7}}
    assert api.async_read_block.await_count == 2


async def test_a_gated_block_is_read_only_while_its_gate_is_positive():
    reader, api = _reader(None)
    api.async_read_block.return_value = b"\x00\x00\x00\x00"
    assert await reader.async_read((BLOCK, GATED)) == {"one": {"value": 0}}
    assert api.async_read_block.await_count == 1
    api.async_read_block.return_value = b"\x00\x01\x00\x00"
    result = await reader.async_read((BLOCK, GATED))
    assert result == {"one": {"value": 1}, "two": {"flag": 1}}


# ---------- the entities ------------------------------------------------------


async def _entry(hass):
    entry = create_mock_sunspec_config_entry(hass)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    return entry


def _sensors(hass):
    component = hass.data.get("entity_components", {}).get("sensor")
    return (
        {s.unique_id: s for s in component.entities if isinstance(s, RawBlockSensor)}
        if component
        else {}
    )


def _uid(entry, block, field):
    return get_sunspec_unique_id(entry.entry_id, f"raw:{block}:{field}", 0, 0)


def _device(hass, entry, key):
    """The device registry entry of a vendor-added device, found by its three-part identifier."""
    identifier = cast(tuple[str, str], (DOMAIN, entry.entry_id, f"raw:{key}"))
    return dr.async_get(hass).async_get_device_by_identifier(identifier, entry.entry_id)


async def test_a_home_hub_gets_its_battery_as_a_device(hass, sunspec_solaredge_client_mock):
    entry = await _entry(hass)
    coordinator = entry.runtime_data
    assert coordinator.vendor is SOLAREDGE
    assert set(coordinator.raw_blocks) == {
        "grid_status",
        "status_vendor4",
        "site_limit",
        "storage_control",
        "battery_1_info",
        "battery_1",
        "battery_2_info",
        "power_control",
    }
    sensors = _sensors(hass)

    soe = sensors[_uid(entry, "battery_1", "state_of_energy")]
    assert soe.native_value == 50.0
    assert sensors[_uid(entry, "battery_1", "power")].native_value == -512.0
    assert sensors[_uid(entry, "battery_1", "status")].native_value == "discharge"
    assert sensors[_uid(entry, "battery_1", "energy_imported")].native_value == 234567
    assert sensors[_uid(entry, "battery_1_info", "rated_energy")].native_value == 9700.0

    device = _device(hass, entry, "battery_1")
    assert device is not None
    assert device.manufacturer == "SolarEdge"
    assert device.model == "Home Battery 48V"
    assert device.serial_number == "B1234567"
    assert device.sw_version == "DCDC 3.3.9"
    assert device.name == "SE10K-RWS48BNN4 Battery 1"


async def test_the_inverter_device_gets_grid_status_and_the_vendor_code(
    hass, sunspec_solaredge_client_mock
):
    entry = await _entry(hass)
    sensors = _sensors(hass)
    assert sensors[_uid(entry, "grid_status", "grid_status")].native_value == "on_grid"
    assert sensors[_uid(entry, "status_vendor4", "status_vendor4")].native_value == "18xBF"
    assert sensors[_uid(entry, "power_control", "rrcr_state")].native_value == 0
    # On the inverter's own device, not on a battery.
    grid = sensors[_uid(entry, "grid_status", "grid_status")]
    assert (DOMAIN, entry.entry_id, "inverter_three_phase") in grid.device_info["identifiers"]


async def test_a_phantom_and_an_absent_slot_get_no_sensors(hass, sunspec_solaredge_client_mock):
    entry = await _entry(hass)
    sensors = _sensors(hass)
    assert _uid(entry, "battery_2", "state_of_energy") not in sensors
    assert _uid(entry, "battery_2_info", "rated_energy") not in sensors
    assert _uid(entry, "battery_3", "state_of_energy") not in sensors
    assert "battery_3_info" not in entry.runtime_data.raw_blocks


async def test_a_block_switched_on_later_gets_its_sensors_then(hass, sunspec_solaredge_client_mock):
    """SolarEdge support enables the battery registers per inverter, at any time."""
    entry = await _entry(hass)
    coordinator = entry.runtime_data
    assert _uid(entry, "battery_3", "state_of_energy") not in _sensors(hass)

    sunspec_solaredge_client_mock.registers.update(battery_3_registers())
    # The reader would wait REPROBE_CYCLES on its own; skip the wait.
    coordinator._raw_reader._probes.clear()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    sensors = _sensors(hass)
    assert sensors[_uid(entry, "battery_3", "state_of_energy")].native_value == 50.0
    device = _device(hass, entry, "battery_3")
    assert device is not None and device.manufacturer == "BYD"


async def test_a_silent_block_does_not_fail_the_cycle(hass, sunspec_solaredge_client_mock, caplog):
    """Firmware from 4.23 hangs on a block it does not serve; the rest still loads."""
    sunspec_solaredge_client_mock.hang.add(0xE004)
    entry = await _entry(hass)
    coordinator = entry.runtime_data
    assert coordinator.last_update_success
    assert "storage_control" not in coordinator.raw_blocks
    assert "battery_1" in coordinator.raw_blocks
    assert "no answer" in caplog.text
    assert _uid(entry, "storage_control", "control_mode") not in _sensors(hass)


async def test_a_device_without_the_feature_set_still_has_the_inverter(
    hass, sunspec_solaredge_client_mock
):
    """Support has not enabled the battery registers: exception 2 on every SolarEdge block."""
    sunspec_solaredge_client_mock.registers = {}
    entry = await _entry(hass)
    coordinator = entry.runtime_data
    assert coordinator.last_update_success
    assert coordinator.raw_blocks == {}
    assert _sensors(hass) == {}
    assert coordinator.data[103] is not None


# ---------- the controls ------------------------------------------------------


def _controls(hass, platform, cls):
    component = hass.data.get("entity_components", {}).get(platform)
    return (
        {e.translation_key: e for e in component.entities if isinstance(e, cls)}
        if component
        else {}
    )


async def _write_entry(hass, beta=False):
    entry = create_mock_sunspec_config_entry(
        hass, options={CONF_WRITE_BETA_ENABLED: True} if beta else {}
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    return entry


async def test_the_storage_controls_exist_without_the_beta(hass, sunspec_solaredge_client_mock):
    await _write_entry(hass)
    selects = _controls(hass, "select", RawBlockSelect)
    numbers = _controls(hass, "number", RawBlockNumber)
    switches = _controls(hass, "switch", RawKeepAliveSwitch)
    assert set(selects) == {
        "storage_control_mode",
        "storage_ac_charge_policy",
        "storage_default_mode",
        "storage_command_mode",
    }
    assert set(numbers) == {
        "storage_ac_charge_limit",
        "storage_backup_reserve",
        "storage_command_timeout",
        "storage_charge_limit",
        "storage_discharge_limit",
    }
    assert set(switches) == {"storage_command_rearm"}
    assert selects["storage_control_mode"].current_option == "maximize_self_consumption"
    assert selects["storage_command_mode"].current_option == "maximize_self_consumption"
    assert numbers["storage_command_timeout"].native_value == 3600.0
    assert numbers["storage_backup_reserve"].native_value == 10.0


async def test_the_export_controls_need_the_beta(hass, sunspec_solaredge_client_mock):
    await _write_entry(hass, beta=True)
    assert {"site_limit_mode", "site_limit_scope"} <= set(_controls(hass, "select", RawBlockSelect))
    numbers = _controls(hass, "number", RawBlockNumber)
    assert {"site_limit", "active_power_limit"} <= set(numbers)
    assert numbers["site_limit"].native_value == 5000.0
    assert numbers["active_power_limit"].native_value == 100.0
    assert "active_power_limit_reassert" in _controls(hass, "switch", RawKeepAliveSwitch)


async def test_a_select_writes_the_register_and_reads_it_back(hass, sunspec_solaredge_client_mock):
    """Remote control is E004 = 4; the register image is the inverter, so the read-back is real."""
    await _write_entry(hass)
    select = _controls(hass, "select", RawBlockSelect)["storage_control_mode"]

    await select.async_select_option("remote_control")
    await hass.async_block_till_done()

    assert sunspec_solaredge_client_mock.registers[0xE004] == 4
    assert select.current_option == "remote_control"


async def test_a_number_writes_in_the_inverters_word_order(hass, sunspec_solaredge_client_mock):
    """The timeout is a uint32 at E00B; 900 lands as 0x0384 in the first register."""
    await _write_entry(hass)
    number = _controls(hass, "number", RawBlockNumber)["storage_command_timeout"]

    await number.async_set_native_value(900)
    await hass.async_block_till_done()

    assert sunspec_solaredge_client_mock.registers[0xE00B] == 0x0384
    assert sunspec_solaredge_client_mock.registers[0xE00C] == 0
    assert number.native_value == 900.0


async def test_the_site_limit_mode_keeps_the_other_bits(hass, sunspec_solaredge_client_mock):
    """Bit 10 says external production is present; choosing a mode must not clear it."""
    sunspec_solaredge_client_mock.registers[0xE000] = 1 | (1 << 10)
    await _write_entry(hass, beta=True)
    select = _controls(hass, "select", RawBlockSelect)["site_limit_mode"]
    assert select.current_option == "export_control_export_import_meter"

    await select.async_select_option("production_control")
    await hass.async_block_till_done()

    assert sunspec_solaredge_client_mock.registers[0xE000] == 4 | (1 << 10)
    assert select.current_option == "production_control"


async def test_persistent_writes_are_counted_and_volatile_ones_are_not(
    hass, sunspec_solaredge_client_mock, caplog
):
    entry = await _write_entry(hass, beta=True)
    numbers = _controls(hass, "number", RawBlockNumber)
    counter = next(iter(_controls(hass, "sensor", RawWriteCountSensor).values()))
    assert counter.native_value == 0

    await numbers["storage_charge_limit"].async_set_native_value(4000)
    await numbers["active_power_limit"].async_set_native_value(80)
    await hass.async_block_till_done()

    assert entry.runtime_data.raw_write_count == 1
    assert counter.native_value == 1
    assert "flash" in caplog.text
    assert sunspec_solaredge_client_mock.registers[0xF001] == 80


async def test_the_rearm_switch_writes_the_command_again_while_remote(
    hass, sunspec_solaredge_client_mock, freezer
):
    await _write_entry(hass)
    selects = _controls(hass, "select", RawBlockSelect)
    numbers = _controls(hass, "number", RawBlockNumber)
    rearm = _controls(hass, "switch", RawKeepAliveSwitch)["storage_command_rearm"]

    await selects["storage_control_mode"].async_select_option("remote_control")
    await numbers["storage_command_timeout"].async_set_native_value(3600)
    await selects["storage_command_mode"].async_select_option("charge_from_solar_and_grid")
    await hass.async_block_till_done()
    registers = sunspec_solaredge_client_mock.registers
    # The inverter forgets: the nightly restart puts the default back.
    registers[0xE00D] = 7
    registers[0xE00B] = 0
    await rearm.async_turn_on()
    await hass.async_block_till_done()
    assert rearm.is_on
    assert registers[0xE00D] == 3 and registers[0xE00B] == 0x0E10

    registers[0xE00D] = 7
    freezer.tick(REMOTE_COMMAND_REARM_SECONDS + 1)
    async_fire_time_changed(hass)
    # The interval fires the rewrite as a background task, which the
    # plain block_till_done does not wait for: on a slow runner the
    # write landed after the assertion.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registers[0xE00D] == 3

    # Out of remote control the rewrite stays quiet.
    await selects["storage_control_mode"].async_select_option("maximize_self_consumption")
    await hass.async_block_till_done()
    registers[0xE00D] = 7
    freezer.tick(REMOTE_COMMAND_REARM_SECONDS + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registers[0xE00D] == 7

    await rearm.async_turn_off()
    assert not rearm.is_on


async def test_other_vendors_read_no_raw_blocks(hass, sunspec_fronius_client_mock):
    entry = await _entry(hass)
    assert entry.runtime_data.raw_blocks == {}
    assert _sensors(hass) == {}
