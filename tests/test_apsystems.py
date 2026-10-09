"""The APsystems profile: the DC floats behind the end marker, read from their fixed addresses."""

from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers import entity_registry as er

from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.const import CONF_WRITE_BETA_ENABLED
from custom_components.sunspec2.sensor import SunSpecEnergySensor
from custom_components.sunspec2.sensor import SunSpecSensor
from custom_components.sunspec2.vendor_blocks import RawBlockSensor
from custom_components.sunspec2.vendors import profile_for
from custom_components.sunspec2.vendors.apsystems import APSYSTEMS

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .apsystems_registers import ds3_registers
from .const import MOCK_CONFIG
from .test_number import _live_entities


def test_apsystems_is_matched_by_its_manufacturer_name():
    assert profile_for("APsystems") is APSYSTEMS
    assert profile_for("Fronius") is not APSYSTEMS


def test_the_dc_block_sits_where_the_allocation_table_puts_the_floats():
    (block,) = APSYSTEMS.raw_blocks
    assert (block.address, block.count) == (40214, 48)
    offsets = {field.name: field.offset for field in block.fields}
    # DCV1 40214, DCA1 40230, DCW1 40246, two registers per value.
    assert offsets["dcv1"] == 0
    assert offsets["dca1"] == 40230 - 40214
    assert offsets["dcw1"] == 40246 - 40214
    assert offsets["dcw8"] == 40260 - 40214


async def _entry(hass):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    client = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)
    return entry


async def test_the_dc_inputs_of_a_micro_inverter_become_sensors(
    hass, sunspec_apsystems_client_mock
):
    entry = await _entry(hass)
    coordinator = entry.runtime_data

    assert coordinator.vendor is APSYSTEMS
    assert coordinator.raw_blocks["apsystems_dc"]["dcv2"] > 34.0

    component = hass.data["entity_components"]["sensor"]
    sensors = {e.translation_key: e for e in component.entities if isinstance(e, RawBlockSensor)}
    assert round(sensors["apsystems_dc_voltage_1"].native_value, 1) == 33.5
    assert round(sensors["apsystems_dc_current_2"].native_value, 2) == 0.85
    assert round(sensors["apsystems_dc_power_1"].native_value, 1) == 30.2

    # Inputs 3 to 8 read 0.0 on a DS3, so they are there to turn on, not on.
    registry = er.async_get(hass)
    for field, expected in (("dcv2", True), ("dcv3", False)):
        unique_id = get_sunspec_unique_id(entry.entry_id, f"raw:apsystems_dc:{field}", 0, 0)
        entity_id = registry.async_get_entity_id("sensor", "sunspec2", unique_id)
        assert entity_id is not None
        entry_for_sensor = registry.async_get(entity_id)
        assert entry_for_sensor is not None
        assert (entry_for_sensor.disabled_by is None) is expected


async def test_an_ecu_that_does_not_serve_the_block_gets_no_dc_sensors(
    hass, sunspec_apsystems_client_mock
):
    sunspec_apsystems_client_mock.registers = ds3_registers()
    del sunspec_apsystems_client_mock.registers[40214]
    entry = await _entry(hass)

    assert "apsystems_dc" not in entry.runtime_data.raw_blocks


async def _controls_entry(hass, setup=True):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    hass.config_entries.async_update_entry(entry, options={CONF_WRITE_BETA_ENABLED: True})
    if setup:
        client = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
        await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)
    return entry


async def test_only_the_controls_the_ecu_implements_are_built(
    hass, sunspec_apsystems_controls_client_mock
):
    await _controls_entry(hass)

    numbers = {e._point_name: e for e in _live_entities(hass, "number")}
    switches = {e._point_name for e in _live_entities(hass, "switch")}

    # No revert time, no power factor, and Conn is there to be turned on.
    assert set(numbers) == {"WMaxLimPct"}
    assert switches == {"WMaxLim_Ena"}
    # 30.0 % read with a scale factor of -1 would have offered tenths.
    assert numbers["WMaxLimPct"].native_step == 1.0
    assert numbers["WMaxLimPct"].native_value == 30.0


async def test_the_enable_flag_reads_back_as_the_ascii_digit(
    hass, sunspec_apsystems_controls_client_mock
):
    entry = await _controls_entry(hass)
    switch = next(e for e in _live_entities(hass, "switch") if e._point_name == "WMaxLim_Ena")

    # The fixture holds 49, ord("1").
    assert switch.is_on is True

    entry.runtime_data.data[123].getPoint("WMaxLim_Ena").value = 48
    assert switch.is_on is False


async def test_controls_the_ecu_lacks_are_removed_from_the_registry(
    hass, sunspec_apsystems_controls_client_mock
):
    entry = await _controls_entry(hass, setup=False)
    registry = er.async_get(hass)
    stale = {}
    for domain, point in (("number", "WMaxLimPct_RvrtTms"), ("switch", "OutPFSet_Ena")):
        unique_id = get_sunspec_unique_id(entry.entry_id, point, 123, 0)
        stale[point] = registry.async_get_or_create(
            domain, "sunspec2", unique_id, config_entry=entry
        ).entity_id

    client = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)

    assert all(registry.async_get(entity_id) is None for entity_id in stale.values())


async def test_the_daily_yield_is_not_held_like_a_lifetime_counter(
    hass, sunspec_apsystems_client_mock
):
    entry = await _entry(hass)
    component = hass.data["entity_components"]["sensor"]
    wh = next(e for e in component.entities if isinstance(e, SunSpecSensor) and e.key == "WH")

    assert not isinstance(wh, SunSpecEnergySensor)
    assert wh.state_class == SensorStateClass.TOTAL_INCREASING
    assert wh.translation_key == "daily_energy"
    assert wh.name == "Energy produced today"
    state = hass.states.get(wh.entity_id)
    assert state is not None
    assert state.attributes["friendly_name"].endswith(" Energy produced today")

    # Every morning starts at zero. A lifetime guard would hold yesterday's value.
    entry.runtime_data.data[103].getPoint("WH").value = 0
    assert wh.native_value == 0


async def test_the_daily_energy_name_keeps_the_registered_entity_id(
    hass, sunspec_apsystems_client_mock
):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    registry = er.async_get(hass)
    original = registry.async_get_or_create(
        "sensor",
        "sunspec2",
        get_sunspec_unique_id(entry.entry_id, "WH", 103, 0),
        config_entry=entry,
        suggested_object_id="ds3_inverter_three_phase_watthours",
    )
    client = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)

    component = hass.data["entity_components"]["sensor"]
    wh = next(e for e in component.entities if isinstance(e, SunSpecSensor) and e.key == "WH")
    assert wh.entity_id == original.entity_id
    assert wh.name == "Energy produced today"
    registered = registry.async_get(wh.entity_id)
    assert registered is not None
    assert registered.original_name == "Energy produced today"
