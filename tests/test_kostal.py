"""The Kostal profile: the register blocks, the battery device and the sensors.

The entity tests run against tests/test_data/inverter_kostal.json plus
the register images in tests/kostal_registers.py.
"""

from typing import cast

from homeassistant.helpers import device_registry as dr

from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.const import default_models_for
from custom_components.sunspec2.raw_blocks import decode_block
from custom_components.sunspec2.vendor_blocks import RawBlockSensor
from custom_components.sunspec2.vendors import profile_for
from custom_components.sunspec2.vendors.kostal import KOSTAL
from custom_components.sunspec2.vendors.kostal import RAW_BLOCKS
from custom_components.sunspec2.vendors.kostal import as_option

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .kostal_registers import plenticore_registers

# ---------- the profile ------------------------------------------------------


def test_the_manufacturer_string_picks_the_profile():
    assert profile_for("KOSTAL") is KOSTAL
    assert profile_for("KOSTAL Solar Electric GmbH") is KOSTAL
    assert profile_for("SolarEdge ") is not KOSTAL


def test_every_field_decodes_at_the_address_the_interface_description_gives():
    """Each block against the register image, so an offset a register off shows up.

    The offsets in the profile are counted from the block's first
    register, the interface description numbers them absolutely. This
    is where the two are checked against each other.
    """
    registers = plenticore_registers()
    decoded = {}
    for block in RAW_BLOCKS:
        words = [registers.get(block.address + index) for index in range(block.count)]
        assert None not in words, f"{block.key}: the image is short of {block.count} registers"
        data = b"".join(int(word).to_bytes(2, "big") for word in words)
        decoded[block.key] = decode_block(block, data)

    assert decoded["inverter"] == {
        "controller_temperature": 41.5,
        "total_dc_power": 3300.0,
        "energy_manager_state": 0,
        "home_consumption_battery": 2900.0,
        "home_consumption_grid": 150.0,
        "home_consumption_battery_total": 1500000.0,
        "home_consumption_grid_total": 2400000.0,
        "home_consumption_pv_total": 5100000.0,
        "home_consumption_pv": 450.0,
        "home_consumption_total": 9000000.0,
        "isolation_resistance": 15000.0,
        "power_limit_evu": 70.0,
    }
    assert decoded["battery_info"] == {
        "gross_capacity": 190,
        "manufacturer": "BYD",
        "serial": 30412,
        "firmware": 0x0107,
        "battery_type": 0x0004,
    }
    assert decoded["battery_state"] == {
        "pssb_fuse_state": 1.0,
        "ready_flag": 1.0,
        "temperature": 21.5,
    }
    assert decoded["battery_energy"] == {
        "dc_charge_energy": 3100000.0,
        "dc_discharge_energy": 2800000.0,
        "ac_charge_energy": 2900000.0,
        "ac_discharge_energy": 41000.0,
        "ac_charge_energy_grid": 120000.0,
        "energy_to_grid": 6400000.0,
    }
    assert decoded["battery_limits"] == {
        "work_capacity": 10240.0,
        "max_charge_power": 7000.0,
        "max_discharge_power": 7000.0,
        "management_mode": 0x02,
        "sensor_type": 0x03,
    }


def test_an_enum_kostal_puts_in_a_float_register_reaches_its_option_map():
    """``options`` looks up by int, and the fuse state arrives as 1.0."""
    assert as_option(1.0) == 1
    assert as_option(255.0) == 255
    assert as_option(2) == 2
    assert as_option(None) is None


def test_a_plenticore_serving_both_inverter_halves_gets_one_of_them():
    """Kostal answers 103 and 113; ticking both would build every reading twice."""
    ticked = default_models_for({1, 103, 113, 160, 802})
    assert 103 in ticked
    assert 113 not in ticked


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
    identifier = cast(tuple[str, str], (DOMAIN, entry.entry_id, f"raw:{key}"))
    return dr.async_get(hass).async_get_device(identifiers={identifier})


async def test_a_plenticore_reads_every_block(hass, sunspec_kostal_client_mock):
    entry = await _entry(hass)
    coordinator = entry.runtime_data
    assert coordinator.vendor is KOSTAL
    assert set(coordinator.raw_blocks) == {
        "inverter",
        "battery_info",
        "battery_state",
        "battery_energy",
        "battery_limits",
    }


async def test_the_house_consumption_split_lands_on_the_inverter(hass, sunspec_kostal_client_mock):
    """The three sources of the house's power, which no SunSpec model carries."""
    entry = await _entry(hass)
    sensors = _sensors(hass)
    assert sensors[_uid(entry, "inverter", "home_consumption_pv")].native_value == 450.0
    assert sensors[_uid(entry, "inverter", "home_consumption_battery")].native_value == 2900.0
    assert sensors[_uid(entry, "inverter", "home_consumption_grid")].native_value == 150.0
    assert sensors[_uid(entry, "inverter", "home_consumption_total")].native_value == 9000000.0
    assert sensors[_uid(entry, "inverter", "energy_manager_state")].native_value == "idle"
    assert sensors[_uid(entry, "inverter", "power_limit_evu")].native_value == 70.0

    pv = sensors[_uid(entry, "inverter", "home_consumption_pv")]
    assert (DOMAIN, entry.entry_id, "inverter_three_phase") in pv.device_info["identifiers"]


async def test_the_battery_is_a_device_named_after_its_type(hass, sunspec_kostal_client_mock):
    """Kostal reports which battery is fitted as a number; 0x0004 is the BYD."""
    entry = await _entry(hass)
    device = _device(hass, entry, "battery")
    assert device is not None
    assert device.manufacturer == "BYD"
    assert device.model == "BYD"
    assert device.serial_number == "30412"
    assert device.name == "PLENTICORE plus 10 Battery"


async def test_the_battery_gets_the_readings_sunspec_leaves_out(hass, sunspec_kostal_client_mock):
    """Model 802 has no temperature field, and no counter that separates the sources."""
    entry = await _entry(hass)
    sensors = _sensors(hass)
    assert sensors[_uid(entry, "battery_state", "temperature")].native_value == 21.5
    assert sensors[_uid(entry, "battery_state", "pssb_fuse_state")].native_value == "fuse_ok"
    assert sensors[_uid(entry, "battery_state", "ready_flag")].native_value == "ready"
    assert sensors[_uid(entry, "battery_energy", "ac_charge_energy_grid")].native_value == 120000.0
    assert sensors[_uid(entry, "battery_energy", "dc_charge_energy")].native_value == 3100000.0
    assert sensors[_uid(entry, "battery_limits", "management_mode")].native_value == "modbus"
    assert sensors[_uid(entry, "battery_limits", "sensor_type")].native_value == "ksem"


async def test_an_inverter_without_a_battery_gets_no_battery_entities(
    hass, sunspec_kostal_no_battery_client_mock
):
    """Register 588 reads 0, so the gated blocks never answer and the device is not built."""
    entry = await _entry(hass)
    coordinator = entry.runtime_data
    assert set(coordinator.raw_blocks) == {"inverter", "battery_info"}
    sensors = _sensors(hass)
    assert _uid(entry, "battery_state", "temperature") not in sensors
    assert _uid(entry, "battery_energy", "dc_charge_energy") not in sensors
    assert _device(hass, entry, "battery") is None
    # The inverter's own block is untouched by the gate.
    assert sensors[_uid(entry, "inverter", "home_consumption_pv")].native_value == 450.0
