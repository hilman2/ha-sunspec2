"""The blueprint that sets the export limit to what the house uses.

A run of this automation is a row of rounds. In each round it waits for
the meter, reads the inverter, writes the limit and then waits a
period of 30 seconds. A run is in a wait almost all the time, so these
tests never wait for the automation to finish: they move the clock
second by second and look at what has been written by then.

Every test starts ten seconds after a full minute. The tick 20 seconds
later starts a run, and the first reading of the meter that counts is
one from ten seconds into the run. So the first reading of a test
comes 31 seconds after its start.

``test_blueprint_follow_plant.py`` runs the same automation against a
model of a house, where every write changes what the meter reads next.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.setup import async_setup_component

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .blueprint_helpers import ENTRY_ID
from .blueprint_helpers import RecordedAction
from .blueprint_helpers import pass_time
from .blueprint_helpers import set_state
from .blueprint_helpers import settle
from .blueprint_helpers import start_automation
from .blueprint_helpers import sunspec_entity
from .const import MOCK_CONFIG_WRITE
from .const import TEST_NUMBER_EXPORT_LIMIT

BLUEPRINT = "follow_house_consumption"
LIMIT = "number.inverter_export_limit"
INVERTER = "sensor.inverter_power"
GRID = "sensor.grid_power"
PRICE = "sensor.price"

pytestmark = pytest.mark.usefixtures("repository_blueprints")


async def _start(hass, freezer, inverter="3000", grid="-1990", step=1.0, **inputs):
    """An 8 kW inverter delivering 3 kW, of which 2 kW go into the grid."""
    freezer.move_to("2030-06-01 12:00:10+00:00")
    assert await async_setup_component(hass, "homeassistant", {})
    sunspec_entity(hass, LIMIT, "100", friendly_name="Inverter Export limit", step=step)
    hass.states.async_set(INVERTER, inverter, {"unit_of_measurement": "W"})
    hass.states.async_set(GRID, grid, {"unit_of_measurement": "W"})
    action = RecordedAction(hass, "sunspec2", "set_export_limit")
    await start_automation(
        hass,
        BLUEPRINT,
        {
            "export_limit": LIMIT,
            "inverter_power": INVERTER,
            "grid_power": GRID,
            "rated_power": 8000,
            **inputs,
        },
    )
    return action


async def _wait(hass, freezer, seconds):
    """Pass time while a run is in a wait, second by second.

    A step of several seconds would end a wait late: a timer only fires
    when the clock is moved, and what follows it would then happen at
    the end of the step.
    """
    for _ in range(seconds):
        await pass_time(hass, freezer, block=False, seconds=1)


async def _report(hass, freezer, value=None):
    """Let the meter report, and pass the second in which the automation notices.

    Args:
        value: The new value. Without one the meter reports the value
            it had, which is what a meter with a coarse resolution does
            while the power is steady. Home Assistant then only notes
            that it reported.
    """
    state = hass.states.get(GRID)
    hass.states.async_set(GRID, state.state if value is None else value, state.attributes)
    # A round looks for a report of the meter once a second.
    await _wait(hass, freezer, 1)


def _percents(action):
    return [call["percent"] for call in action.calls]


async def _meter_reports_for(hass, freezer, seconds, values=("-2000", "-2001")):
    """Let the meter report every five seconds, a value that changes a little each time."""
    for index in range(seconds // 5):
        await _wait(hass, freezer, 4)
        await _report(hass, freezer, values[index % 2])


async def _the_inverter_follows(hass):
    """What the house looks like once the inverter runs at 12 %: 960 W, 40 W from the grid."""
    hass.states.async_set(INVERTER, "960", {"unit_of_measurement": "W"})
    await settle(hass)


async def _first_limit(hass, freezer, action, grid="-2000"):
    """The limit of the first round, written when the meter reports ``grid``."""
    await _wait(hass, freezer, 30)
    await _report(hass, freezer, grid)
    return _percents(action)


# ---------------------------------------------------------------------------
# What a round works out
# ---------------------------------------------------------------------------


async def test_the_limit_is_what_the_house_uses(hass, freezer):
    """3000 W delivered and 2000 W fed in: the house uses 1000 W, 12.5 % of 8000 W."""
    action = await _start(hass, freezer)

    await _wait(hass, freezer, 19)
    assert action.calls == []

    # The tick starts a run, and its first round waits for the meter.
    await _wait(hass, freezer, 12)
    assert action.calls == []

    await _report(hass, freezer, "-2000")
    # Rounded down to the whole percent the entity takes.
    assert action.calls == [{"config_entry_id": ENTRY_ID, "percent": 12, "enable": True}]


async def test_power_taken_from_the_grid_raises_the_limit(hass, freezer):
    """960 W delivered while the house takes another 2040 W from the grid."""
    action = await _start(hass, freezer, inverter="960", grid="2000")

    assert await _first_limit(hass, freezer, action, "2040") == [37]


async def test_a_meter_that_counts_feed_in_as_positive(hass, freezer):
    action = await _start(hass, freezer, grid="1990", grid_direction="export")

    assert await _first_limit(hass, freezer, action, "2000") == [12]


async def test_readings_in_kilowatts(hass, freezer):
    action = await _start(hass, freezer)
    await set_state(hass, INVERTER, "3.0", unit_of_measurement="kW")
    await set_state(hass, GRID, "-1.9", unit_of_measurement="kW")

    assert await _first_limit(hass, freezer, action, "-2.0") == [12]


async def test_the_allowed_feed_in_is_added(hass, freezer):
    """1000 W for the house and 600 W for the grid are 20 % of 8000 W."""
    action = await _start(hass, freezer, max_export=600)

    assert await _first_limit(hass, freezer, action) == [20]


async def test_the_limit_uses_the_step_of_the_entity(hass, freezer):
    action = await _start(hass, freezer, step=0.1)

    assert await _first_limit(hass, freezer, action) == [12.5]


@pytest.mark.parametrize(
    ("inverter", "grid", "expected"),
    [
        # The house uses more than the inverter can deliver.
        ("7000", "3000", 100),
        # Nothing is used: the limit stops at the lowest one allowed.
        ("2000", "-2000", 5),
    ],
)
async def test_the_limit_stays_between_the_lowest_and_full_power(
    hass, freezer, inverter, grid, expected
):
    action = await _start(hass, freezer, inverter=inverter, grid="0", min_percent=5)

    assert await _first_limit(hass, freezer, action, grid) == [expected]


async def test_the_inverter_power_counts_up_to_the_limit_last_written(hass, freezer):
    """A reading above that limit is from before it, whatever Home Assistant says of its age."""
    action = await _start(hass, freezer)
    assert await _first_limit(hass, freezer, action) == [12]

    # The second round. The inverter delivers 960 W by now and the
    # house takes 40 W from the grid, but the sensor still says 3000 W.
    await _wait(hass, freezer, 25)
    await _report(hass, freezer, "40")
    await _wait(hass, freezer, 5)

    # 960 W and 40 W, not 3000 W and 40 W.
    assert _percents(action) == [12, 12]


@pytest.mark.parametrize("missing", [INVERTER, GRID])
async def test_without_a_reading_the_fallback_is_written(hass, freezer, missing):
    action = await _start(hass, freezer, fallback_percent=0)
    await set_state(hass, missing, "unavailable")

    if missing == GRID:
        # Without a grid power there is nothing to wait for.
        await _wait(hass, freezer, 20)
        assert _percents(action) == [0]
    else:
        assert await _first_limit(hass, freezer, action) == [0]


# ---------------------------------------------------------------------------
# When a round has a reading of the meter
# ---------------------------------------------------------------------------


async def test_the_limit_is_written_once_per_period(hass, freezer):
    """A meter that reports every five seconds does not get a write every five seconds."""
    action = await _start(hass, freezer)
    await _wait(hass, freezer, 20)

    await _meter_reports_for(hass, freezer, 15)
    assert _percents(action) == [12]
    await _the_inverter_follows(hass)
    await _meter_reports_for(hass, freezer, 70, values=("40", "41"))

    # 85 seconds: ten seconds into the run, and twice 30 seconds later.
    assert _percents(action) == [12, 12, 12]


async def test_a_longer_period_writes_less_often(hass, freezer):
    action = await _start(hass, freezer, interval="60")
    await _wait(hass, freezer, 20)

    await _meter_reports_for(hass, freezer, 10)
    await _the_inverter_follows(hass)
    await _meter_reports_for(hass, freezer, 135, values=("40", "41"))

    # 145 seconds: ten seconds into the run, and twice a minute later.
    assert _percents(action) == [12, 12, 12]


async def test_a_meter_that_repeats_its_value_still_gets_a_limit(hass, freezer):
    """A report of the value the meter had changes no state, and is found all the same."""
    action = await _start(hass, freezer)
    await _wait(hass, freezer, 35)
    assert action.calls == []

    await _report(hass, freezer)

    assert _percents(action) == [12]


async def test_a_meter_that_repeats_its_value_for_ten_minutes_is_not_one_that_hangs(hass, freezer):
    """What tells the two apart is the time of the last report, not of the last new value."""
    action = await _start(hass, freezer, fallback_percent=0)
    assert await _first_limit(hass, freezer, action) == [12]
    await _the_inverter_follows(hass)
    await _report(hass, freezer, "40")

    for _ in range(11 * 12):
        await _wait(hass, freezer, 4)
        await _report(hass, freezer)

    assert set(_percents(action)) == {12}
    assert len(action.calls) > 20


async def test_a_reading_of_the_meter_older_than_eight_seconds_is_not_used(hass, freezer):
    """The inverter is read now, and the house may look different than ten seconds ago."""
    action = await _start(hass, freezer)
    assert await _first_limit(hass, freezer, action) == [12]
    await _the_inverter_follows(hass)

    # The next round begins 30 seconds after the write. The meter
    # reports ten seconds before that and then not again.
    await _wait(hass, freezer, 19)
    await _report(hass, freezer, "40")
    await _wait(hass, freezer, 36)
    assert len(action.calls) == 1

    await _report(hass, freezer, "41")
    assert _percents(action) == [12, 12]


async def test_after_a_round_without_a_write_the_inverter_is_not_held_to_the_old_limit(
    hass, freezer
):
    """The meter was silent for longer than the revert time: the inverter is at full power."""
    action = await _start(hass, freezer)
    assert await _first_limit(hass, freezer, action) == [12]

    # The period after the write and a round of waiting in vain.
    await _wait(hass, freezer, 62)
    assert len(action.calls) == 1
    hass.states.async_set(INVERTER, "6000", {"unit_of_measurement": "W"})
    await _report(hass, freezer, "-5000")

    # 6000 W and 5000 W fed in are 1000 W, and not 960 W less 5000 W.
    assert _percents(action) == [12, 12]


async def test_after_a_failed_write_the_inverter_is_not_held_to_the_old_limit(hass, freezer):
    action = await _start(hass, freezer)
    assert await _first_limit(hass, freezer, action) == [12]
    await _the_inverter_follows(hass)
    action.failures = 1
    await _meter_reports_for(hass, freezer, 30, values=("40", "41"))
    assert len(action.calls) == 2

    # The inverter dropped the limit in the meantime.
    hass.states.async_set(INVERTER, "6000", {"unit_of_measurement": "W"})
    await _meter_reports_for(hass, freezer, 30, values=("-5000", "-5001"))

    assert _percents(action) == [12, 12, 12]


async def test_a_round_without_a_report_of_the_meter_writes_nothing(hass, freezer):
    """A meter that reports once a minute gets a new limit once a minute."""
    action = await _start(hass, freezer)
    assert len(await _first_limit(hass, freezer, action)) == 1

    # The period after the write, and a round of waiting in vain.
    await _wait(hass, freezer, 62)
    assert len(action.calls) == 1

    await _wait(hass, freezer, 5)
    await _report(hass, freezer, "-2020")
    assert len(action.calls) == 2


@pytest.mark.parametrize("phase", [1, 6, 12, 27, 29])
async def test_no_phase_of_the_meter_against_the_tick_goes_without_a_limit(hass, freezer, phase):
    """A meter that reports every 30 seconds, ``phase`` seconds after each tick."""
    action = await _start(hass, freezer)
    await _wait(hass, freezer, 20)

    for index in range(6):
        await _wait(hass, freezer, phase)
        await _report(hass, freezer, str(-2001 - index))
        await _wait(hass, freezer, 30 - phase)

    # A limit with every report. The first report is the one exception
    # when it comes in the first ten seconds of the run.
    assert len(action.calls) == (5 if phase < 10 else 6)


async def test_a_reading_from_the_first_seconds_of_a_run_does_not_count(hass, freezer):
    """The run that took a limit back may just have ended, and the meter still shows that."""
    action = await _start(hass, freezer)
    await _wait(hass, freezer, 25)
    await _report(hass, freezer, "-5000")
    await _wait(hass, freezer, 4)
    assert action.calls == []

    await _wait(hass, freezer, 2)
    await _report(hass, freezer, "-2000")
    assert _percents(action) == [12]


async def test_the_meter_is_read_before_the_inverter_and_never_asked(hass, freezer):
    """The inverter is asked for its power, once, between the meter's report and the write."""
    action = await _start(hass, freezer)
    asked = []
    hass.services.async_register(
        "homeassistant",
        "update_entity",
        lambda call: asked.append((call.data["entity_id"], len(action.calls))),
    )

    await _wait(hass, freezer, 31)
    assert asked == []
    await _report(hass, freezer, "-2000")

    assert asked == [([INVERTER], 0)]
    assert len(action.calls) == 1


class _Meter(SensorEntity):
    """A grid meter as an entity that counts how often it is asked for an update."""

    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_should_poll = False

    def __init__(self) -> None:
        self.entity_id = GRID
        self._attr_native_value = -2000.0
        self.asked = 0

    async def async_update(self) -> None:
        self.asked += 1


async def test_a_meter_entity_that_reports_its_old_value_counts_as_reported(hass, freezer):
    """With a real entity: writing the same state is a report, and nobody asks the meter."""
    freezer.move_to("2030-06-01 12:00:10+00:00")
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "sensor", {})
    meter = _Meter()
    await hass.data["sensor"].async_add_entities([meter])
    sunspec_entity(hass, LIMIT, "100", step=1.0)
    hass.states.async_set(INVERTER, "3000", {"unit_of_measurement": "W"})
    action = RecordedAction(hass, "sunspec2", "set_export_limit")
    await start_automation(
        hass,
        BLUEPRINT,
        {
            "export_limit": LIMIT,
            "inverter_power": INVERTER,
            "grid_power": GRID,
            "rated_power": 8000,
        },
    )

    # A round in which the meter says nothing.
    await _wait(hass, freezer, 51)
    assert action.calls == []

    # A round in which it reports the value it had.
    await _wait(hass, freezer, 10)
    meter.async_write_ha_state()
    await _wait(hass, freezer, 20)
    assert _percents(action) == [12]
    assert meter.asked == 0


async def test_a_meter_that_hangs_counts_as_no_reading(hass, freezer):
    """After ten minutes without a report the fallback is written."""
    action = await _start(hass, freezer, fallback_percent=0)
    assert await _first_limit(hass, freezer, action) == [12]

    await _wait(hass, freezer, 595)
    assert len(action.calls) == 1

    await _wait(hass, freezer, 40)
    assert _percents(action)[:2] == [12, 0]


# ---------------------------------------------------------------------------
# When a run starts and ends
# ---------------------------------------------------------------------------


async def test_an_inverter_that_is_off_gets_no_write(hass, freezer):
    action = await _start(hass, freezer)
    await set_state(hass, LIMIT, "unavailable")

    assert await _first_limit(hass, freezer, action) == []


async def test_a_run_ends_when_the_inverter_goes_off(hass, freezer):
    action = await _start(hass, freezer)
    assert len(await _first_limit(hass, freezer, action)) == 1

    await set_state(hass, LIMIT, "unavailable", block=False)
    for index in range(8):
        await _wait(hass, freezer, 10)
        await _report(hass, freezer, str(-2020 - index))

    assert len(action.calls) == 1


async def test_with_a_price_the_limit_only_follows_while_the_price_is_low(hass, freezer):
    hass.states.async_set(PRICE, "0.05", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("notify.phone", "unknown")
    messages = RecordedAction(hass, "notify", "send_message")
    action = await _start(hass, freezer, price=PRICE, notify=["notify.phone"])

    assert await _first_limit(hass, freezer, action) == []

    await set_state(hass, PRICE, "-0.01", block=False)
    assert action.calls == []
    assert [call["message"] for call in messages.calls] == [
        "The price is -0.01 EUR/kWh. The feed-in is limited to 0 W."
    ]
    # The next tick starts a run, 29 seconds from now.
    for index in range(9):
        await _wait(hass, freezer, 5)
        await _report(hass, freezer, str(-2010 - index))
    assert _percents(action) == [12]

    # The price recovers. The run ends, and the limit is taken back once.
    await set_state(hass, PRICE, "0.02", block=False)
    for index in range(14):
        await _wait(hass, freezer, 5)
        await _report(hass, freezer, str(-2030 - index))
    assert _percents(action) == [12, 100]
    assert len(messages.calls) == 2
    assert messages.calls[1]["message"].startswith("Export limit set to 100 %.")


async def test_while_the_price_has_no_value_nothing_is_written(hass, freezer):
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE)
    assert len(await _first_limit(hass, freezer, action)) == 1

    await set_state(hass, PRICE, "unavailable", block=False)
    await _meter_reports_for(hass, freezer, 70)
    assert len(action.calls) == 1

    # The next tick starts a new run.
    await set_state(hass, PRICE, "-0.02", block=False)
    await _meter_reports_for(hass, freezer, 45)
    assert _percents(action) == [12, 12]


async def test_an_hour_without_a_price_ends_the_limit(hass, freezer):
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE)
    await set_state(hass, PRICE, "unavailable", block=False)

    await pass_time(hass, freezer, block=False, minutes=59)
    assert action.calls == []

    await pass_time(hass, freezer, minutes=2)
    assert _percents(action) == [100]


async def test_ticks_do_not_pile_up_behind_a_run(hass, freezer):
    """A run goes on for many ticks, and the run that ends the limit still gets its turn."""
    hass.states.async_set(PRICE, "-0.01")
    await _start(hass, freezer, price=PRICE)
    release = asyncio.Event()
    started = []

    async def slow(call):
        started.append(call.data["percent"])
        await release.wait()

    hass.services.async_register("sunspec2", "set_export_limit", slow)

    # The first round gets its reading and hangs in the write.
    await _wait(hass, freezer, 31)
    await _report(hass, freezer, "-2000")
    await _wait(hass, freezer, 240)
    await set_state(hass, PRICE, "0.03", block=False)
    release.set()
    await _wait(hass, freezer, 2)

    assert started == [12, 100]


# ---------------------------------------------------------------------------
# Writes the inverter does not take
# ---------------------------------------------------------------------------


async def test_a_write_the_inverter_did_not_take_is_tried_in_the_next_round(hass, freezer):
    """And after three of them in a row the next round is five minutes away."""
    action = await _start(hass, freezer)
    action.failures = 4
    await _wait(hass, freezer, 20)

    # Ten seconds into the run, and twice 30 seconds later.
    await _meter_reports_for(hass, freezer, 75)
    assert len(action.calls) == 3

    # The fourth, five minutes after the third.
    await _meter_reports_for(hass, freezer, 290)
    assert len(action.calls) == 3
    await _meter_reports_for(hass, freezer, 10)
    assert len(action.calls) == 4

    # That one failed as well. The fifth goes through.
    await _meter_reports_for(hass, freezer, 290)
    assert len(action.calls) == 4
    await _meter_reports_for(hass, freezer, 10)
    assert len(action.calls) == 5

    # And the rounds are 30 seconds apart again.
    await _the_inverter_follows(hass)
    await _meter_reports_for(hass, freezer, 30, values=("40", "41"))
    assert len(action.calls) == 6


async def test_a_limit_that_could_not_be_taken_back_is_taken_back_later(hass, freezer):
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE)
    action.failures = 3

    await set_state(hass, PRICE, "0.03", block=False)
    await _wait(hass, freezer, 62)
    assert len(action.calls) == 3
    await pass_time(hass, freezer, minutes=5, seconds=1)

    assert _percents(action) == [100, 100, 100, 100]


async def test_the_attempts_to_take_the_limit_back_end_when_the_price_is_low_again(hass, freezer):
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE)
    action.failures = 3

    await set_state(hass, PRICE, "0.03", block=False)
    await _wait(hass, freezer, 62)
    await set_state(hass, PRICE, "-0.02", block=False)
    # The wait for the fourth attempt ends with the low price, and the
    # next tick starts a run: no five minutes without a limit.
    for index in range(9):
        await _wait(hass, freezer, 5)
        await _report(hass, freezer, str(-2010 - index))

    assert _percents(action) == [100, 100, 100, 12]


async def test_after_two_hours_of_failures_the_limit_is_given_up(hass, freezer, caplog):
    hass.states.async_set(PRICE, "-0.01")
    notices = RecordedAction(hass, "persistent_notification", "create")
    action = await _start(hass, freezer, price=PRICE)
    action.failures = 100

    await set_state(hass, PRICE, "0.03", block=False)
    # Two attempts 30 seconds apart, then 24 that are five minutes apart.
    await pass_time(hass, freezer, block=False, seconds=31)
    await pass_time(hass, freezer, block=False, seconds=31)
    for _ in range(24):
        await pass_time(hass, freezer, block=False, minutes=5, seconds=1)
    await hass.async_block_till_done()

    assert len(action.calls) == 27
    assert ["has given up" in notice["message"] for notice in notices.calls] == [True]
    assert "did not take the export limit for two hours" in caplog.text


# ---------------------------------------------------------------------------
# With the integration
# ---------------------------------------------------------------------------


async def test_the_real_action_takes_what_the_blueprint_sends(
    hass, freezer, sunspec_write_client_mock
):
    """End to end: a round, the integration's own action, the write to the device."""
    freezer.move_to("2030-06-01 12:00:10+00:00")
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    assert await async_setup_component(hass, "homeassistant", {})
    hass.states.async_set(INVERTER, "3000", {"unit_of_measurement": "W"})
    hass.states.async_set(GRID, "-1990", {"unit_of_measurement": "W"})
    await start_automation(
        hass,
        BLUEPRINT,
        {
            "export_limit": TEST_NUMBER_EXPORT_LIMIT,
            "inverter_power": INVERTER,
            "grid_power": GRID,
            "rated_power": 8000,
        },
    )
    coordinator = entry.runtime_data
    written = []

    async def spy(model_id, points):
        written.append((model_id, points))

    with (
        patch.object(coordinator.api, "async_write_points", side_effect=spy),
        patch.object(coordinator, "async_request_refresh"),
    ):
        await _wait(hass, freezer, 31)
        await _report(hass, freezer, "-2000")

    # The fixture's export limit has a step of 0.01 %.
    assert written == [(123, [("WMaxLimPct", 12.5), ("WMaxLim_Ena", 1)])]
