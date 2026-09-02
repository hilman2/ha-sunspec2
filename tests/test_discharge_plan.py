"""The scheduled discharge (#57): a window, a reserve, a capacity, and the writes they cause.

The entity tests run against tests/test_data/inverter_fronius.json: a
5 kW battery at 55 % charge, no nameplate model, so the capacity has
to come from the entity.
"""

from datetime import time
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.sunspec2.const import CONF_WRITE_BETA_ENABLED
from custom_components.sunspec2.discharge_plan import DischargePlanCapacityNumber
from custom_components.sunspec2.discharge_plan import DischargePlanReserveNumber
from custom_components.sunspec2.discharge_plan import DischargePlanSwitch
from custom_components.sunspec2.discharge_plan import DischargePlanTime
from custom_components.sunspec2.discharge_plan import hours_until
from custom_components.sunspec2.discharge_plan import in_window
from custom_components.sunspec2.discharge_plan import planned_power_w
from custom_components.sunspec2.discharge_plan import window_hours

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG_WRITE

# ---------- the arithmetic ----------------------------------------------------


def test_window_hours_wraps_past_midnight():
    assert window_hours(time(20, 0), time(6, 0)) == 10.0
    assert window_hours(time(22, 30), time(23, 0)) == 0.5
    assert window_hours(time(22, 0), time(22, 0)) == 24.0


def test_in_window_wraps_past_midnight():
    assert in_window(time(23, 0), time(20, 0), time(6, 0))
    assert in_window(time(2, 0), time(20, 0), time(6, 0))
    assert not in_window(time(12, 0), time(20, 0), time(6, 0))
    assert in_window(time(9, 0), time(8, 0), time(10, 0))
    assert not in_window(time(10, 0), time(8, 0), time(10, 0))


def test_hours_until_counts_to_the_next_end():
    now = dt_util.now().replace(hour=2, minute=0, second=0, microsecond=0)
    assert hours_until(now, time(6, 0)) == 4.0
    assert hours_until(now, time(1, 0)) == 23.0


def test_the_reporters_numbers_give_4200_w():
    """#57: a 60 kWh battery, 42 kWh above the reserve, ten hours."""
    assert planned_power_w(80.0, 10.0, 60.0, 10.0, wchamax_w=10000.0, step_w=10.0) == 4200.0


def test_the_power_is_capped_at_wchamax_and_floored_to_the_step():
    assert planned_power_w(80.0, 10.0, 60.0, 10.0, 3000.0, 10.0) == 3000.0
    # 45 % of 12.3 kWh over ten hours is 553.5 W.
    assert planned_power_w(55.0, 10.0, 12.3, 10.0, 5000.0, 10.0) == 550.0
    assert planned_power_w(5.0, 10.0, 60.0, 10.0, 5000.0, 10.0) == 0.0
    assert planned_power_w(80.0, 10.0, 60.0, 0.0, 5000.0, 10.0) == 0.0


# ---------- the entities ------------------------------------------------------


async def _entry(hass):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    hass.config_entries.async_update_entry(entry, options={CONF_WRITE_BETA_ENABLED: True})
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    return entry


def _entities(hass, platform, cls):
    component = hass.data.get("entity_components", {}).get(platform)
    return [e for e in component.entities if isinstance(e, cls)] if component else []


def _local(hour, minute=0, second=0):
    return dt_util.now().replace(hour=hour, minute=minute, second=second, microsecond=0)


DISCHARGE_TO_GRID_450_W = [
    (124, [("OutWRte", 100.0), ("InWRte", -9.0)]),
    (124, [("StorCtl_Mod", 1)]),
]
AUTOMATIC = [
    (124, [("OutWRte", 100.0), ("InWRte", 100.0)]),
    (124, [("StorCtl_Mod", 0)]),
]


async def test_a_fronius_battery_gets_the_plan_entities(hass, sunspec_fronius_client_mock):
    entry = await _entry(hass)
    assert len(_entities(hass, "switch", DischargePlanSwitch)) == 1
    assert {t._which for t in _entities(hass, "time", DischargePlanTime)} == {"start", "end"}
    assert len(_entities(hass, "number", DischargePlanReserveNumber)) == 1
    assert len(_entities(hass, "number", DischargePlanCapacityNumber)) == 1

    planner = entry.runtime_data.discharge_plan
    assert planner is not None
    assert not planner.settings.enabled
    assert (planner.settings.start, planner.settings.end) == (time(20, 0), time(6, 0))
    assert planner.settings.reserve_pct == 10.0
    # No nameplate model in the fixture: the capacity waits for the user.
    assert planner.settings.capacity_kwh is None


async def test_a_device_without_battery_modes_gets_no_plan(hass, sunspec_write_client_mock):
    entry = await _entry(hass)
    assert entry.runtime_data.discharge_plan is None
    assert _entities(hass, "switch", DischargePlanSwitch) == []
    assert _entities(hass, "time", DischargePlanTime) == []


async def test_the_start_of_the_window_plans_the_whole_window(hass, sunspec_fronius_client_mock):
    """55 % of 10 kWh minus the 10 % reserve is 4.5 kWh; over ten hours 450 W, 9 % of 5 kW."""
    entry = await _entry(hass)
    planner = entry.runtime_data.discharge_plan
    planner.settings.capacity_kwh = 10.0
    planner.settings.enabled = True

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_at_start()

    assert planner.planned_power_w == 450.0
    assert entry.runtime_data.storage_setpoints["grid_discharge"] == 450.0
    assert [call.args for call in write.call_args_list] == DISCHARGE_TO_GRID_450_W


async def test_the_end_of_the_window_hands_the_battery_back(hass, sunspec_fronius_client_mock):
    entry = await _entry(hass)
    planner = entry.runtime_data.discharge_plan
    planner.settings.enabled = True

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_at_end()

    assert [call.args for call in write.call_args_list] == AUTOMATIC
    assert planner.planned_power_w == 0.0


async def test_a_plan_that_is_off_writes_nothing(hass, sunspec_fronius_client_mock):
    entry = await _entry(hass)
    planner = entry.runtime_data.discharge_plan
    planner.settings.capacity_kwh = 10.0

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_at_start()
        await planner.async_at_end()

    write.assert_not_called()


async def test_without_a_capacity_nothing_is_written_and_the_log_says_why(
    hass, sunspec_fronius_client_mock, caplog
):
    entry = await _entry(hass)
    planner = entry.runtime_data.discharge_plan
    planner.settings.enabled = True

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_at_start()

    write.assert_not_called()
    assert "battery capacity" in caplog.text


async def test_switched_on_inside_the_window_plans_what_is_left(
    hass, sunspec_fronius_client_mock, freezer
):
    """At 02:00 four hours remain: 4.5 kWh over four hours is 1125 W, floored to 1120 W."""
    entry = await _entry(hass)
    planner = entry.runtime_data.discharge_plan
    planner.settings.capacity_kwh = 10.0
    freezer.move_to(_local(2))
    switch = _entities(hass, "switch", DischargePlanSwitch)[0]

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await switch.async_turn_on()

    assert planner.settings.enabled
    assert planner.planned_power_w == 1120.0
    model_id, rates = write.call_args_list[0].args
    assert model_id == 124
    assert rates[0] == ("OutWRte", 100.0)
    assert rates[1][0] == "InWRte"
    assert rates[1][1] == pytest.approx(-22.4)
    assert switch.extra_state_attributes == {"planned_power_w": 1120.0, "window_hours": 10.0}


async def test_switched_on_outside_the_window_waits_for_the_start(
    hass, sunspec_fronius_client_mock, freezer
):
    entry = await _entry(hass)
    entry.runtime_data.discharge_plan.settings.capacity_kwh = 10.0
    freezer.move_to(_local(12))
    switch = _entities(hass, "switch", DischargePlanSwitch)[0]

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await switch.async_turn_on()

    write.assert_not_called()
    assert entry.runtime_data.discharge_plan.settings.enabled


async def test_switched_off_inside_the_window_hands_the_battery_back(
    hass, sunspec_fronius_client_mock, freezer
):
    entry = await _entry(hass)
    planner = entry.runtime_data.discharge_plan
    planner.settings.enabled = True
    freezer.move_to(_local(23))
    switch = _entities(hass, "switch", DischargePlanSwitch)[0]

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await switch.async_turn_off()

    assert not planner.settings.enabled
    assert [call.args for call in write.call_args_list] == AUTOMATIC


async def test_switched_off_outside_the_window_leaves_the_battery_alone(
    hass, sunspec_fronius_client_mock, freezer
):
    entry = await _entry(hass)
    planner = entry.runtime_data.discharge_plan
    planner.settings.enabled = True
    freezer.move_to(_local(12))
    switch = _entities(hass, "switch", DischargePlanSwitch)[0]

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await switch.async_turn_off()

    write.assert_not_called()


async def test_the_window_is_set_through_the_time_entities(hass, sunspec_fronius_client_mock):
    entry = await _entry(hass)
    planner = entry.runtime_data.discharge_plan
    times = {t._which: t for t in _entities(hass, "time", DischargePlanTime)}

    await times["start"].async_set_value(time(21, 30))
    await times["end"].async_set_value(time(5, 0))

    assert (planner.settings.start, planner.settings.end) == (time(21, 30), time(5, 0))
    assert times["start"].native_value == time(21, 30)
    assert window_hours(planner.settings.start, planner.settings.end) == 7.5


async def test_the_reserve_and_capacity_are_set_through_the_numbers(
    hass, sunspec_fronius_client_mock
):
    entry = await _entry(hass)
    planner = entry.runtime_data.discharge_plan
    reserve = _entities(hass, "number", DischargePlanReserveNumber)[0]
    capacity = _entities(hass, "number", DischargePlanCapacityNumber)[0]

    await reserve.async_set_native_value(25.0)
    await capacity.async_set_native_value(60.0)

    assert planner.settings.reserve_pct == 25.0
    assert planner.settings.capacity_kwh == 60.0
    assert capacity.native_value == 60.0


async def test_the_start_trigger_fires_at_the_start_of_the_window(
    hass, sunspec_fronius_client_mock, freezer
):
    """The clock is frozen before setup so the trigger is armed for the frozen day."""
    freezer.move_to(_local(19, 59, 55))
    entry = await _entry(hass)
    planner = entry.runtime_data.discharge_plan
    planner.settings.capacity_kwh = 10.0
    planner.settings.enabled = True

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        async_fire_time_changed(hass, _local(20, 0, 1))
        # Time triggers run their job as a background task.
        await hass.async_block_till_done(wait_background_tasks=True)

    assert planner.planned_power_w == 450.0
    assert [call.args for call in write.call_args_list] == DISCHARGE_TO_GRID_450_W


async def test_after_a_restart_inside_the_window_the_plan_resumes(
    hass, sunspec_fronius_client_mock, freezer
):
    """The switch restores "on" and, once the other entities had time to restore, plans the rest."""
    freezer.move_to(_local(2))
    entry = await _entry(hass)
    planner = entry.runtime_data.discharge_plan
    planner.settings.capacity_kwh = 10.0
    planner.settings.enabled = True
    planner.async_check_after_restore()

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        async_fire_time_changed(hass, _local(2) + timedelta(seconds=11))
        await hass.async_block_till_done(wait_background_tasks=True)

    assert planner.planned_power_w == 1120.0
    assert write.call_count == 2
