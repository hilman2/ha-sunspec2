"""The Datamanager 2.0 generation of Fronius: Symo, Primo, Eco, Galvo and Symo Hybrid.

The entity tests run against tests/test_data/inverter_fronius_symo_hybrid.json,
a Symo Hybrid 5.0-3-S with its battery discharging.
"""

from dataclasses import replace
from unittest.mock import patch

import pytest
import voluptuous as vol

from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.const import CONF_REARM_ON_CHANGE
from custom_components.sunspec2.const import CONF_WRITE_BETA_ENABLED
from custom_components.sunspec2.dc_channels import BatteryDirectionSensor
from custom_components.sunspec2.dc_channels import DcChannelSensor
from custom_components.sunspec2.dc_channels import PvPowerSensor
from custom_components.sunspec2.storage_modes import StorageModeSelect
from custom_components.sunspec2.storage_modes import StorageSetpointNumber
from custom_components.sunspec2.vendors import profile_for
from custom_components.sunspec2.vendors.fronius import FRONIUS
from custom_components.sunspec2.vendors.fronius_datamanager import FRONIUS_DATAMANAGER
from custom_components.sunspec2.vendors.fronius_datamanager import is_datamanager
from custom_components.sunspec2.vendors.fronius_datamanager import module_role
from custom_components.sunspec2.vendors.profile import ModuleRole
from custom_components.sunspec2.vendors.profile import Rate

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG_STEP_1
from .const import MOCK_CONFIG_WRITE
from .test_fronius import _set_export_limit

# ---------- telling the generations apart -----------------------------------


@pytest.mark.parametrize(
    ("model", "option", "version", "expected"),
    [
        ("Symo 8.2-3-M", "3.10.2-1", "0.3.13.4", FRONIUS_DATAMANAGER),
        ("Symo 5.0-3-M", "3.31.1-7", "0.3.30.1", FRONIUS_DATAMANAGER),
        ("Symo Hybrid 5.0-3-S", "1.31.1-5", "0.3.30.0", FRONIUS_DATAMANAGER),
        ("Primo 5.0-1", "3.16.6-1", "", FRONIUS_DATAMANAGER),
        ("Galvo 1.5-1", "", "0.3.13.4", FRONIUS_DATAMANAGER),
        ("Symo GEN24 10.0 Plus", "", "1.40.7-1", FRONIUS),
        ("Primo GEN24 6.0", "", "1.41.11-1", FRONIUS),
        ("Tauro 50-3-D", "", "1.30.7-1", FRONIUS),
        ("Verto 15.0", "", "1.36.5-1", FRONIUS),
        # A device that says nothing about itself keeps the profile
        # every Fronius had before the split.
        ("", "", "", FRONIUS),
    ],
)
def test_the_generation_is_told_from_the_common_model(model, option, version, expected):
    assert profile_for("Fronius", model, option, version) is expected
    assert is_datamanager(model, option, version) is (expected is FRONIUS_DATAMANAGER)


def test_the_manufacturer_alone_still_finds_the_gen24_profile():
    assert profile_for("Fronius") is FRONIUS
    assert profile_for("Fronius International GmbH") is FRONIUS


@pytest.mark.parametrize(
    ("label", "model", "role"),
    [
        ("String 1", "Symo Hybrid 5.0-3-S", ModuleRole.PV),
        ("String 2", "Symo Hybrid 5.0-3-S", ModuleRole.BATTERY),
        ("String 1", "Symo 8.2-3-M", ModuleRole.PV),
        ("String 2", "Symo 8.2-3-M", ModuleRole.PV),
        ("not supported", "Primo 5.0-1", None),
        ("MPPT 1", "Symo 8.2-3-M", None),
        ("", "Symo 8.2-3-M", None),
    ],
)
def test_module_role_reads_the_string_labels(label, model, role):
    assert module_role(label, model) is role


# ---------- entities on a Symo Hybrid ---------------------------------------


async def _entry(hass, options=None):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    if options is not None:
        hass.config_entries.async_update_entry(entry, options=options)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    return entry


def _entities(hass, platform, cls):
    component = hass.data.get("entity_components", {}).get(platform)
    return [e for e in component.entities if isinstance(e, cls)] if component else []


async def test_string_2_is_the_battery_and_chast_gives_the_direction(
    hass, sunspec_symo_hybrid_client_mock
):
    """String 1 at 1500 W is PV; String 2 at 800 W is the battery, discharging (ChaSt 3)."""
    entry = await _entry(hass)
    assert entry.runtime_data.vendor is FRONIUS_DATAMANAGER

    by_key = {s.translation_key: s for s in _entities(hass, "sensor", DcChannelSensor)}
    assert set(by_key) == {"battery_charge_power", "battery_discharge_power"}
    assert all(isinstance(sensor, BatteryDirectionSensor) for sensor in by_key.values())
    assert by_key["battery_discharge_power"].native_value == 800
    assert by_key["battery_charge_power"].native_value == 0
    assert by_key["battery_discharge_power"].extra_state_attributes["module_label"] == "String 2"
    # The same ids as a battery reported as two channels gets.
    assert by_key["battery_charge_power"].unique_id == get_sunspec_unique_id(
        entry.entry_id, "battery_charge:DCW", 160, 0
    )

    with patch("custom_components.sunspec2.dc_channels.charge_state", return_value=4):
        assert by_key["battery_charge_power"].native_value == 800
        assert by_key["battery_discharge_power"].native_value == 0
    # Holding: the inverter says nothing moves.
    with patch("custom_components.sunspec2.dc_channels.charge_state", return_value=6):
        assert by_key["battery_charge_power"].native_value == 0
        assert by_key["battery_discharge_power"].native_value == 0
    # No storage model to ask: a power with no direction is no reading.
    with patch("custom_components.sunspec2.dc_channels.charge_state", return_value=None):
        assert by_key["battery_charge_power"].native_value is None

    pv = _entities(hass, "sensor", PvPowerSensor)
    assert len(pv) == 1
    assert pv[0].native_value == 1500
    assert pv[0].extra_state_attributes["modules"] == [0]


async def test_the_symo_hybrid_gets_the_battery_modes_in_whole_watts(
    hass, sunspec_symo_hybrid_client_mock
):
    await _entry(hass)
    selects = _entities(hass, "select", StorageModeSelect)
    assert len(selects) == 1
    assert selects[0].current_option == "auto"
    numbers = {n._rate: n for n in _entities(hass, "number", StorageSetpointNumber)}
    assert numbers[Rate.GRID_CHARGE].native_step == 1.0
    # WChaMax as the dump reports it, the capacity in Wh: the bound is
    # what the inverter says, and so is its percent arithmetic.
    assert numbers[Rate.GRID_CHARGE].native_max_value == 11520.0


# ---------- the enable edge is the procedure here ---------------------------


async def test_a_new_export_limit_is_cycled_without_the_option(
    hass, sunspec_symo_hybrid_client_mock
):
    """The manual's own recipe: the value, then the operating mode restarted via WMaxLim_Ena."""
    entry = await _entry(hass, {CONF_WRITE_BETA_ENABLED: True})
    entry.runtime_data.vendor = replace(FRONIUS_DATAMANAGER, enable_edge_settle_seconds=0.0)
    assert await _set_export_limit(hass, entry, 60) == [
        (123, [("WMaxLim_Ena", 0)]),
        (123, [("WMaxLimPct", 60.0)]),
        (123, [("WMaxLim_Ena", 1)]),
    ]


async def test_the_cycle_can_still_be_switched_off(hass, sunspec_symo_hybrid_client_mock):
    entry = await _entry(hass, {CONF_WRITE_BETA_ENABLED: True, CONF_REARM_ON_CHANGE: False})
    assert await _set_export_limit(hass, entry, 60) == [(123, [("WMaxLimPct", 60.0)])]


async def test_the_option_is_offered_on_by_default(hass, sunspec_symo_hybrid_client_mock):
    entry = await _entry(hass, {CONF_WRITE_BETA_ENABLED: True})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )
    assert result["step_id"] == "model_options"
    defaults = {
        key.schema: key.default()
        for key in result["data_schema"].schema
        if key.default is not vol.UNDEFINED
    }
    assert defaults[CONF_REARM_ON_CHANGE] is True
