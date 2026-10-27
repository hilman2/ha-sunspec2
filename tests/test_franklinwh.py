"""FranklinWH register decoding and battery commands through the real async SunSpec client."""

import asyncio
from typing import cast

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from modbus_connection import IllegalDataAddressError
from modbus_connection import IllegalDataValueError
from modbus_connection import ModbusTimeoutError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.const import CONF_ENABLED_MODELS
from custom_components.sunspec2.const import CONF_PREFIX
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.franklinwh_controls import FranklinBatteryButton
from custom_components.sunspec2.franklinwh_controls import FranklinBatteryState
from custom_components.sunspec2.franklinwh_controls import FranklinHomeLoad
from custom_components.sunspec2.franklinwh_controls import FranklinPowerNumber
from custom_components.sunspec2.franklinwh_controls import async_set_power
from custom_components.sunspec2.franklinwh_controls import battery_power
from custom_components.sunspec2.franklinwh_controls import power_limits
from custom_components.sunspec2.pysunspec2 import mb
from custom_components.sunspec2.sensor import SunSpecSensor
from custom_components.sunspec2.vendor_blocks import RawBlockSensor
from custom_components.sunspec2.vendors import profile_for
from custom_components.sunspec2.vendors.franklinwh import FRANKLINWH

from .const import MOCK_CONFIG
from .fake_unit import FakeConnection
from .fake_unit import FakeUnit
from .franklinwh_registers import agate_registers


@pytest.fixture(autouse=True)
def no_control_settling(mocker):
    for name in ("ENABLE_SETTLE_SECONDS", "VERIFY_SETTLE_SECONDS"):
        mocker.patch(f"custom_components.sunspec2.franklinwh_controls.{name}", 0)


@pytest.fixture
async def agate(hass, mocker):
    unit = FakeUnit(agate_registers())
    connection = FakeConnection({1: unit})
    mocker.patch.object(SunSpecApiClient, "_get_connection", return_value=connection)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG, CONF_ENABLED_MODELS: [701]},
        options={"scan_delay": 0, "model_read_delay": 0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, unit, connection


def _entities(hass, domain, cls):
    return [
        entity
        for entity in hass.data["entity_components"][domain].entities
        if isinstance(entity, cls)
    ]


@pytest.mark.parametrize(
    "manufacturer", ["FranklinWH", "FranklinWH Technologies Co., Ltd", " franklinwh "]
)
@pytest.mark.parametrize("model", ["aGate X", "aGate 1.3", " agate x "])
def test_agate_profile_matches_both_known_manufacturer_strings(manufacturer, model):
    assert profile_for(manufacturer, model) is FRANKLINWH


@pytest.mark.parametrize("manufacturer,model", [("Other", "aGate X"), ("FranklinWH", "Unknown")])
def test_other_devices_do_not_gain_franklin_register_reads(manufacturer, model):
    assert profile_for(manufacturer, model) is not FRANKLINWH


@pytest.mark.parametrize("prefix,name", [("", "aGate X Gateway"), ("Garage", "Garage Gateway")])
async def test_extension_sensors_move_to_a_gateway_device_and_keep_their_entity_ids(
    hass, mocker, prefix, name
):
    connection = FakeConnection({1: FakeUnit(agate_registers())})
    mocker.patch.object(SunSpecApiClient, "_get_connection", return_value=connection)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG, CONF_ENABLED_MODELS: [701], CONF_PREFIX: prefix},
        options={"scan_delay": 0, "model_read_delay": 0},
    )
    entry.add_to_hass(hass)
    # Up to 2026.10.12 the sensor sat on a SunSpec model device.
    devices = dr.async_get(hass)
    solar = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={cast(tuple[str, str], (DOMAIN, entry.entry_id, "inverter_mppt"))},
    )
    unique_id = get_sunspec_unique_id(entry.entry_id, "raw:franklinwh_extensions:native_mode", 0, 0)
    entities = er.async_get(hass)
    entities.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id,
        config_entry=entry,
        device_id=solar.id,
        suggested_object_id="agate_x_solar_module_native_operating_mode",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    gateway = devices.async_get_device_by_identifier(
        cast(tuple[str, str], (DOMAIN, entry.entry_id, "raw:agate")), entry.entry_id
    )
    assert gateway is not None
    assert (gateway.name, gateway.manufacturer, gateway.model) == (name, "FranklinWH", "aGate X")
    assert (gateway.serial_number, gateway.sw_version) == ("test-franklinwh", "V12R02B60D00")
    mode = entities.async_get("sensor.agate_x_solar_module_native_operating_mode")
    assert mode is not None and mode.device_id == gateway.id
    # Every extension sensor, including those disabled by default.
    moved = [
        registered
        for registered in er.async_entries_for_config_entry(entities, entry.entry_id)
        if "_raw:franklinwh_" in registered.unique_id
        or registered.unique_id.endswith("franklinwh:franklinwh_home_consumption-0-0")
    ]
    assert len(moved) == len(FRANKLINWH.raw_sensors) + 1
    assert {registered.device_id for registered in moved} == {gateway.id}


async def test_profile_reads_battery_models_without_changing_the_users_selection(hass, agate):
    entry, unit, _connection = agate
    coordinator = entry.runtime_data
    assert coordinator.vendor is FRANKLINWH
    assert coordinator.option_model_filter == {701}
    assert {702, 704, 713, 714}.issubset(coordinator.data)
    assert entry.data[CONF_ENABLED_MODELS] == [701]
    assert unit.writes == []

    state = _entities(hass, "sensor", FranklinBatteryState)[0]
    assert state.native_value == "charging"
    assert coordinator.data[713].getValue("SoC") == 32
    assert coordinator.data[713].getValue("SoH") == 94.8
    assert coordinator.data[714].getValue("Prt:0:DCW") == 0
    assert battery_power(coordinator.data[714]) == -100


async def test_extensions_decode_measured_powers_modes_and_reserves(hass, agate):
    entry, unit, _connection = agate
    sensors = {
        entity.translation_key: entity for entity in _entities(hass, "sensor", RawBlockSensor)
    }
    assert _entities(hass, "sensor", FranklinHomeLoad)[0].native_value == 185
    assert sensors["franklinwh_native_mode"].native_value == "self_consumption"
    assert sensors["franklinwh_active_reserve"].native_value == 8
    assert "franklinwh_self_reserve" not in sensors
    assert sensors["franklinwh_pv_proximal"].native_value == 300
    assert entry.runtime_data.raw_blocks["franklinwh_extensions"]["pv_energy"] == 15882870
    assert (15500, 14) in unit.reads
    assert (16000, 2) in unit.reads
    assert not FRANKLINWH.raw_numbers
    assert not FRANKLINWH.raw_selects
    assert not FRANKLINWH.raw_keepalives


@pytest.mark.parametrize("watts,words", [(5000, [0, 5000]), (-1500, [65535, 64036]), (0, [0, 0])])
async def test_command_batches_mode_and_watts_before_enabling(agate, watts, words):
    entry, unit, _connection = agate
    await async_set_power(entry.runtime_data, watts)

    assert unit.writes == [(319, [1, *words]), (318, [1])]
    assert entry.runtime_data.data[704].getValue("WSet") == watts
    assert entry.runtime_data.data[704].getValue("WSetPct") == 0


async def test_changing_an_enabled_command_never_cycles_or_rewrites_enable(agate):
    entry, unit, _connection = agate
    unit.holding[318] = 1
    await async_set_power(entry.runtime_data, 2000)

    assert unit.writes == [(319, [1, 0, 2000])]


async def test_standby_keeps_control_and_release_hands_it_back_in_one_step(hass, agate):
    entry, unit, _connection = agate
    buttons = {
        entity.translation_key: entity
        for entity in _entities(hass, "button", FranklinBatteryButton)
    }
    await buttons["franklinwh_standby"].async_press()
    assert unit.holding[318] == 1
    assert unit.holding[320] == unit.holding[321] == 0
    unit.writes.clear()

    await buttons["franklinwh_release"].async_press()

    assert unit.writes == [(318, [0]), (320, [0, 0]), (324, [0])]
    assert unit.holding[318] == 0


@pytest.mark.parametrize("watts", [5001, -5001, float("nan"), float("inf")])
async def test_invalid_targets_cannot_write(agate, watts):
    entry, unit, _connection = agate
    with pytest.raises(HomeAssistantError):
        await async_set_power(entry.runtime_data, watts)
    assert unit.writes == []


async def test_bounds_use_each_direction_instead_of_a_fixed_five_kw_limit(hass, agate):
    entry, unit, _connection = agate
    unit.holding[235] = 3000
    unit.holding[236] = 7000
    await entry.runtime_data.async_refresh()
    number = _entities(hass, "number", FranklinPowerNumber)[0]
    assert (number.native_min_value, number.native_max_value) == (-3000, 7000)
    await number.async_set_native_value(6500)
    assert unit.writes == [(319, [1, 0, 6500]), (318, [1])]


async def test_hundred_watt_ratings_reach_sensors_and_directional_control_limits(hass, agate):
    entry, unit, _connection = agate
    unit.holding.update({227: 19200, 235: 160, 236: 192})
    await entry.runtime_data.async_refresh()
    capacity = entry.runtime_data.data[702]
    assert capacity.getValue("WMaxRtg") == 19200
    assert capacity.getValue("WChaRteMaxRtg") == 16000
    assert capacity.getValue("WDisChaRteMaxRtg") == 19200
    assert capacity.getPoint("WChaRteMaxRtg").value == 160
    assert capacity.getPoint("WDisChaRteMaxRtg").value == 192
    sensors = {
        sensor.key: sensor.native_value
        for sensor in _entities(hass, "sensor", SunSpecSensor)
        if sensor.model_id == 702
    }
    assert sensors["WChaRteMaxRtg"] == 16000
    assert sensors["WDisChaRteMaxRtg"] == 19200
    number = _entities(hass, "number", FranklinPowerNumber)[0]
    assert (number.native_min_value, number.native_max_value) == (-16000, 19200)

    with pytest.raises(HomeAssistantError, match="exceeds the reported range"):
        await async_set_power(entry.runtime_data, -16001)
    assert unit.writes == []

    await hass.services.async_call(
        "number", "set_value", {"entity_id": number.entity_id, "value": -16000}, blocking=True
    )
    assert unit.writes == [(319, [1, 65535, 49536]), (318, [1])]
    await number.async_set_native_value(19200)
    assert unit.writes[-1] == (319, [1, 0, 19200])

    await entry.runtime_data.async_refresh()
    assert power_limits(entry.runtime_data.data[702]) == (16000, 19200)


@pytest.mark.parametrize("rating", [0, 65535])
async def test_missing_ratings_still_fall_back_to_the_active_power_rating(hass, agate, rating):
    entry, unit, _connection = agate
    unit.holding.update({227: 19200, 235: rating, 236: rating})
    await entry.runtime_data.async_refresh()
    number = _entities(hass, "number", FranklinPowerNumber)[0]
    assert (number.native_min_value, number.native_max_value) == (-19200, 19200)
    unit.holding[227] = 500
    await entry.runtime_data.async_refresh()
    assert power_limits(entry.runtime_data.data[702]) is None
    assert hass.states.get(number.entity_id).state == "unavailable"


async def test_ratings_already_scaled_by_sunspec_are_not_multiplied_again(agate):
    entry, unit, _connection = agate
    unit.holding.update({227: 192, 235: 160, 236: 192, 270: 2})
    entry.runtime_data.api.reconnect_next(rescan=False)
    await entry.runtime_data.async_refresh()
    capacity = entry.runtime_data.data[702]
    assert capacity.getValue("WMaxRtg") == 19200
    assert power_limits(capacity) == (16000, 19200)


@pytest.mark.parametrize("manufacturer,model", [("Other", "aGate X"), ("FranklinWH", "Other")])
async def test_short_ratings_of_other_devices_keep_their_sunspec_values(agate, manufacturer, model):
    entry, unit, _connection = agate
    for address, text in ((4, manufacturer), (20, model)):
        data = mb.str_to_data(text, 32)
        unit.holding.update(
            {
                address + offset // 2: int.from_bytes(data[offset : offset + 2], "big")
                for offset in range(0, len(data), 2)
            }
        )
    unit.holding.update({227: 19200, 235: 160, 236: 192})
    await entry.runtime_data.api.async_get_data(1)
    capacity = await entry.runtime_data.api.async_get_data(702)
    assert capacity.getValue("WMaxRtg") == 19200
    assert capacity.getValue("WChaRteMaxRtg") == 160
    assert capacity.getValue("WDisChaRteMaxRtg") == 192


async def test_acknowledged_but_ignored_commands_report_failure(agate, mocker):
    entry, unit, _connection = agate

    async def acknowledge(address, values):
        unit.writes.append((address, list(values)))

    mocker.patch.object(unit, "write_registers", side_effect=acknowledge)
    with pytest.raises(HomeAssistantError, match="readback differs"):
        await async_set_power(entry.runtime_data, 1000)
    assert len(unit.writes) == 2
    assert not entry.runtime_data._gateway_lock.locked()


async def test_device_refusal_is_not_retried(agate, mocker):
    entry, unit, connection = agate
    connects = connection.connects
    writer = mocker.patch.object(unit, "write_registers", side_effect=IllegalDataValueError())

    with pytest.raises(HomeAssistantError):
        await async_set_power(entry.runtime_data, -500)

    assert writer.await_count == 1
    assert connection.connects == connects
    assert not entry.runtime_data._gateway_lock.locked()


@pytest.mark.parametrize("stage", ["initial", "capacity", "verification"])
@pytest.mark.parametrize("error", [ModbusTimeoutError, IllegalDataAddressError])
async def test_command_read_errors_reach_the_ui_as_home_assistant_errors(
    agate, mocker, stage, error
):
    """A read can fail before dispatch or after the target has already been enabled."""
    entry, unit, connection = agate
    read = unit.read_holding_registers

    async def fail_at_stage(address, count):
        should_fail = (
            (stage == "initial" and address == 296 and not unit.writes)
            or (stage == "capacity" and address == 225)
            or (stage == "verification" and address == 296 and unit.holding[318] == 1)
        )
        if should_fail:
            raise error()
        return await read(address, count)

    mocker.patch.object(unit, "read_holding_registers", side_effect=fail_at_stage)
    with pytest.raises(HomeAssistantError, match="FranklinWH battery command failed: Modbus"):
        await async_set_power(entry.runtime_data, 1000)

    expected = [(319, [1, 0, 1000]), (318, [1])] if stage == "verification" else []
    assert unit.writes == expected
    assert not entry.runtime_data._gateway_lock.locked()
    assert connection.disconnects > 0


async def test_all_reads_writes_and_verification_share_one_lock_hold(agate, mocker):
    entry, _unit, _connection = agate
    coordinator = entry.runtime_data
    read = coordinator.api.read
    write = coordinator.api.async_write_points
    observations = []

    async def read_locked(model):
        observations.append(coordinator._gateway_lock.locked())
        return await read(model)

    async def write_locked(model, points):
        observations.append(coordinator._gateway_lock.locked())
        return await write(model, points)

    mocker.patch.object(coordinator.api, "read", side_effect=read_locked)
    mocker.patch.object(coordinator.api, "async_write_points", side_effect=write_locked)
    refresh = mocker.patch.object(coordinator, "async_request_refresh")
    await async_set_power(coordinator, 1000)

    assert observations and all(observations)
    assert not coordinator._gateway_lock.locked()
    refresh.assert_not_awaited()


async def test_failed_cleanup_still_releases_the_gateway_lock(agate, mocker):
    entry, _unit, _connection = agate
    coordinator = entry.runtime_data
    mocker.patch.object(coordinator.api, "async_close", side_effect=DeviceError("cleanup failed"))
    with pytest.raises(DeviceError, match="cleanup failed"):
        async with coordinator.async_write_transaction():
            raise DeviceError("write failed")
    assert not coordinator._gateway_lock.locked()


async def test_zero_home_load_does_not_fall_back_to_an_older_coarse_reading(hass, agate):
    entry, unit, _connection = agate
    unit.holding[16000] = 0
    unit.holding[15506] = 600
    await entry.runtime_data.async_refresh()
    sensor = _entities(hass, "sensor", FranklinHomeLoad)[0]
    assert sensor.native_value == 0

    entry.runtime_data.raw_blocks.pop("franklinwh_high_resolution")
    assert sensor.native_value == 600


async def test_home_load_keeps_the_100_w_reading_while_16000_moves_in_1000_w_steps(hass, agate):
    # The pairs follow the history of an aGate X reported as V10R11B40D00 (issue #149).
    entry, unit, _connection = agate
    sensor = _entities(hass, "sensor", FranklinHomeLoad)[0]
    sensor._high_resolution_proven = False
    for fine, coarse in ((1000, 1100), (0, 900), (6000, 6500)):
        unit.holding.update({16000: fine, 15506: coarse})
        await entry.runtime_data.async_refresh()
        assert sensor.native_value == coarse

    unit.holding.update({16000: 1004, 15506: 1000})
    await entry.runtime_data.async_refresh()
    assert sensor.native_value == 1004
    unit.holding.update({16000: 2000, 15506: 1900})
    await entry.runtime_data.async_refresh()
    assert sensor.native_value == 2000

    # Without the 100 W reading, 16000 still answers, proven or not.
    sensor._high_resolution_proven = False
    entry.runtime_data.raw_blocks.pop("franklinwh_extensions")
    assert sensor.native_value == 2000


async def test_solar_powers_below_zero_read_as_negative_watts(hass, agate):
    entry, unit, _connection = agate
    unit.holding.update({15502: 65534, 15503: 0xFFFF, 15504: 65000, 15505: 65535})
    await entry.runtime_data.async_refresh()
    block = entry.runtime_data.raw_blocks["franklinwh_extensions"]
    powers = ("pv_total", "pv_proximal", "pv_remote_1", "pv_remote_2")
    assert [block[name] for name in powers] == [-2, -1, -536, -1]


async def test_unknown_native_modes_and_unavailable_power_stay_unknown(hass, agate):
    entry, unit, _connection = agate
    unit.holding[15507] = 4
    await entry.runtime_data.async_refresh()
    modes = {sensor.translation_key: sensor for sensor in _entities(hass, "sensor", RawBlockSensor)}
    assert modes["franklinwh_native_mode"].native_value is None

    entry.runtime_data.data[714].getPoint("DCW").value = None
    assert _entities(hass, "sensor", FranklinBatteryState)[0].native_value is None


async def test_aggregate_battery_power_does_not_add_the_ports_again(agate):
    entry, _unit, _connection = agate
    wrapper = entry.runtime_data.data[714]
    wrapper.getPoint("DCW").value = -100
    wrapper.getPoint("Prt:0:DCW").value = -100
    assert battery_power(wrapper) == -100


async def test_battery_activity_follows_the_inverter_state_before_the_deadband(hass, agate):
    entry, _unit, _connection = agate
    data = entry.runtime_data.data
    state = _entities(hass, "sensor", FranklinBatteryState)[0]
    # Issue #149: DCW stood at 100 W, one step of its resolution, while
    # InvSt reported STANDBY.
    data[714].getPoint("DCW").value = 100
    data[701].getPoint("InvSt").value = 7
    assert state.native_value == "idle"

    data[701].getPoint("InvSt").value = 3
    assert state.native_value == "discharging"

    del data[701]
    data[714].getPoint("DCW").value = 40
    assert state.native_value == "idle"


async def test_cancelled_verification_releases_the_gateway_and_drops_the_session(agate, mocker):
    entry, _unit, connection = agate
    coordinator = entry.runtime_data
    read = coordinator.api.read
    verifying = asyncio.Event()
    blocked = asyncio.Event()
    control_reads = 0

    async def pause_verification(model):
        nonlocal control_reads
        if model == 704:
            control_reads += 1
            if control_reads == 2:
                verifying.set()
                await blocked.wait()
        return await read(model)

    mocker.patch.object(coordinator.api, "read", side_effect=pause_verification)
    pending = asyncio.create_task(async_set_power(coordinator, 1000))
    try:
        async with asyncio.timeout(1):
            await verifying.wait()
        assert coordinator._gateway_lock.locked()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not coordinator._gateway_lock.locked()
        assert connection.disconnects > 0
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
