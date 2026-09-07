"""Battery plans: direction, window, target, capacity, and the writes they cause.

The entity tests run against tests/test_data/inverter_fronius.json: a
5 kW battery at 55 % charge, no nameplate model, so the capacity has
to come from the entity.
"""

from dataclasses import replace
from datetime import time
from datetime import timedelta
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.exceptions import Unauthorized
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.sunspec2.battery_plan import BatteryPlanCapacityNumber
from custom_components.sunspec2.battery_plan import BatteryPlanDirection
from custom_components.sunspec2.battery_plan import BatteryPlanSwitch
from custom_components.sunspec2.battery_plan import BatteryPlanTargetNumber
from custom_components.sunspec2.battery_plan import BatteryPlanTime
from custom_components.sunspec2.battery_plan import PlanDirection
from custom_components.sunspec2.battery_plan import hours_until
from custom_components.sunspec2.battery_plan import in_window
from custom_components.sunspec2.battery_plan import planned_power_w
from custom_components.sunspec2.battery_plan import window_hours
from custom_components.sunspec2.const import CONF_WRITE_BETA_ENABLED

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
    # The device's own revert timer first, pointed at the end of the
    # ten hour window, then the rates, then the mode (#57).
    (124, [("InOutWRte_RvrtTms", 36000)]),
    (124, [("OutWRte", 100.0), ("InWRte", -9.0)]),
    (124, [("StorCtl_Mod", 1)]),
]
AUTOMATIC = [
    (124, [("OutWRte", 100.0), ("InWRte", 100.0)]),
    (124, [("StorCtl_Mod", 0)]),
]


async def test_a_fronius_battery_gets_the_plan_entities(hass, sunspec_fronius_client_mock):
    entry = await _entry(hass)
    assert len(_entities(hass, "switch", BatteryPlanSwitch)) == 1
    assert {t._which for t in _entities(hass, "time", BatteryPlanTime)} == {"start", "end"}
    assert len(_entities(hass, "number", BatteryPlanTargetNumber)) == 1
    assert len(_entities(hass, "number", BatteryPlanCapacityNumber)) == 1

    planner = entry.runtime_data.battery_plan
    assert planner is not None
    assert not planner.settings.enabled
    assert (planner.settings.start, planner.settings.end) == (time(20, 0), time(6, 0))
    assert planner.settings.target_pct == 10.0
    # No nameplate model in the fixture: the capacity waits for the user.
    assert planner.settings.capacity_kwh is None


async def test_a_device_without_battery_modes_gets_no_plan(hass, sunspec_write_client_mock):
    entry = await _entry(hass)
    assert entry.runtime_data.battery_plan is None
    assert _entities(hass, "switch", BatteryPlanSwitch) == []
    assert _entities(hass, "time", BatteryPlanTime) == []


async def test_the_start_of_the_window_plans_the_whole_window(hass, sunspec_fronius_client_mock):
    """55 % of 10 kWh minus the 10 % reserve is 4.5 kWh; over ten hours 450 W, 9 % of 5 kW."""
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    planner.settings.capacity_kwh = 10.0
    planner.settings.enabled = True

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_at_start()

    assert planner.planned_power_w == 450.0
    assert entry.runtime_data.storage_setpoints["grid_discharge"] == 450.0
    assert [call.args for call in write.call_args_list] == DISCHARGE_TO_GRID_450_W


async def test_the_end_of_the_window_hands_the_battery_back(hass, sunspec_fronius_client_mock):
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    planner.settings.enabled = True

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_at_end()

    assert [call.args for call in write.call_args_list] == AUTOMATIC
    assert planner.planned_power_w == 0.0


async def test_a_plan_that_is_off_writes_nothing(hass, sunspec_fronius_client_mock):
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    planner.settings.capacity_kwh = 10.0

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_at_start()
        await planner.async_at_end()

    write.assert_not_called()


async def test_without_a_capacity_nothing_is_written_and_the_log_says_why(
    hass, sunspec_fronius_client_mock, caplog
):
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
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
    planner = entry.runtime_data.battery_plan
    planner.settings.capacity_kwh = 10.0
    freezer.move_to(_local(2))
    switch = _entities(hass, "switch", BatteryPlanSwitch)[0]

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await switch.async_turn_on()

    assert planner.settings.enabled
    assert planner.planned_power_w == 1120.0
    assert write.call_args_list[0].args == (124, [("InOutWRte_RvrtTms", 14400)])
    model_id, rates = write.call_args_list[1].args
    assert model_id == 124
    assert rates[0] == ("OutWRte", 100.0)
    assert rates[1][0] == "InWRte"
    assert rates[1][1] == pytest.approx(-22.4)
    assert switch.extra_state_attributes == {"planned_power_w": 1120.0, "window_hours": 10.0}


async def test_switched_on_outside_the_window_waits_for_the_start(
    hass, sunspec_fronius_client_mock, freezer
):
    entry = await _entry(hass)
    entry.runtime_data.battery_plan.settings.capacity_kwh = 10.0
    freezer.move_to(_local(12))
    switch = _entities(hass, "switch", BatteryPlanSwitch)[0]

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await switch.async_turn_on()

    write.assert_not_called()
    assert entry.runtime_data.battery_plan.settings.enabled


async def test_switched_off_inside_the_window_hands_the_battery_back(
    hass, sunspec_fronius_client_mock, freezer
):
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    planner.settings.enabled = True
    freezer.move_to(_local(23))
    switch = _entities(hass, "switch", BatteryPlanSwitch)[0]

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await switch.async_turn_off()

    assert not planner.settings.enabled
    assert [call.args for call in write.call_args_list] == AUTOMATIC


async def test_switched_off_outside_the_window_leaves_the_battery_alone(
    hass, sunspec_fronius_client_mock, freezer
):
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    planner.settings.enabled = True
    freezer.move_to(_local(12))
    switch = _entities(hass, "switch", BatteryPlanSwitch)[0]

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await switch.async_turn_off()

    write.assert_not_called()


async def test_a_round_the_clock_window_caps_the_revert_timer(
    hass, sunspec_fronius_client_mock, freezer
):
    """``InOutWRte_RvrtTms`` is a uint16 of seconds, so 24 hours do not fit in it."""
    freezer.move_to(_local(23))
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    planner.settings.capacity_kwh = 10.0
    planner.settings.enabled = True
    planner.async_set_window(start=time(23, 0), end=time(23, 0))

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_at_start()

    assert write.call_args_list[0].args == (124, [("InOutWRte_RvrtTms", 65535)])


async def test_a_device_that_keeps_its_own_short_timer_is_written_again(
    hass, sunspec_fronius_client_mock, freezer
):
    """#57: the GEN24 that drops the rates after 300 s gets them again at 270.

    The plan asks for a timer that covers the window. This is the net
    under a device that answers with a shorter one anyway: the interval
    is paced off what the device reports, not off what was asked for.
    """
    freezer.move_to(_local(23))
    sunspec_fronius_client_mock.models[124][0].points["InOutWRte_RvrtTms"].value = 300
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    planner.settings.capacity_kwh = 10.0
    planner.settings.enabled = True

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_resume()
        write.reset_mock()
        async_fire_time_changed(hass, _local(23) + timedelta(seconds=271))
        await hass.async_block_till_done(wait_background_tasks=True)

    # Seven hours are left of the window at 23:00, and 4.5 kWh over
    # seven hours is 640 W once floored to the vendor's 10 W step.
    assert write.call_count == 3
    assert write.call_args_list[0].args == (124, [("InOutWRte_RvrtTms", 25200)])
    assert write.call_args_list[2].args == (124, [("StorCtl_Mod", 1)])
    assert planner.planned_power_w == 640.0


async def test_the_end_of_the_window_puts_the_revert_time_back(
    hass, sunspec_fronius_client_mock, freezer
):
    """The plan borrows the register for the night, it does not keep it."""
    freezer.move_to(_local(23))
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    planner.settings.capacity_kwh = 10.0
    planner.settings.enabled = True

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_resume()
        write.reset_mock()
        await planner.async_at_end()

    assert [call.args for call in write.call_args_list] == [
        *AUTOMATIC,
        (124, [("InOutWRte_RvrtTms", 0)]),
    ]


async def test_reaching_the_reserve_hands_the_battery_back(
    hass, sunspec_fronius_client_mock, freezer
):
    """Nothing left to give: the battery goes back to automatic then, not at the end.

    Left in the forced mode it would idle there until the device's own
    timer lapses, and with the timer set to the window that is hours.
    """
    freezer.move_to(_local(23))
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    planner.settings.capacity_kwh = 10.0
    planner.settings.enabled = True

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_resume()
        # The fixture reports 55 % charge; a reserve above that is the
        # battery having arrived at it.
        planner.settings.target_pct = 90.0
        write.reset_mock()
        await planner.async_resume()

    assert [call.args for call in write.call_args_list] == [
        *AUTOMATIC,
        (124, [("InOutWRte_RvrtTms", 0)]),
    ]
    assert planner.planned_power_w == 0.0


async def _discharging_at_23(hass, client_mock):
    """A plan running mid-window, ready for a poll to interrupt it.

    Seven hours are left at 23:00 and the fixture's 10 kWh battery is at
    55 %, so the plan asks for 640 W. The fixture reports no revert
    timer, which is the point: nothing re-plans through the window on
    this device, so the polls are the only thing watching the reserve.
    """
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    planner.settings.capacity_kwh = 10.0
    planner.settings.enabled = True
    await planner.async_resume()
    assert planner.planned_power_w == 640.0
    return entry, planner


def _report_charge(client_mock, pct):
    """Have the device report a state of charge; the fixture scales by 10^-2."""
    client_mock.models[124][0].points["ChaState"].value = int(pct * 100)


async def test_a_poll_at_the_reserve_hands_the_battery_back(
    hass, sunspec_fronius_client_mock, freezer
):
    """The battery arrives at the reserve early, because the house drains it too.

    Before this the reserve was only a divisor in the power the plan
    worked out at the start. A battery that got there sooner than the
    arithmetic expected stayed in a forced discharge below it until the
    end of the window.
    """
    freezer.move_to(_local(23))
    entry, planner = await _discharging_at_23(hass, sunspec_fronius_client_mock)

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        _report_charge(sunspec_fronius_client_mock, 10.0)
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert planner.planned_power_w == 0.0
    assert [call.args for call in write.call_args_list] == [
        *AUTOMATIC,
        (124, [("InOutWRte_RvrtTms", 0)]),
    ]


async def test_a_poll_above_the_reserve_leaves_the_discharge_running(
    hass, sunspec_fronius_client_mock, freezer
):
    """The watch is for the reserve, not an excuse to rewrite the rate every poll."""
    freezer.move_to(_local(23))
    entry, planner = await _discharging_at_23(hass, sunspec_fronius_client_mock)

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        _report_charge(sunspec_fronius_client_mock, 40.0)
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done(wait_background_tasks=True)

    write.assert_not_called()
    assert planner.planned_power_w == 640.0


async def test_changed_window_is_applied_on_the_next_poll(
    hass, sunspec_fronius_client_mock, freezer
):
    freezer.move_to(_local(23))
    entry, planner = await _discharging_at_23(hass, sunspec_fronius_client_mock)
    times = {t._which: t for t in _entities(hass, "time", BatteryPlanTime)}

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await times["end"].async_set_value(time(2))
        write.assert_not_called()
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert planner.planned_power_w == 1500.0
    assert write.call_args_list[0].args == (124, [("InOutWRte_RvrtTms", 10800)])
    assert write.call_args_list[1].args == (124, [("OutWRte", 100.0), ("InWRte", -30.0)])


async def _set_plan(hass, entry, **values):
    await hass.services.async_call(
        "sunspec2",
        "set_battery_plan",
        {"config_entry_id": entry.entry_id, **values},
        blocking=True,
    )


async def test_one_action_configures_charging_and_updates_all_entities(
    hass, sunspec_fronius_client_mock, freezer
):
    freezer.move_to(_local(23))
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await _set_plan(
            hass,
            entry,
            direction="charge",
            start="22:00",
            end="02:00",
            target_pct=85,
            capacity_kwh=12,
            enabled=True,
        )
        write.assert_not_called()
        direction = _entities(hass, "select", BatteryPlanDirection)[0]
        target = _entities(hass, "number", BatteryPlanTargetNumber)[0]
        switch = _entities(hass, "switch", BatteryPlanSwitch)[0]
        assert hass.states.get(direction.entity_id).state == "charge"
        assert float(hass.states.get(target.entity_id).state) == 85
        assert hass.states.get(switch.entity_id).state == "on"
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert planner.planned_power_w == 1200
    assert [call.args for call in write.call_args_list] == [
        (124, [("InOutWRte_RvrtTms", 10800)]),
        (124, [("OutWRte", -24.0), ("InWRte", 100.0)]),
        (124, [("StorCtl_Mod", 2)]),
    ]
    assert entry.runtime_data.storage_setpoints["grid_charge"] == 1200


@pytest.mark.parametrize("soc,stops", [(79, False), (80, True), (95, True)])
async def test_charging_stops_at_the_target(hass, sunspec_fronius_client_mock, freezer, soc, stops):
    freezer.move_to(_local(23))
    entry = await _entry(hass)
    await _set_plan(hass, entry, direction="charge", target_pct=80, capacity_kwh=10, enabled=True)
    planner = entry.runtime_data.battery_plan
    await planner.async_resume()
    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        _report_charge(sunspec_fronius_client_mock, soc)
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done(wait_background_tasks=True)
    if stops:
        assert planner.planned_power_w == 0
        assert [call.args for call in write.call_args_list] == [
            *AUTOMATIC,
            (124, [("InOutWRte_RvrtTms", 0)]),
        ]
    else:
        write.assert_not_called()
        assert planner.planned_power_w == 350


async def test_changed_direction_and_target_replace_a_running_discharge(
    hass, sunspec_fronius_client_mock, freezer
):
    freezer.move_to(_local(23))
    entry, planner = await _discharging_at_23(hass, sunspec_fronius_client_mock)
    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await _set_plan(hass, entry, direction="charge", end="02:00", target_pct=85)
        write.assert_not_called()
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert planner.settings.start == time(20)
    assert planner.settings.capacity_kwh == 10
    assert planner.planned_power_w == 1000
    assert write.call_args_list[1].args == (124, [("OutWRte", -20.0), ("InWRte", 100.0)])
    assert write.call_args_list[2].args == (124, [("StorCtl_Mod", 2)])


async def test_moving_a_running_window_outside_now_stops_on_the_next_poll(
    hass, sunspec_fronius_client_mock, freezer
):
    freezer.move_to(_local(23))
    entry, planner = await _discharging_at_23(hass, sunspec_fronius_client_mock)
    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await _set_plan(hass, entry, start="10:00", end="16:00")
        write.assert_not_called()
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert planner.planned_power_w == 0
    assert [call.args for call in write.call_args_list] == [
        *AUTOMATIC,
        (124, [("InOutWRte_RvrtTms", 0)]),
    ]


async def test_disabling_with_a_new_window_stops_immediately(
    hass, sunspec_fronius_client_mock, freezer
):
    freezer.move_to(_local(23))
    entry, planner = await _discharging_at_23(hass, sunspec_fronius_client_mock)
    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await _set_plan(hass, entry, enabled=False, start="10:00", end="16:00")
    assert not planner.settings.enabled
    assert planner.planned_power_w == 0
    assert [call.args for call in write.call_args_list] == [
        *AUTOMATIC,
        (124, [("InOutWRte_RvrtTms", 0)]),
    ]


async def test_pending_changes_wait_for_successful_poll_and_use_latest_values(
    hass, sunspec_fronius_client_mock, freezer
):
    freezer.move_to(_local(23))
    entry, planner = await _discharging_at_23(hass, sunspec_fronius_client_mock)
    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await _set_plan(hass, entry, end="02:00", target_pct=25)
        entry.runtime_data.last_update_success = False
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done(wait_background_tasks=True)
        write.assert_not_called()
        await _set_plan(hass, entry, target_pct=40)
        entry.runtime_data.last_update_success = True
        entry.runtime_data.async_update_listeners()
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert planner.planned_power_w == 500
    assert write.call_count == 3


@pytest.mark.parametrize(
    "values",
    [
        {"target_pct": -1},
        {"target_pct": 101},
        {"target_pct": "nan"},
        {"capacity_kwh": "inf"},
        {"capacity_kwh": 0},
        {"direction": "both"},
        {"start": "25:00"},
        {"end": "22:30:45"},
        {"end": "22:00:00+02:00"},
    ],
)
async def test_invalid_action_changes_nothing(hass, sunspec_fronius_client_mock, values):
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    before = replace(planner.settings)
    with pytest.raises(vol.Invalid):
        await _set_plan(hass, entry, enabled=True, **values)
    assert planner.settings == before


async def test_action_rejects_unknown_and_unsupported_entries(hass, sunspec_write_client_mock):
    entry = await _entry(hass)
    for entry_id in ("missing", entry.entry_id):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "sunspec2",
                "set_battery_plan",
                {"config_entry_id": entry_id, "enabled": True},
                blocking=True,
            )


async def test_action_cannot_bypass_permissions(hass, sunspec_fronius_client_mock):
    entry = await _entry(hass)
    await hass.auth.async_create_user("Owner")
    user = await hass.auth.async_create_user("Restricted user", group_ids=[])
    assert not user.is_admin
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            "sunspec2",
            "set_battery_plan",
            {"config_entry_id": entry.entry_id, "enabled": True},
            blocking=True,
            context=Context(user_id=user.id),
        )
    assert not entry.runtime_data.battery_plan.settings.enabled


async def test_charging_plan_restores_after_reload(hass, sunspec_fronius_client_mock, freezer):
    freezer.move_to(_local(23))
    entry = await _entry(hass)
    await _set_plan(
        hass,
        entry,
        direction="charge",
        target_pct=85,
        capacity_kwh=12,
        start="22:00",
        end="02:00",
        enabled=True,
    )
    old_ids = {
        entity.unique_id: entity.entity_id
        for platform, cls in (
            ("select", BatteryPlanDirection),
            ("switch", BatteryPlanSwitch),
            ("number", BatteryPlanTargetNumber),
            ("time", BatteryPlanTime),
        )
        for entity in _entities(hass, platform, cls)
    }
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    planner = entry.runtime_data.battery_plan
    assert planner.settings.direction is PlanDirection.CHARGE
    assert planner.settings.target_pct == 85
    assert planner.settings.capacity_kwh == 12
    assert (planner.settings.start, planner.settings.end) == (time(22), time(2))
    assert planner.settings.enabled
    for unique_id, entity_id in old_ids.items():
        assert hass.states.get(entity_id) is not None, unique_id
    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        async_fire_time_changed(hass, _local(23) + timedelta(seconds=11))
        await hass.async_block_till_done(wait_background_tasks=True)
    assert planner.planned_power_w == 1200
    assert write.call_args_list[-1].args == (124, [("StorCtl_Mod", 2)])


async def test_a_full_day_plan_is_not_stopped_by_its_end_trigger(
    hass, sunspec_fronius_client_mock, freezer
):
    freezer.move_to(_local(12))
    entry = await _entry(hass)
    await _set_plan(hass, entry, start="20:00", end="20:00", capacity_kwh=10, enabled=True)
    planner = entry.runtime_data.battery_plan
    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await planner.async_resume()
        assert planner.planned_power_w == 560
        await planner.async_at_end()
    assert write.call_count == 3
    assert planner.planned_power_w == 560


async def test_the_polls_after_the_reserve_hand_nothing_back_twice(
    hass, sunspec_fronius_client_mock, freezer
):
    """Handed back once. The polls keep coming and must not keep writing."""
    freezer.move_to(_local(23))
    entry, planner = await _discharging_at_23(hass, sunspec_fronius_client_mock)
    _report_charge(sunspec_fronius_client_mock, 10.0)
    entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done(wait_background_tasks=True)

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        entry.runtime_data.async_update_listeners()
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done(wait_background_tasks=True)

    write.assert_not_called()


async def test_the_window_is_set_through_the_time_entities(hass, sunspec_fronius_client_mock):
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    times = {t._which: t for t in _entities(hass, "time", BatteryPlanTime)}

    await times["start"].async_set_value(time(21, 30))
    await times["end"].async_set_value(time(5, 0))

    assert (planner.settings.start, planner.settings.end) == (time(21, 30), time(5, 0))
    assert times["start"].native_value == time(21, 30)
    assert window_hours(planner.settings.start, planner.settings.end) == 7.5


async def test_the_reserve_and_capacity_are_set_through_the_numbers(
    hass, sunspec_fronius_client_mock
):
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
    reserve = _entities(hass, "number", BatteryPlanTargetNumber)[0]
    capacity = _entities(hass, "number", BatteryPlanCapacityNumber)[0]

    await reserve.async_set_native_value(25.0)
    await capacity.async_set_native_value(60.0)

    assert planner.settings.target_pct == 25.0
    assert planner.settings.capacity_kwh == 60.0
    assert capacity.native_value == 60.0


async def test_the_start_trigger_fires_at_the_start_of_the_window(
    hass, sunspec_fronius_client_mock, freezer
):
    """The clock is frozen before setup so the trigger is armed for the frozen day."""
    freezer.move_to(_local(19, 59, 55))
    entry = await _entry(hass)
    planner = entry.runtime_data.battery_plan
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
    planner = entry.runtime_data.battery_plan
    planner.settings.capacity_kwh = 10.0
    planner.settings.enabled = True
    planner.async_check_after_restore()

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        async_fire_time_changed(hass, _local(2) + timedelta(seconds=11))
        await hass.async_block_till_done(wait_background_tasks=True)

    assert planner.planned_power_w == 1120.0
    assert write.call_count == 3
