"""The Fronius vendor profile: battery modes in watts on top of model 124.

The recipe expectations are the seven examples in the Fronius GEN24
Modbus manual, section "Basic Storage Control Model (124)", with
WChaMax = 3300 W as the manual assumes. The entity tests run against
tests/test_data/inverter_fronius.json, a GEN24 with a 5 kW battery.
"""

from dataclasses import replace
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.const import CONF_REARM_ON_CHANGE
from custom_components.sunspec2.const import CONF_WRITE_BETA_ENABLED
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.dc_channels import DcChannelEnergySensor
from custom_components.sunspec2.dc_channels import DcChannelSensor
from custom_components.sunspec2.dc_channels import PvPowerSensor
from custom_components.sunspec2.storage_modes import StorageModeSelect
from custom_components.sunspec2.storage_modes import StorageSetpointNumber
from custom_components.sunspec2.vendors import profile_for
from custom_components.sunspec2.vendors.fronius import FRONIUS
from custom_components.sunspec2.vendors.fronius import infer_mode
from custom_components.sunspec2.vendors.fronius import module_role
from custom_components.sunspec2.vendors.profile import ModuleRole
from custom_components.sunspec2.vendors.profile import Rate
from custom_components.sunspec2.vendors.profile import StorageMode
from custom_components.sunspec2.vendors.profile import WriteStep
from custom_components.sunspec2.vendors.profile import plan_write
from custom_components.sunspec2.vendors.profile import resolve_rate

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG_STEP_1
from .const import MOCK_CONFIG_WRITE

# ---------- profile lookup --------------------------------------------------


def test_fronius_is_matched_by_manufacturer_prefix():
    assert profile_for("Fronius") is FRONIUS
    assert profile_for("Fronius International GmbH") is FRONIUS
    assert profile_for("fronius") is FRONIUS


def test_other_manufacturers_get_no_profile():
    assert profile_for("KACO new energy") is None
    assert profile_for("SunSpecTest") is None
    assert profile_for(None) is None
    assert profile_for("") is None


# ---------- recipes against the manual's examples ---------------------------

WCHAMAX = 3300.0
STORAGE = FRONIUS.storage
assert STORAGE is not None
RECIPES = STORAGE.recipes


def _writes(mode, setpoints):
    recipe = RECIPES[mode]
    return (
        recipe.ctl_mod,
        resolve_rate(recipe.in_rate, setpoints, WCHAMAX),
        resolve_rate(recipe.out_rate, setpoints, WCHAMAX),
    )


def test_example_1_only_charging_is_block_discharging():
    # "Only permit energy storage charging": OutWRte 0 %, StorCtl_Mod 2.
    ctl, in_pct, out_pct = _writes(StorageMode.BLOCK_DISCHARGING, {})
    assert (ctl, in_pct, out_pct) == (3, 100.0, 0.0)


def test_example_2_only_discharging_is_block_charging():
    # "Only permit energy storage discharging": InWRte 0 %, StorCtl_Mod 1.
    ctl, in_pct, out_pct = _writes(StorageMode.BLOCK_CHARGING, {})
    assert (ctl, in_pct, out_pct) == (3, 0.0, 100.0)


def test_example_4_both_limited_to_half():
    # "Charging and discharging with maximum 50 %": 50 / 50, StorCtl_Mod 3.
    ctl, in_pct, out_pct = _writes(
        StorageMode.CHARGE_AND_DISCHARGE_LIMIT,
        {"charge_limit": 1650.0, "discharge_limit": 1650.0},
    )
    assert (ctl, in_pct, out_pct) == (3, 50.0, 50.0)


def test_example_7_charging_from_the_grid_with_half_the_power():
    # "Charging with 50 % to 100 %": OutWRte -50 %, StorCtl_Mod 2, and
    # Solar.web reports "Forced Recharge".
    ctl, in_pct, out_pct = _writes(StorageMode.CHARGE_FROM_GRID, {"grid_charge": 1650.0})
    assert (ctl, in_pct, out_pct) == (2, 100.0, -50.0)


def test_discharging_to_the_grid_forces_through_the_charge_cap():
    ctl, in_pct, out_pct = _writes(StorageMode.DISCHARGE_TO_GRID, {"grid_discharge": 990.0})
    assert (ctl, in_pct, out_pct) == (1, -30.0, 100.0)


def test_auto_lifts_both_caps():
    assert _writes(StorageMode.AUTO, {"charge_limit": 10.0}) == (0, 100.0, 100.0)


def test_a_limit_never_set_limits_nothing_and_a_grid_power_never_set_is_none():
    assert _writes(StorageMode.PV_CHARGE_LIMIT, {}) == (1, 100.0, 100.0)
    assert _writes(StorageMode.CHARGE_FROM_GRID, {}) == (2, 100.0, 0.0)


def test_watts_beyond_wchamax_clamp_to_the_full_rate():
    assert resolve_rate(Rate.CHARGE_LIMIT, {"charge_limit": 9999.0}, WCHAMAX) == 100.0
    assert resolve_rate(Rate.GRID_CHARGE, {"grid_charge": 9999.0}, WCHAMAX) == -100.0


# ---------- reading the mode back -------------------------------------------


@pytest.mark.parametrize(
    ("ctl", "in_pct", "out_pct", "expected"),
    [
        (0, 100.0, 100.0, StorageMode.AUTO),
        (0, 0.0, 0.0, StorageMode.AUTO),
        (1, 50.0, 100.0, StorageMode.PV_CHARGE_LIMIT),
        (2, 100.0, 50.0, StorageMode.DISCHARGE_LIMIT),
        (3, 50.0, 50.0, StorageMode.CHARGE_AND_DISCHARGE_LIMIT),
        (2, 100.0, -50.0, StorageMode.CHARGE_FROM_GRID),
        (3, 100.0, -50.0, StorageMode.CHARGE_FROM_GRID),
        (1, -50.0, 100.0, StorageMode.DISCHARGE_TO_GRID),
        (3, 100.0, 0.0, StorageMode.BLOCK_DISCHARGING),
        (2, 100.0, 0.0, StorageMode.BLOCK_DISCHARGING),
        (3, 0.0, 100.0, StorageMode.BLOCK_CHARGING),
        (1, 0.0, 100.0, StorageMode.BLOCK_CHARGING),
    ],
)
def test_mode_is_inferred_from_the_registers(ctl, in_pct, out_pct, expected):
    assert infer_mode(ctl, in_pct, out_pct) is expected


def test_every_recipe_reads_back_as_its_own_mode():
    """What the Select writes, the Select must recognise afterwards.

    callifo/fronius_modbus#127 is the failure this guards against: an
    entity that reports what it asked for rather than what the
    inverter did.
    """
    setpoints = {
        "charge_limit": 1000.0,
        "discharge_limit": 2000.0,
        "grid_charge": 1500.0,
        "grid_discharge": 500.0,
    }
    for mode in StorageMode:
        ctl, in_pct, out_pct = _writes(mode, setpoints)
        assert infer_mode(ctl, in_pct, out_pct) is mode, mode


# ---------- entities on a Fronius device ------------------------------------


async def _fronius_entry(hass):
    """An entry with the write beta on, which is what the battery entities need."""
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    hass.config_entries.async_update_entry(entry, options={CONF_WRITE_BETA_ENABLED: True})
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    return entry


def _entities(hass, platform, cls):
    component = hass.data.get("entity_components", {}).get(platform)
    return [e for e in component.entities if isinstance(e, cls)] if component else []


async def test_fronius_device_gets_the_mode_select_and_watt_numbers(
    hass, sunspec_fronius_client_mock
):
    entry = await _fronius_entry(hass)
    coordinator = entry.runtime_data
    assert coordinator.vendor is FRONIUS

    selects = _entities(hass, "select", StorageModeSelect)
    assert len(selects) == 1
    assert selects[0].options == [mode.value for mode in StorageMode]
    # The fixture ships with both caps off: automatic.
    assert selects[0].current_option == "auto"

    numbers = {n._rate: n for n in _entities(hass, "number", StorageSetpointNumber)}
    assert set(numbers) == set(Rate) - {Rate.FULL, Rate.ZERO}
    # First run: a limit that limits nothing, a grid power of none, both
    # bounded by the battery's own WChaMax.
    assert numbers[Rate.CHARGE_LIMIT].native_value == 5000.0
    assert numbers[Rate.GRID_CHARGE].native_value == 0.0
    assert numbers[Rate.GRID_CHARGE].native_max_value == 5000.0
    assert numbers[Rate.GRID_CHARGE].native_step == 10.0
    assert numbers[Rate.CHARGE_LIMIT].native_step == 1.0


async def test_generic_percent_entities_ship_disabled_on_fronius(hass, sunspec_fronius_client_mock):
    """Two entities on one register in different units would disagree.

    An entity that ships disabled never reaches the platform, so the
    registry is the place to look.
    """
    entry = await _fronius_entry(hass)
    registry = er.async_get(hass)

    def disabled_by(platform, point):
        entity_id = registry.async_get_entity_id(
            platform, DOMAIN, get_sunspec_unique_id(entry.entry_id, point, 124, 0)
        )
        assert entity_id is not None, point
        registry_entry = registry.async_get(entity_id)
        assert registry_entry is not None, point
        return registry_entry.disabled_by

    assert disabled_by("number", "InWRte") is er.RegistryEntryDisabler.INTEGRATION
    assert disabled_by("number", "OutWRte") is er.RegistryEntryDisabler.INTEGRATION
    assert disabled_by("select", "StorCtl_Mod") is er.RegistryEntryDisabler.INTEGRATION
    # A register the profile does not take over stays as it was.
    assert disabled_by("number", "WChaMax") is None


async def test_non_fronius_device_gets_no_battery_modes(hass, sunspec_write_client_mock):
    entry = await _fronius_entry(hass)
    assert entry.runtime_data.vendor is None
    assert _entities(hass, "select", StorageModeSelect) == []
    assert _entities(hass, "number", StorageSetpointNumber) == []


async def test_charge_from_grid_writes_the_rates_first_and_the_mode_last(
    hass, sunspec_fronius_client_mock
):
    """2000 W of grid charging on a 5 kW battery is OutWRte -40 % under cap bit 2.

    Two writes in this order: both rates in one frame (the registers
    are adjacent), then the mode. The other way round the inverter acts
    on the old rates for a moment and may refuse the window with Modbus
    exception 3 (callifo/fronius_modbus#126).
    """
    entry = await _fronius_entry(hass)
    coordinator = entry.runtime_data
    select = _entities(hass, "select", StorageModeSelect)[0]
    grid_charge = {n._rate: n for n in _entities(hass, "number", StorageSetpointNumber)}[
        Rate.GRID_CHARGE
    ]

    with patch.object(coordinator.api, "async_write_points") as write:
        await grid_charge.async_set_native_value(2000.0)
        # Automatic mode does not use the grid charge power, so the
        # number only remembers it.
        write.assert_not_called()
        await select.async_select_option("charge_from_grid")

    assert [call.args for call in write.call_args_list] == [
        (124, [("OutWRte", -40.0), ("InWRte", 100.0)]),
        (124, [("StorCtl_Mod", 2)]),
    ]


async def test_grid_charge_power_snaps_to_ten_watt_steps(hass, sunspec_fronius_client_mock):
    """Any other value "does odd things like charging at 500 W" on a GEN24."""
    entry = await _fronius_entry(hass)
    coordinator = entry.runtime_data
    grid_charge = {n._rate: n for n in _entities(hass, "number", StorageSetpointNumber)}[
        Rate.GRID_CHARGE
    ]
    with patch.object(coordinator.api, "async_write_points"):
        await grid_charge.async_set_native_value(1234.0)
    assert grid_charge.native_value == 1230.0


async def test_changing_a_setpoint_in_use_writes_its_register_at_once(
    hass, sunspec_fronius_client_mock
):
    entry = await _fronius_entry(hass)
    coordinator = entry.runtime_data
    charge_limit = {n._rate: n for n in _entities(hass, "number", StorageSetpointNumber)}[
        Rate.CHARGE_LIMIT
    ]
    # The device is in a PV charge limit: cap bit 1 set, InWRte at 50 %.
    with (
        patch(
            "custom_components.sunspec2.storage_modes.device_rates",
            return_value=(1, 50.0, 100.0),
        ),
        patch.object(coordinator.api, "async_write_points") as write,
    ):
        await charge_limit.async_set_native_value(1000.0)

    assert [call.args for call in write.call_args_list] == [(124, [("InWRte", 20.0)])]


async def test_mode_select_refuses_a_mode_it_does_not_know(hass, sunspec_fronius_client_mock):
    await _fronius_entry(hass)
    select = _entities(hass, "select", StorageModeSelect)[0]
    with pytest.raises(HomeAssistantError):
        await select.async_select_option("turbo")


# ---------- the battery and PV channels of model 160 ------------------------


@pytest.mark.parametrize(
    "label, role",
    [
        ("MPPT 1", ModuleRole.PV),
        ("MPPT 2", ModuleRole.PV),
        ("ST CHA", ModuleRole.BATTERY_CHARGE),
        ("ST DISCHA", ModuleRole.BATTERY_DISCHARGE),
        ("Battery", None),
        ("", None),
    ],
)
def test_module_role_reads_the_gen24_labels(label, role):
    assert module_role(label) is role


async def test_fronius_device_gets_battery_and_pv_sensors(hass, sunspec_fronius_client_mock):
    """The fixture: MPPT 1 at 900 W, MPPT 2 at 920 W, ST CHA idle, ST DISCHA at 2000 W."""
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)

    by_key = {s.translation_key: s for s in _entities(hass, "sensor", DcChannelSensor)}
    assert set(by_key) == {
        "battery_charge_power",
        "battery_discharge_power",
        "battery_charged_energy",
        "battery_discharged_energy",
    }
    assert by_key["battery_charge_power"].native_value == 0
    assert by_key["battery_discharge_power"].native_value == 2000
    assert isinstance(by_key["battery_charged_energy"], DcChannelEnergySensor)
    assert by_key["battery_charged_energy"].native_value == 3
    assert by_key["battery_discharged_energy"].native_value == 4
    assert by_key["battery_discharge_power"].extra_state_attributes["module_label"] == "ST DISCHA"

    pv = _entities(hass, "sensor", PvPowerSensor)
    assert len(pv) == 1
    assert pv[0].native_value == 1820
    assert pv[0].extra_state_attributes["modules"] == [0, 1]


async def test_channel_sensors_are_keyed_by_role_and_named_by_it(hass, sunspec_fronius_client_mock):
    """The generic "Module 3" sensor keeps its id; the role sensor has its own and a name of its own."""
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    registry = er.async_get(hass)

    generic_id = registry.async_get_entity_id(
        "sensor", DOMAIN, get_sunspec_unique_id(entry.entry_id, "module:3:DCW", 160, 0)
    )
    role_id = registry.async_get_entity_id(
        "sensor", DOMAIN, get_sunspec_unique_id(entry.entry_id, "battery_discharge:DCW", 160, 0)
    )
    assert generic_id is not None
    assert role_id is not None
    assert role_id != generic_id

    state = hass.states.get(role_id)
    assert state is not None
    assert state.name.endswith("Battery discharge power")
    assert state.state == "2000"
    pv_id = registry.async_get_entity_id(
        "sensor", DOMAIN, get_sunspec_unique_id(entry.entry_id, "pv:DCW", 160, 0)
    )
    assert pv_id is not None
    pv_state = hass.states.get(pv_id)
    assert pv_state is not None
    assert pv_state.name.endswith("PV power")
    assert float(pv_state.state) == 1820


async def test_non_fronius_device_gets_no_channel_sensors(hass, sunspec_write_client_mock):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    assert _entities(hass, "sensor", DcChannelSensor) == []
    assert _entities(hass, "sensor", PvPowerSensor) == []


# ---------- the enable edge of the export limit -----------------------------


def _limit_on(model_id, point):
    """The device as the fixture has it: the export limit on, the power factor off."""
    return point == "WMaxLim_Ena"


def test_a_write_stays_plain_without_the_option_or_a_profile():
    steps = plan_write(FRONIUS, 123, [("WMaxLimPct", 60.0)], _limit_on, rearm=False)
    assert steps == [WriteStep([("WMaxLimPct", 60.0)])]
    assert plan_write(None, 123, [("WMaxLimPct", 60.0)], _limit_on, rearm=True) == steps


def test_a_new_limit_with_the_enable_on_goes_out_between_off_and_on():
    steps = plan_write(FRONIUS, 123, [("WMaxLimPct", 60.0)], _limit_on, rearm=True)
    assert steps == [
        WriteStep([("WMaxLim_Ena", 0)]),
        WriteStep([("WMaxLimPct", 60.0)], 1.0),
        WriteStep([("WMaxLim_Ena", 1)]),
    ]


def test_the_action_asking_for_enable_is_cycled_the_same_way():
    points: list[tuple[str, object]] = [("WMaxLimPct", 60.0), ("WMaxLim_Ena", 1)]
    steps = plan_write(FRONIUS, 123, points, _limit_on, rearm=True)
    assert steps == [
        WriteStep([("WMaxLim_Ena", 0)]),
        WriteStep([("WMaxLimPct", 60.0)], 1.0),
        WriteStep([("WMaxLim_Ena", 1)]),
    ]


@pytest.mark.parametrize(
    "points",
    [
        # The power factor's enable is off: the value waits for the switch.
        [("OutPFSet", 0.95)],
        # The write turns the enable off: nothing to re-arm.
        [("WMaxLimPct", 60.0), ("WMaxLim_Ena", 0)],
        # The switch alone is not a value the edge is for.
        [("WMaxLim_Ena", 1)],
        # A point the vendor applies as written.
        [("WMaxLimPct_RvrtTms", 120)],
    ],
)
def test_writes_that_need_no_edge_stay_plain(points):
    assert plan_write(FRONIUS, 123, points, _limit_on, rearm=True) == [WriteStep(points)]


async def _set_export_limit(hass, entry, percent):
    """Set the model 123 export limit Number and return the api writes it caused."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "number", DOMAIN, get_sunspec_unique_id(entry.entry_id, "WMaxLimPct", 123, 0)
    )
    assert entity_id is not None
    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": percent}, blocking=True
        )
    return [call.args for call in write.call_args_list]


async def test_the_number_cycles_the_enable_with_the_option_on(hass, sunspec_fronius_client_mock):
    """The fixture has the limit on at 100 %; a new value goes out between off and on."""
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    hass.config_entries.async_update_entry(
        entry, options={CONF_WRITE_BETA_ENABLED: True, CONF_REARM_ON_CHANGE: True}
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    # No second of waiting in a test.
    entry.runtime_data.vendor = replace(FRONIUS, enable_edge_settle_seconds=0.0)

    assert await _set_export_limit(hass, entry, 60) == [
        (123, [("WMaxLim_Ena", 0)]),
        (123, [("WMaxLimPct", 60.0)]),
        (123, [("WMaxLim_Ena", 1)]),
    ]


async def test_the_number_writes_the_value_alone_without_the_option(
    hass, sunspec_fronius_client_mock
):
    entry = await _fronius_entry(hass)
    assert await _set_export_limit(hass, entry, 60) == [(123, [("WMaxLimPct", 60.0)])]


async def _model_option_fields(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "host_options"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )
    assert result["step_id"] == "model_options"
    return {key.schema for key in result["data_schema"].schema}


async def test_the_option_is_offered_on_a_fronius(hass, sunspec_fronius_client_mock):
    entry = await _fronius_entry(hass)
    assert CONF_REARM_ON_CHANGE in await _model_option_fields(hass, entry)


async def test_the_option_is_not_offered_on_a_device_that_applies_as_written(
    hass, sunspec_write_client_mock
):
    entry = await _fronius_entry(hass)
    assert CONF_REARM_ON_CHANGE not in await _model_option_fields(hass, entry)
