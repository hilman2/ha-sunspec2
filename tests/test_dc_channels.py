"""The role-named model 160 sensors when the device reports less than the fixtures do.

tests/test_fronius.py and tests/test_fronius_datamanager.py have the
working path. The GEN24 fixture carries "MPPT 1" at 900 W, "MPPT 2" at
920 W, "ST CHA" idle and "ST DISCHA" at 2000 W. The Symo Hybrid fixture
carries "String 1" at 1500 W and "String 2", the battery, at 800 W
unsigned, with ChaSt 3 (discharging) in model 124.
"""

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.sunspec2.dc_channels import BatteryDirectionSensor
from custom_components.sunspec2.dc_channels import DcChannelSensor
from custom_components.sunspec2.dc_channels import PvPowerSensor
from custom_components.sunspec2.sensor import SunSpecSensor

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG_WRITE


async def _entry(hass):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _entities(hass, platform, cls):
    component = hass.data.get("entity_components", {}).get(platform)
    return [e for e in component.entities if isinstance(e, cls)] if component else []


def _channels(hass):
    return {s.translation_key: s for s in _entities(hass, "sensor", DcChannelSensor)}


def _modules(client):
    return client.models[160][0].groups["module"]


# ---------- which modules get a role -----------------------------------------


def _forget_the_module_count(client):
    client.models[160][0].N.value = None


def _relabel_every_module(client):
    for module in _modules(client):
        module.IDStr.value = "not supported"


@pytest.mark.parametrize("change", [_forget_the_module_count, _relabel_every_module])
async def test_a_model_160_the_vendor_cannot_read_roles_from_gets_no_role_sensors(
    hass, sunspec_fronius_client_mock, change
):
    """Without N there is no telling how many modules are real; without a known label, what they are."""
    change(sunspec_fronius_client_mock)

    await _entry(hass)

    assert _entities(hass, "sensor", DcChannelSensor) == []
    assert _entities(hass, "sensor", PvPowerSensor) == []
    # The generic "Module n" sensors do not depend on a role.
    assert _entities(hass, "sensor", SunSpecSensor)


async def test_a_module_without_a_label_is_left_out_and_the_others_keep_their_roles(
    hass, sunspec_fronius_client_mock
):
    """Module 2 is "ST CHA" in the fixture; here its label register reports nothing."""
    _modules(sunspec_fronius_client_mock)[2].IDStr.value = None

    await _entry(hass)

    by_key = _channels(hass)
    assert set(by_key) == {"battery_discharge_power", "battery_discharged_energy"}
    assert by_key["battery_discharge_power"].native_value == 2000
    pv = _entities(hass, "sensor", PvPowerSensor)
    assert len(pv) == 1
    assert pv[0].extra_state_attributes["modules"] == [0, 1]


# ---------- PV power ----------------------------------------------------------


async def test_pv_power_leaves_out_a_string_that_reports_nothing(hass, sunspec_fronius_client_mock):
    entry = await _entry(hass)
    coordinator = entry.runtime_data
    pv = _entities(hass, "sensor", PvPowerSensor)[0]
    assert pv.native_value == 1820

    _modules(sunspec_fronius_client_mock)[1].DCW.value = None
    await coordinator.async_refresh()
    assert pv.native_value == 900

    _modules(sunspec_fronius_client_mock)[0].DCW.value = None
    await coordinator.async_refresh()
    assert pv.native_value is None


async def test_pv_power_reads_nothing_once_the_device_stops_listing_model_160(
    hass, sunspec_fronius_client_mock
):
    """A firmware update can take a model away; the next scan then leaves it out of the data."""
    entry = await _entry(hass)
    coordinator = entry.runtime_data
    pv = _entities(hass, "sensor", PvPowerSensor)[0]
    assert pv.native_value == 1820

    del sunspec_fronius_client_mock.models[160]
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert 160 not in coordinator.data
    assert pv.native_value is None


# ---------- one battery module, direction from the storage model -------------


async def _symo_hybrid_battery(hass):
    entry = await _entry(hass)
    by_key = _channels(hass)
    assert all(isinstance(sensor, BatteryDirectionSensor) for sensor in by_key.values())
    return entry.runtime_data, by_key["battery_charge_power"], by_key["battery_discharge_power"]


def _forget_the_charge_state(client):
    client.models[124][0].ChaSt.value = None


def _drop_the_storage_model(client):
    del client.models[124]


@pytest.mark.parametrize("change", [_forget_the_charge_state, _drop_the_storage_model])
async def test_battery_power_has_no_direction_without_a_charge_state(
    hass, sunspec_symo_hybrid_client_mock, change
):
    """800 W with nothing to say which way is no reading; 0 W is at rest either way."""
    coordinator, charge, discharge = await _symo_hybrid_battery(hass)
    assert discharge.native_value == 800
    assert charge.native_value == 0

    change(sunspec_symo_hybrid_client_mock)
    await coordinator.async_refresh()
    assert coordinator.last_update_success

    assert charge.native_value is None
    assert discharge.native_value is None

    _modules(sunspec_symo_hybrid_client_mock)[1].DCW.value = 0
    await coordinator.async_refresh()

    assert charge.native_value == 0
    assert discharge.native_value == 0


async def test_battery_power_follows_the_charge_state_the_device_reports(
    hass, sunspec_symo_hybrid_client_mock
):
    """ChaSt 4 is charging, 3 discharging; in any other state nothing moves."""
    coordinator, charge, discharge = await _symo_hybrid_battery(hass)
    storage = sunspec_symo_hybrid_client_mock.models[124][0]

    storage.ChaSt.value = 4
    await coordinator.async_refresh()
    assert charge.native_value == 800
    assert discharge.native_value == 0

    storage.ChaSt.value = 6
    await coordinator.async_refresh()
    assert charge.native_value == 0
    assert discharge.native_value == 0


async def test_a_battery_module_that_reports_no_power_reads_nothing_in_either_direction(
    hass, sunspec_symo_hybrid_client_mock
):
    coordinator, charge, discharge = await _symo_hybrid_battery(hass)

    _modules(sunspec_symo_hybrid_client_mock)[1].DCW.value = None
    await coordinator.async_refresh()

    assert charge.native_value is None
    assert discharge.native_value is None
