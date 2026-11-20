"""The blueprint that limits the inverter while the price is negative.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

from unittest.mock import patch

import pytest

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .blueprint_helpers import ENTRY_ID
from .blueprint_helpers import RecordedAction
from .blueprint_helpers import pass_time
from .blueprint_helpers import set_state
from .blueprint_helpers import start_automation
from .blueprint_helpers import sunspec_entity
from .const import MOCK_CONFIG_WRITE
from .const import TEST_NUMBER_EXPORT_LIMIT

BLUEPRINT = "limit_at_negative_price"
LIMIT = "number.inverter_export_limit"
PRICE = "sensor.price"

pytestmark = pytest.mark.usefixtures("repository_blueprints")


async def _start(hass, price="0.05", **inputs):
    """An inverter that answers, a price, and the automation on top of both."""
    sunspec_entity(hass, LIMIT, "100", friendly_name="Inverter Export limit")
    hass.states.async_set(PRICE, price, {"unit_of_measurement": "EUR/kWh"})
    action = RecordedAction(hass, "sunspec2", "set_export_limit")
    await start_automation(hass, BLUEPRINT, {"export_limit": LIMIT, "price": PRICE, **inputs})
    return action


async def test_the_limit_follows_the_price_across_the_threshold(hass):
    """Below the threshold the limit is written once, above it the normal value once."""
    action = await _start(hass)

    await set_state(hass, PRICE, "-0.01")
    assert action.calls == [{"config_entry_id": ENTRY_ID, "percent": 0, "enable": True}]

    # The next hour is negative as well: nothing new to write.
    await set_state(hass, PRICE, "-0.03")
    assert len(action.calls) == 1

    await set_state(hass, PRICE, "0.02")
    assert action.calls[1:] == [{"config_entry_id": ENTRY_ID, "percent": 100, "enable": True}]

    await set_state(hass, PRICE, "0.04")
    assert len(action.calls) == 2


async def test_a_price_exactly_at_the_threshold_is_not_low(hass):
    action = await _start(hass, price="0.05", threshold=0)

    await set_state(hass, PRICE, "0")

    assert action.calls == []


async def test_the_percentages_and_the_enable_flag_come_from_the_inputs(hass):
    action = await _start(
        hass, threshold=0.02, limit_percent=30, normal_percent=110, send_enable=False
    )

    await set_state(hass, PRICE, "0.01")
    await set_state(hass, PRICE, "0.03")

    assert [(call["percent"], call["enable"]) for call in action.calls] == [
        (30, False),
        (110, False),
    ]


async def test_a_price_that_returns_after_a_gap_is_applied(hass):
    """The first number after "unavailable" counts, whichever side it is on."""
    action = await _start(hass)

    await set_state(hass, PRICE, "unavailable")
    assert action.calls == []

    await set_state(hass, PRICE, "0.06")
    assert [call["percent"] for call in action.calls] == [100]


async def test_an_hour_without_a_price_ends_the_limit(hass, freezer):
    action = await _start(hass, price="-0.02")
    await set_state(hass, PRICE, "-0.05")
    await set_state(hass, PRICE, "unavailable")

    await pass_time(hass, freezer, minutes=59)
    assert action.calls == []

    await pass_time(hass, freezer, minutes=2)
    assert [call["percent"] for call in action.calls] == [100]


async def test_an_inverter_that_is_off_is_written_when_it_answers_again(hass):
    """A price that turns negative before sunrise still limits the inverter that day."""
    action = await _start(hass)
    await set_state(hass, LIMIT, "unavailable")

    await set_state(hass, PRICE, "-0.01")
    assert action.calls == []

    await set_state(hass, LIMIT, "100")
    assert [call["percent"] for call in action.calls] == [0]


async def test_home_assistant_starting_applies_the_current_price(hass):
    """A limit left behind by a restart during low prices is taken back."""
    action = await _start(hass, price="0.05")

    hass.bus.async_fire("homeassistant_started")
    await hass.async_block_till_done()

    assert [call["percent"] for call in action.calls] == [100]


async def test_a_failed_write_is_tried_again(hass, freezer):
    action = await _start(hass)
    action.failures = 2

    await set_state(hass, PRICE, "-0.01", block=False)
    assert len(action.calls) == 1
    await pass_time(hass, freezer, block=False, seconds=31)
    assert len(action.calls) == 2
    await pass_time(hass, freezer, seconds=31)

    assert [call["percent"] for call in action.calls] == [0, 0, 0]
    # The third attempt went through, so nothing is left to do.
    await pass_time(hass, freezer, seconds=31)
    assert len(action.calls) == 3


async def test_after_three_failures_the_write_is_tried_every_five_minutes(hass, freezer):
    hass.states.async_set("notify.phone", "unknown")
    messages = RecordedAction(hass, "notify", "send_message")
    action = await _start(hass, notify=["notify.phone"])
    action.failures = 4

    await set_state(hass, PRICE, "-0.01", block=False)
    await pass_time(hass, freezer, block=False, seconds=31)
    await pass_time(hass, freezer, block=False, seconds=31)
    assert len(action.calls) == 3
    assert [call["message"] for call in messages.calls] == [
        "The export limit of 0 % could not be written in three attempts. "
        "The automation keeps trying every five minutes for two hours."
    ]

    await pass_time(hass, freezer, block=False, minutes=4)
    assert len(action.calls) == 3
    await pass_time(hass, freezer, block=False, minutes=1, seconds=1)
    assert len(action.calls) == 4
    await pass_time(hass, freezer, minutes=5, seconds=1)

    assert len(action.calls) == 5
    assert messages.calls[-1]["message"] == "Export limit set to 0 % (-0.01 EUR/kWh)."


async def test_a_limit_that_could_not_be_taken_back_is_taken_back_later(hass, freezer):
    """The case that must not be given up: the inverter would stay limited for good."""
    action = await _start(hass, price="-0.01")
    await set_state(hass, PRICE, "-0.02")
    action.failures = 3

    await set_state(hass, PRICE, "0.03", block=False)
    await pass_time(hass, freezer, block=False, seconds=31)
    await pass_time(hass, freezer, block=False, seconds=31)
    # Normal prices come in. None of them crosses the threshold.
    await set_state(hass, PRICE, "0.04", block=False)
    await pass_time(hass, freezer, minutes=5, seconds=1)

    assert [call["percent"] for call in action.calls] == [100, 100, 100, 100]


async def test_a_later_attempt_writes_what_fits_the_price_by_then(hass, freezer):
    action = await _start(hass, price="-0.01")
    action.failures = 3

    await set_state(hass, PRICE, "0.03", block=False)
    await pass_time(hass, freezer, block=False, seconds=31)
    await pass_time(hass, freezer, block=False, seconds=31)
    # The price is low again before the inverter took the 100 %.
    await set_state(hass, PRICE, "-0.05", block=False)
    await pass_time(hass, freezer, minutes=5, seconds=1)

    # The fourth attempt of the first run, then the run of the second crossing.
    assert [call["percent"] for call in action.calls] == [100, 100, 100, 0, 0]


async def test_the_attempts_end_when_the_inverter_goes_off(hass, freezer):
    action = await _start(hass)
    action.failures = 3

    await set_state(hass, PRICE, "-0.01", block=False)
    await pass_time(hass, freezer, block=False, seconds=31)
    await pass_time(hass, freezer, block=False, seconds=31)
    await set_state(hass, LIMIT, "unavailable", block=False)
    await pass_time(hass, freezer, minutes=5, seconds=1)
    assert len(action.calls) == 3

    # When it answers again, the limit is written after all.
    await set_state(hass, LIMIT, "100")
    assert [call["percent"] for call in action.calls] == [0, 0, 0, 0]


async def test_after_two_hours_of_failures_the_automation_gives_up(hass, freezer, caplog):
    hass.states.async_set("notify.phone", "unknown")
    messages = RecordedAction(hass, "notify", "send_message")
    action = await _start(hass, notify=["notify.phone"])
    action.failures = 100

    await set_state(hass, PRICE, "-0.01", block=False)
    await pass_time(hass, freezer, block=False, seconds=31)
    await pass_time(hass, freezer, block=False, seconds=31)
    for _ in range(24):
        await pass_time(hass, freezer, block=False, minutes=5, seconds=1)
    await hass.async_block_till_done()

    assert len(action.calls) == 27
    assert "has given up" in messages.calls[-1]["message"]
    assert "did not take the export limit for two hours" in caplog.text


async def test_a_price_that_is_not_a_number_is_treated_as_no_price(hass, caplog):
    action = await _start(hass)

    await set_state(hass, PRICE, "nan")
    await set_state(hass, PRICE, "inf")

    assert action.calls == []
    assert "Error rendering variables" not in caplog.text


async def test_a_sensor_without_a_price_from_the_start_ends_the_limit_after_an_hour(hass, freezer):
    freezer.move_to("2030-06-01 12:00:00+00:00")
    action = await _start(hass, price="unavailable")

    for _ in range(59):
        await pass_time(hass, freezer, minutes=1)
    assert action.calls == []

    await pass_time(hass, freezer, minutes=1)
    assert [call["percent"] for call in action.calls] == [100]


async def test_run_actions_from_the_menu_writes_what_fits_the_price(hass):
    action = await _start(hass, price="-0.01")

    await hass.services.async_call(
        "automation",
        "trigger",
        {"entity_id": f"automation.{BLUEPRINT}", "skip_condition": True},
        blocking=True,
    )

    assert [call["percent"] for call in action.calls] == [0]


async def test_run_actions_from_the_menu_without_a_price_does_nothing(hass, caplog):
    action = await _start(hass, price="unavailable")

    await hass.services.async_call(
        "automation",
        "trigger",
        {"entity_id": f"automation.{BLUEPRINT}", "skip_condition": True},
        blocking=True,
    )

    assert action.calls == []
    assert "Error" not in caplog.text


async def test_a_message_names_the_limit_and_the_price(hass):
    hass.states.async_set("notify.phone", "unknown")
    messages = RecordedAction(hass, "notify", "send_message")
    await _start(hass, notify=["notify.phone"])

    await set_state(hass, PRICE, "-0.01")

    assert messages.calls == [
        {
            "entity_id": ["notify.phone"],
            "title": "Inverter Export limit",
            "message": "Export limit set to 0 % (-0.01 EUR/kWh).",
        }
    ]


async def test_the_limit_is_written_again_every_period_while_the_price_is_low(hass, freezer):
    freezer.move_to("2030-06-01 12:02:00+00:00")
    action = await _start(hass, price="-0.01", refresh="5")

    for _ in range(11):
        await pass_time(hass, freezer, minutes=1)
    # 12:06 and 12:11.
    assert [call["percent"] for call in action.calls] == [0, 0]

    await set_state(hass, PRICE, "0.03")
    for _ in range(10):
        await pass_time(hass, freezer, minutes=1)
    # Only the write that ended the limit.
    assert [call["percent"] for call in action.calls] == [0, 0, 100]


async def test_a_price_that_falls_on_the_quarter_hour_is_written_once(hass, freezer):
    """The periods avoid the minutes in which prices change."""
    freezer.move_to("2030-06-01 12:15:00+00:00")
    action = await _start(hass, refresh="5")

    await set_state(hass, PRICE, "-0.01")

    assert [call["percent"] for call in action.calls] == [0]


async def test_nothing_is_written_again_without_a_period(hass, freezer):
    freezer.move_to("2030-06-01 12:01:00+00:00")
    action = await _start(hass, price="-0.01")

    for _ in range(30):
        await pass_time(hass, freezer, minutes=1)

    assert action.calls == []


async def test_the_real_action_takes_what_the_blueprint_sends(hass, sunspec_write_client_mock):
    """End to end: the blueprint, the integration's own action, the write to the device."""
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    hass.states.async_set(PRICE, "0.05")
    await start_automation(
        hass, BLUEPRINT, {"export_limit": TEST_NUMBER_EXPORT_LIMIT, "price": PRICE}
    )
    coordinator = entry.runtime_data
    written = []

    async def spy(model_id, points):
        written.append((model_id, points))

    with (
        patch.object(coordinator.api, "async_write_points", side_effect=spy),
        patch.object(coordinator, "async_request_refresh"),
    ):
        await set_state(hass, PRICE, "-0.01")

    assert written == [(123, [("WMaxLimPct", 0.0), ("WMaxLim_Ena", 1)])]
