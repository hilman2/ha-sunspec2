"""The blueprint that reports an inverter that is off, in a fault state or raising events.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

import pytest
from homeassistant.core import callback

from .blueprint_helpers import RecordedAction
from .blueprint_helpers import pass_time
from .blueprint_helpers import set_state
from .blueprint_helpers import start_automation
from .blueprint_helpers import sunspec_entity

BLUEPRINT = "inverter_alert"
POWER = "sensor.inverter_ac_power"
STATE = "sensor.inverter_operating_state"
EVENTS = "sensor.inverter_events"

pytestmark = pytest.mark.usefixtures("repository_blueprints")


async def _start(hass, elevation=40.0, **inputs):
    """A running inverter at midday and somebody to tell."""
    hass.states.async_set("sun.sun", "above_horizon", {"elevation": elevation})
    hass.states.async_set("notify.phone", "unknown")
    sunspec_entity(hass, POWER, "2500", friendly_name="Inverter AC power")
    sunspec_entity(hass, STATE, "MPPT", friendly_name="Inverter Operating state")
    sunspec_entity(hass, EVENTS, "", friendly_name="Inverter Events", active_flags=[])
    messages = RecordedAction(hass, "notify", "send_message")
    await start_automation(hass, BLUEPRINT, {"notify": ["notify.phone"], **inputs})
    return messages


def _texts(messages):
    return [call["message"] for call in messages.calls]


async def _minutes(hass, freezer, count):
    """Pass the time minute by minute: a template that reads now() is checked once a minute."""
    for _ in range(count):
        await pass_time(hass, freezer, minutes=1)


async def test_an_inverter_that_stops_answering_during_the_day(hass, freezer):
    freezer.move_to("2030-06-01 12:00:00+00:00")
    messages = await _start(hass, watch=POWER)

    await set_state(hass, POWER, "unavailable")
    await _minutes(hass, freezer, 14)
    assert _texts(messages) == []

    await _minutes(hass, freezer, 2)
    assert messages.calls == [
        {
            "entity_id": ["notify.phone"],
            "title": "Inverter AC power",
            "message": "The inverter has not answered for 15 minutes.",
        }
    ]

    await _minutes(hass, freezer, 20)
    await set_state(hass, POWER, "2400")
    assert _texts(messages)[1:] == ["The inverter answers again after 36 minutes."]


async def test_a_short_gap_is_no_reason_for_a_message(hass, freezer):
    freezer.move_to("2030-06-01 12:00:00+00:00")
    messages = await _start(hass, watch=POWER)

    await set_state(hass, POWER, "unavailable")
    await _minutes(hass, freezer, 5)
    await set_state(hass, POWER, "2400")
    await _minutes(hass, freezer, 20)

    assert _texts(messages) == []


async def test_an_inverter_that_sleeps_at_night_is_left_alone(hass, freezer):
    """Off at dusk, back at dawn: neither is a message."""
    freezer.move_to("2030-06-01 20:00:00+00:00")
    messages = await _start(hass, elevation=4.0, watch=POWER)

    await set_state(hass, POWER, "unavailable")
    await _minutes(hass, freezer, 30)
    await set_state(hass, "sun.sun", "below_horizon", elevation=-12.0)
    await _minutes(hass, freezer, 30)
    await set_state(hass, "sun.sun", "above_horizon", elevation=3.0)
    await set_state(hass, POWER, "40")

    assert _texts(messages) == []


async def test_an_inverter_that_does_not_come_back_in_the_morning(hass, freezer):
    freezer.move_to("2030-06-01 20:00:00+00:00")
    messages = await _start(hass, elevation=4.0, watch=POWER)
    await set_state(hass, POWER, "unavailable")
    await _minutes(hass, freezer, 30)
    assert _texts(messages) == []

    await set_state(hass, "sun.sun", "above_horizon", elevation=10.5)

    assert _texts(messages) == ["The inverter has not answered for 15 minutes."]


async def test_an_inverter_that_runs_at_night_is_watched_at_night(hass, freezer):
    freezer.move_to("2030-06-01 23:00:00+00:00")
    messages = await _start(hass, elevation=-30.0, watch=POWER, min_elevation=-90)

    await set_state(hass, POWER, "unavailable")
    await _minutes(hass, freezer, 16)

    assert _texts(messages) == ["The inverter has not answered for 15 minutes."]


async def test_a_fault_state_and_its_end(hass):
    messages = await _start(hass, operating_state=STATE)

    await set_state(hass, STATE, "THROTTLED")
    assert _texts(messages) == []

    await set_state(hass, STATE, "FAULT")
    assert messages.calls == [
        {
            "entity_id": ["notify.phone"],
            "title": "Inverter Operating state",
            "message": "The inverter reports the state FAULT.",
        }
    ]

    # The same state with another attribute is not a second fault.
    await set_state(hass, STATE, "FAULT", raw=7)
    assert len(messages.calls) == 1

    # A sensor that goes away is not the end of the fault.
    await set_state(hass, STATE, "unavailable")
    assert len(messages.calls) == 1

    await set_state(hass, STATE, "FAULT")
    assert len(messages.calls) == 2
    await set_state(hass, STATE, "STARTING")
    assert _texts(messages)[2:] == [
        "The inverter is out of the state FAULT. It now reports STARTING."
    ]


async def test_the_states_that_count_as_a_problem_are_an_input(hass):
    messages = await _start(hass, operating_state=STATE, fault_states=["FAULT", "OFF"])

    await set_state(hass, STATE, "OFF")

    assert _texts(messages) == ["The inverter reports the state OFF."]


async def test_events_are_reported_once_when_they_are_raised(hass):
    messages = await _start(hass, events=[EVENTS], ignored_events=["MANUAL_SHUTDOWN"])

    await set_state(hass, EVENTS, "GROUND_FAULT", active_flags=["GROUND_FAULT"])
    assert _texts(messages) == ["The inverter reports: GROUND_FAULT."]

    await set_state(
        hass,
        EVENTS,
        "GROUND_FAULT,DC_OVER_VOLT,MANUAL_SHUTDOWN",
        active_flags=["GROUND_FAULT", "DC_OVER_VOLT", "MANUAL_SHUTDOWN"],
    )
    assert _texts(messages)[1:] == ["The inverter reports: DC_OVER_VOLT."]

    await set_state(hass, EVENTS, "MANUAL_SHUTDOWN", active_flags=["MANUAL_SHUTDOWN"])
    assert _texts(messages)[2:] == ["The inverter no longer reports an event."]


async def test_an_ignored_event_alone_is_no_message(hass):
    messages = await _start(hass, events=[EVENTS], ignored_events=["MANUAL_SHUTDOWN"])

    await set_state(hass, EVENTS, "MANUAL_SHUTDOWN", active_flags=["MANUAL_SHUTDOWN"])
    await set_state(hass, EVENTS, "", active_flags=[])

    assert _texts(messages) == []


async def test_the_end_of_a_problem_can_stay_silent(hass):
    messages = await _start(hass, operating_state=STATE, events=[EVENTS], recovery=False)

    await set_state(hass, STATE, "FAULT")
    await set_state(hass, STATE, "MPPT")
    await set_state(hass, EVENTS, "GROUND_FAULT", active_flags=["GROUND_FAULT"])
    await set_state(hass, EVENTS, "", active_flags=[])

    assert _texts(messages) == [
        "The inverter reports the state FAULT.",
        "The inverter reports: GROUND_FAULT.",
    ]


async def test_further_actions_get_the_text_and_the_kind_of_problem(hass):
    seen = []
    hass.bus.async_listen("blueprint_test", callback(lambda event: seen.append(event.data)))
    await _start(
        hass,
        operating_state=STATE,
        notify=[],
        extra_actions=[
            {
                "event": "blueprint_test",
                "event_data": {
                    "title": "{{ title }}",
                    "message": "{{ message }}",
                    "problem": "{{ problem }}",
                    "over": "{{ over }}",
                },
            }
        ],
    )

    await set_state(hass, STATE, "FAULT")
    await set_state(hass, STATE, "MPPT")

    assert seen == [
        {
            "title": "Inverter Operating state",
            "message": "The inverter reports the state FAULT.",
            "problem": "fault",
            "over": False,
        },
        {
            "title": "Inverter Operating state",
            "message": "The inverter is out of the state FAULT. It now reports MPPT.",
            "problem": "fault",
            "over": True,
        },
    ]
