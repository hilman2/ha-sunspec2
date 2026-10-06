"""The APsystems profile: the DC floats behind the end marker, read from their fixed addresses."""

from homeassistant.helpers import entity_registry as er

from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.vendor_blocks import RawBlockSensor
from custom_components.sunspec2.vendors import profile_for
from custom_components.sunspec2.vendors.apsystems import APSYSTEMS

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .apsystems_registers import ds3_registers
from .const import MOCK_CONFIG


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
