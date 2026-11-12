"""The battery mode Select and its watt setpoints when the device does not cooperate.

tests/test_fronius.py has the recipes and the working path. These run
against the same fixture, tests/test_data/inverter_fronius.json, a GEN24
with a 5 kW battery in automatic mode, and change what the device
reports: no battery, registers it does not implement, writes it refuses.
"""

import logging
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError

from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.number import SunSpecWriteNumber
from custom_components.sunspec2.storage_modes import StorageModeSelect
from custom_components.sunspec2.storage_modes import StorageSetpointNumber
from custom_components.sunspec2.vendors.fronius import FRONIUS
from custom_components.sunspec2.vendors.profile import Rate

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG_WRITE


async def _fronius_entry(hass):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.vendor is FRONIUS
    return entry


def _entities(hass, platform, cls):
    component = hass.data.get("entity_components", {}).get(platform)
    return [e for e in component.entities if isinstance(e, cls)] if component else []


def _mode_select(hass):
    selects = _entities(hass, "select", StorageModeSelect)
    assert len(selects) == 1
    return selects[0]


def _setpoint(hass, rate):
    return {n._rate: n for n in _entities(hass, "number", StorageSetpointNumber)}[rate]


def _pv_charge_limit_at_half(client):
    """Put the device into a PV charge limit: cap bit 1 set, InWRte at 50 %."""
    storage = client.models[124][0]
    storage.StorCtl_Mod.value = 1
    storage.InWRte.value = 5000


# ---------- a Fronius without a battery --------------------------------------


@pytest.mark.parametrize(
    "wchamax",
    [
        0,
        # The register answers "not implemented".
        None,
    ],
)
async def test_a_fronius_that_reports_no_charge_power_gets_no_battery_modes(
    hass, sunspec_fronius_client_mock, wchamax
):
    """WChaMax above zero is how the device says it has a battery."""
    sunspec_fronius_client_mock.models[124][0].WChaMax.value = wchamax

    await _fronius_entry(hass)

    assert _entities(hass, "select", StorageModeSelect) == []
    assert _entities(hass, "number", StorageSetpointNumber) == []
    # The export limit of model 123 is still there.
    assert _entities(hass, "number", SunSpecWriteNumber)


async def test_a_fronius_without_the_storage_model_gets_no_battery_modes(
    hass, sunspec_fronius_client_mock
):
    del sunspec_fronius_client_mock.models[124]

    await _fronius_entry(hass)

    assert _entities(hass, "select", StorageModeSelect) == []
    assert _entities(hass, "number", StorageSetpointNumber) == []
    # The export limit of model 123 is still there.
    assert _entities(hass, "number", SunSpecWriteNumber)


async def test_selecting_a_mode_is_refused_once_the_battery_is_gone(
    hass, sunspec_fronius_client_mock
):
    """The rates are percent of WChaMax; without it there is nothing to compute them from."""
    entry = await _fronius_entry(hass)
    coordinator = entry.runtime_data
    select = _mode_select(hass)

    sunspec_fronius_client_mock.models[124][0].WChaMax.value = 0
    await coordinator.async_refresh()

    with (
        patch.object(coordinator.api, "async_write_points") as write,
        pytest.raises(
            HomeAssistantError, match=r"The inverter reports no battery \(WChaMax is 0\)"
        ),
    ):
        await select.async_select_option("block_charging")

    write.assert_not_called()
    assert _setpoint(hass, Rate.GRID_CHARGE).native_max_value == 0.0


async def test_a_setpoint_in_use_is_not_written_while_the_battery_reports_no_charge_power(
    hass, sunspec_fronius_client_mock
):
    _pv_charge_limit_at_half(sunspec_fronius_client_mock)
    entry = await _fronius_entry(hass)
    coordinator = entry.runtime_data
    select = _mode_select(hass)
    charge_limit = _setpoint(hass, Rate.CHARGE_LIMIT)
    assert select.current_option == "pv_charge_limit"

    sunspec_fronius_client_mock.models[124][0].WChaMax.value = 0
    await coordinator.async_refresh()

    with patch.object(coordinator.api, "async_write_points") as write:
        await charge_limit.async_set_native_value(1000.0)

    write.assert_not_called()
    assert charge_limit.native_value == 1000.0


# ---------- registers the device does not report -----------------------------


@pytest.mark.parametrize("point", ["StorCtl_Mod", "InWRte", "OutWRte"])
async def test_the_mode_is_unknown_while_a_register_it_is_read_from_reports_nothing(
    hass, sunspec_fronius_client_mock, point
):
    """The mode is read back from all three; a guess from two would be a remembered mode."""
    sunspec_fronius_client_mock.models[124][0].points[point].value = None
    await _fronius_entry(hass)

    select = _mode_select(hass)

    assert select.current_option is None
    state = hass.states.get(select.entity_id)
    assert state is not None
    assert state.state == "unknown"


async def test_a_setpoint_is_only_remembered_while_the_mode_cannot_be_read(
    hass, sunspec_fronius_client_mock
):
    """Without a mode nothing says the register holds this setpoint, so it is left alone."""
    _pv_charge_limit_at_half(sunspec_fronius_client_mock)
    sunspec_fronius_client_mock.models[124][0].OutWRte.value = None
    entry = await _fronius_entry(hass)
    charge_limit = _setpoint(hass, Rate.CHARGE_LIMIT)

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await charge_limit.async_set_native_value(1000.0)

    write.assert_not_called()
    assert charge_limit.native_value == 1000.0


async def test_mode_and_setpoints_read_nothing_once_the_device_stops_listing_model_124(
    hass, sunspec_fronius_client_mock
):
    """A firmware update can take a model away; the next scan then leaves it out of the data."""
    _pv_charge_limit_at_half(sunspec_fronius_client_mock)
    entry = await _fronius_entry(hass)
    coordinator = entry.runtime_data
    select = _mode_select(hass)
    charge_limit = _setpoint(hass, Rate.CHARGE_LIMIT)
    assert select.current_option == "pv_charge_limit"

    del sunspec_fronius_client_mock.models[124]
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert 124 not in coordinator.data

    assert select.current_option is None
    assert charge_limit.native_max_value == 0.0
    with patch.object(coordinator.api, "async_write_points") as write:
        await charge_limit.async_set_native_value(1000.0)
        with pytest.raises(HomeAssistantError, match="The inverter reports no battery"):
            await select.async_select_option("auto")
    write.assert_not_called()
    assert charge_limit.native_value == 1000.0


# ---------- writes the device refuses ----------------------------------------


async def test_a_failed_mode_change_names_the_mode_and_the_cause(hass, sunspec_fronius_client_mock):
    entry = await _fronius_entry(hass)
    coordinator = entry.runtime_data
    select = _mode_select(hass)

    with (
        patch.object(
            coordinator.api, "async_write_points", side_effect=DeviceError("inverter said no")
        ),
        patch.object(coordinator, "async_request_refresh") as refresh,
        pytest.raises(
            HomeAssistantError,
            match="Failed to set battery mode block_charging: inverter said no",
        ),
    ):
        await select.async_select_option("block_charging")

    refresh.assert_not_called()
    assert not coordinator._gateway_lock.locked()


async def test_a_failed_setpoint_write_names_the_register_and_the_value(
    hass, sunspec_fronius_client_mock
):
    """1000 W of a 5 kW battery is InWRte 20 %."""
    _pv_charge_limit_at_half(sunspec_fronius_client_mock)
    entry = await _fronius_entry(hass)
    coordinator = entry.runtime_data
    charge_limit = _setpoint(hass, Rate.CHARGE_LIMIT)

    with (
        patch.object(
            coordinator.api, "async_write_points", side_effect=DeviceError("inverter said no")
        ),
        patch.object(coordinator, "async_request_refresh") as refresh,
        pytest.raises(HomeAssistantError, match="Failed to write InWRte=20.0: inverter said no"),
    ):
        await charge_limit.async_set_native_value(1000.0)

    refresh.assert_not_called()
    assert not coordinator._gateway_lock.locked()


async def test_a_setpoint_in_use_reaches_the_device_and_the_mode_stays(
    hass, sunspec_fronius_client_mock
):
    """The counterpart of the tests above, against the registers instead of a patched reader."""
    _pv_charge_limit_at_half(sunspec_fronius_client_mock)
    await _fronius_entry(hass)
    charge_limit = _setpoint(hass, Rate.CHARGE_LIMIT)

    await hass.services.async_call(
        "number", "set_value", {"entity_id": charge_limit.entity_id, "value": 1000}, blocking=True
    )

    storage = sunspec_fronius_client_mock.models[124][0]
    # InOutWRte_SF is -2: 20 % is raw 2000.
    assert storage.InWRte.value == 2000
    assert storage.StorCtl_Mod.value == 1
    assert _mode_select(hass).current_option == "pv_charge_limit"


# ---------- grid charging with the web interface in the way ------------------


async def test_grid_charging_goes_out_over_modbus_when_the_web_interface_refuses_the_flag(
    hass, sunspec_fronius_client_mock, caplog
):
    """The mode is what the user asked for; the refused flag is a warning, not a stop."""
    entry = await _fronius_entry(hass)
    coordinator = entry.runtime_data
    select = _mode_select(hass)
    web = Mock()
    web.async_allow_grid_charging = AsyncMock(side_effect=RuntimeError("401 Unauthorized"))
    coordinator.web = web

    with (
        caplog.at_level(logging.WARNING),
        patch.object(coordinator.api, "async_write_points") as write,
    ):
        await select.async_select_option("charge_from_grid")

    web.async_allow_grid_charging.assert_awaited_once()
    assert "Could not enable grid charging in the inverter's web interface" in caplog.text
    assert "401 Unauthorized" in caplog.text
    # The grid charge power was never set: a window of zero width.
    assert [call.args for call in write.call_args_list] == [
        (124, [("OutWRte", 0.0), ("InWRte", 100.0)]),
        (124, [("StorCtl_Mod", 2)]),
    ]


async def test_the_web_interface_is_left_alone_for_a_mode_that_does_not_charge_from_the_grid(
    hass, sunspec_fronius_client_mock
):
    entry = await _fronius_entry(hass)
    coordinator = entry.runtime_data
    select = _mode_select(hass)
    web = Mock()
    web.async_allow_grid_charging = AsyncMock()
    coordinator.web = web

    with patch.object(coordinator.api, "async_write_points"):
        await select.async_select_option("block_discharging")

    web.async_allow_grid_charging.assert_not_awaited()
