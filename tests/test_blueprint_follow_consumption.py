"""The blueprint that sets the export limit to what the house uses.

A run of this automation is a row of rounds of 30 seconds. In each
round it waits for the meter, reads the inverter and writes the limit.
A run is in a wait or a delay almost all the time, so these tests never
wait for the automation to finish: they move the clock in steps and
look at what has been written by then.

A reading of the meter counts from five seconds after the limit last
changed, and a run begins as if it just had. So the first reading of a
test comes six seconds into the first round.

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


async def _start(hass, freezer, inverter="3000", grid="-2000", step=1.0, **inputs):
    """An 8 kW inverter delivering 3 kW, of which 2 kW go into the grid.

    The clock stands ten seconds after a full minute, so with the
    default period of 30 seconds the first tick is 20 seconds away.
    """
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
    """Pass time while a run is in a wait or a delay, second by second.

    A step of several seconds would end a wait or a delay late: a timer
    only fires when the clock is moved, and what follows it would then
    happen at the end of the step.
    """
    for _ in range(seconds):
        await pass_time(hass, freezer, block=False, seconds=1)


async def _report(hass, value=None):
    """Let the meter report: a new value, or without one the value it had.

    The second is what a meter with a coarse resolution does while the
    power is steady. Home Assistant then only notes that it reported.
    """
    state = hass.states.get(GRID)
    hass.states.async_set(GRID, state.state if value is None else value, state.attributes)
    await settle(hass)


def _percents(action):
    return [call["percent"] for call in action.calls]


async def test_the_limit_is_what_the_house_uses(hass, freezer):
    """3000 W delivered and 2000 W fed in: the house uses 1000 W, 12.5 % of 8000 W."""
    action = await _start(hass, freezer, grid="-1990")

    await _wait(hass, freezer, 19)
    assert action.calls == []

    # The tick starts a run, and its first round waits for the meter.
    await _wait(hass, freezer, 1)
    assert action.calls == []

    await _wait(hass, freezer, 6)
    await _report(hass, "-2000")
    # Rounded down to the whole percent the entity takes.
    assert action.calls == [{"config_entry_id": ENTRY_ID, "percent": 12, "enable": True}]


async def test_the_limit_is_written_once_per_period(hass, freezer):
    """A meter that reports every five seconds does not get a write every five seconds."""
    action = await _start(hass, freezer)
    await _wait(hass, freezer, 20)

    for index in range(17):
        await _wait(hass, freezer, 5)
        await _report(hass, str(-2001 - index))

    # 85 seconds, the third round is on its way.
    assert _percents(action) == [12, 12, 12]


async def _first_limit(hass, freezer, action, grid):
    """The limit of the first round, written when the meter reports ``grid``."""
    await _wait(hass, freezer, 26)
    await _report(hass, grid)
    return _percents(action)


async def test_power_taken_from_the_grid_raises_the_limit(hass, freezer):
    """Limited to 960 W while the house takes another 2040 W from the grid."""
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
    action = await _start(hass, freezer, grid="-1990", max_export=600)

    assert await _first_limit(hass, freezer, action, "-2000") == [20]


async def test_the_limit_uses_the_step_of_the_entity(hass, freezer):
    action = await _start(hass, freezer, grid="-1990", step=0.1)

    assert await _first_limit(hass, freezer, action, "-2000") == [12.5]


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


async def test_a_reading_right_after_a_changed_limit_does_not_count(hass, freezer):
    """The inverter needs a moment to follow, and the meter still shows the power from before."""
    action = await _start(hass, freezer)
    await _wait(hass, freezer, 26)
    await _report(hass, "-2010")
    assert _percents(action) == [12]

    # The second round: a reading two seconds after the write.
    await _wait(hass, freezer, 2)
    await _report(hass, "-2500")
    await _wait(hass, freezer, 20)
    assert _percents(action) == [12]

    # One six seconds after it counts. 3000 W delivered, 1500 W fed in.
    await _wait(hass, freezer, 4)
    await _report(hass, "-1500")
    await _wait(hass, freezer, 12)
    assert _percents(action) == [12, 18]


async def test_a_reading_right_after_the_same_limit_counts(hass, freezer):
    """A write of the limit already in force changes nothing the meter could lag behind."""
    action = await _start(hass, freezer)
    await _wait(hass, freezer, 26)
    await _report(hass, "-2010")
    # The second round begins 24 seconds later. The meter repeats its
    # value in it, and the round ends with the same limit.
    await _wait(hass, freezer, 34)
    await _report(hass)
    await _wait(hass, freezer, 21)
    assert _percents(action) == [12, 12]

    # The third: a report one second after that write.
    await _report(hass)
    await _wait(hass, freezer, 30)
    assert _percents(action) == [12, 12, 12]


async def test_the_inverter_is_not_read_twice_within_twelve_seconds(hass, freezer):
    """Home Assistant would hand out the reading from before, which can be from before a write."""
    action = await _start(hass, freezer)
    asked = []
    hass.services.async_register(
        "homeassistant", "update_entity", lambda call: asked.append(len(action.calls))
    )
    await _wait(hass, freezer, 26)
    await _report(hass, "-2010")
    # The second round ends without a report, with a read at its end.
    await _wait(hass, freezer, 54)
    assert len(asked) == 2

    # The meter reports two seconds into the next round.
    await _wait(hass, freezer, 2)
    await _report(hass, "-2020")
    await _wait(hass, freezer, 9)
    assert len(asked) == 2
    await _wait(hass, freezer, 2)
    assert len(asked) == 3
    assert _percents(action) == [12, 12]


async def test_a_shorter_period_makes_shorter_rounds(hass, freezer):
    action = await _start(hass, freezer, interval="/15")
    # The first tick is five seconds away.
    await _wait(hass, freezer, 5)

    for index in range(8):
        await _wait(hass, freezer, 8)
        await _report(hass, str(-2001 - index))
        await _wait(hass, freezer, 7)

    # Two minutes, eight rounds.
    assert len(action.calls) == 8


async def test_after_a_write_the_inverter_refused_the_next_one_is_five_minutes_away(hass, freezer):
    """Each refused write is an error in the log, and the next round would fail the same way."""
    action = await _start(hass, freezer)
    action.failures = 1
    await _wait(hass, freezer, 26)
    await _report(hass, "-2010")
    assert len(action.calls) == 1

    for index in range(9):
        await _wait(hass, freezer, 30)
        await _report(hass, str(-2020 - index))
    assert len(action.calls) == 1

    await _wait(hass, freezer, 30)
    await _report(hass, "-2040")
    await _wait(hass, freezer, 30)
    assert len(action.calls) == 2


async def test_a_meter_that_repeats_its_value_still_gets_a_limit(hass, freezer):
    """Its report ends no wait, so the limit comes at the end of the round."""
    action = await _start(hass, freezer)
    await _wait(hass, freezer, 20)

    await _wait(hass, freezer, 5)
    await _report(hass)
    await _wait(hass, freezer, 24)
    assert action.calls == []

    await _wait(hass, freezer, 2)
    assert _percents(action) == [12]


async def test_a_round_without_a_report_of_the_meter_writes_nothing(hass, freezer):
    """A meter that reports once a minute gets a new limit once a minute."""
    action = await _start(hass, freezer)
    await _wait(hass, freezer, 26)
    await _report(hass, "-2010")
    assert len(action.calls) == 1

    # The rest of the first round and all of the second.
    await _wait(hass, freezer, 28)
    await _wait(hass, freezer, 31)
    assert len(action.calls) == 1

    await _wait(hass, freezer, 5)
    await _report(hass, "-2020")
    # The inverter was read at the end of the round before, and is not
    # read again within twelve seconds.
    await _wait(hass, freezer, 8)
    assert len(action.calls) == 2


@pytest.mark.parametrize("phase", [1, 12, 27, 29])
async def test_no_phase_of_the_meter_against_the_tick_goes_without_a_limit(hass, freezer, phase):
    """A meter that reports every 30 seconds, ``phase`` seconds after each tick."""
    action = await _start(hass, freezer)
    await _wait(hass, freezer, 20)

    for index in range(6):
        await _wait(hass, freezer, phase)
        await _report(hass, str(-2001 - index))
        await _wait(hass, freezer, 30 - phase)

    # A limit in every round. The first round of a run is the one
    # exception: a reading in its first five seconds does not count.
    assert len(action.calls) == (5 if phase < 5 else 6)


async def test_the_inverter_is_read_when_the_meter_has_reported(hass, freezer):
    """The two readings are of the same moment: the inverter power is asked for last."""
    action = await _start(hass, freezer)
    asked = []
    hass.services.async_register(
        "homeassistant",
        "update_entity",
        lambda call: asked.append((call.data["entity_id"], len(action.calls))),
    )

    await _wait(hass, freezer, 26)
    assert asked == []
    await _report(hass, "-2010")

    # Only the inverter, and before the limit was written.
    assert asked == [([INVERTER], 0)]


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
    await _wait(hass, freezer, 20)
    await _wait(hass, freezer, 31)
    assert action.calls == []

    # A round in which it reports the value it had.
    await _wait(hass, freezer, 10)
    meter.async_write_ha_state()
    await _wait(hass, freezer, 21)
    assert _percents(action) == [12]
    assert meter.asked == 0


async def test_a_meter_that_hangs_counts_as_no_reading(hass, freezer):
    """After ten minutes without a report the fallback is written."""
    action = await _start(hass, freezer, fallback_percent=0)
    await _wait(hass, freezer, 26)
    await _report(hass, "-2010")
    assert _percents(action) == [12]

    # Nine and a half minutes of rounds without a report.
    for _ in range(19):
        await _wait(hass, freezer, 30)
    assert len(action.calls) == 1

    await _wait(hass, freezer, 75)
    assert _percents(action)[:2] == [12, 0]


@pytest.mark.parametrize("missing", [INVERTER, GRID])
async def test_without_a_reading_the_fallback_is_written(hass, freezer, missing):
    action = await _start(hass, freezer, fallback_percent=0)
    await set_state(hass, missing, "unavailable")

    await _wait(hass, freezer, 26)
    if missing == INVERTER:
        # The round still waits for the meter first.
        await _report(hass, "-2010")

    assert _percents(action) == [0]


async def test_an_inverter_that_is_off_gets_no_write(hass, freezer):
    action = await _start(hass, freezer)
    await set_state(hass, LIMIT, "unavailable")

    await _wait(hass, freezer, 26)
    await _report(hass, "-2010")

    assert action.calls == []


async def test_a_run_ends_when_the_inverter_goes_off(hass, freezer):
    action = await _start(hass, freezer)
    await _wait(hass, freezer, 26)
    await _report(hass, "-2010")
    assert len(action.calls) == 1

    await set_state(hass, LIMIT, "unavailable", block=False)
    for index in range(4):
        await _wait(hass, freezer, 15)
        await _report(hass, str(-2020 - index))

    assert len(action.calls) == 1


async def test_with_a_price_the_limit_only_follows_while_the_price_is_low(hass, freezer):
    hass.states.async_set(PRICE, "0.05", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("notify.phone", "unknown")
    messages = RecordedAction(hass, "notify", "send_message")
    action = await _start(hass, freezer, price=PRICE, notify=["notify.phone"])

    await _wait(hass, freezer, 26)
    await _report(hass, "-2010")
    assert action.calls == []

    await set_state(hass, PRICE, "-0.01", block=False)
    assert action.calls == []
    assert [call["message"] for call in messages.calls] == [
        "The price is -0.01 EUR/kWh. The feed-in is limited to 0 W."
    ]
    # The next tick starts a run.
    await _wait(hass, freezer, 30)
    await _report(hass, "-2020")
    assert _percents(action) == [12]

    # The price recovers. The run ends with its round, then the limit
    # is taken back once.
    await set_state(hass, PRICE, "0.02", block=False)
    await _wait(hass, freezer, 31)
    await _report(hass, "-2030")
    await _wait(hass, freezer, 31)
    await _report(hass, "-2040")
    assert _percents(action) == [12, 100]
    assert len(messages.calls) == 2
    assert messages.calls[1]["message"].startswith("Export limit set to 100 %.")


async def test_while_the_price_has_no_value_nothing_is_written(hass, freezer):
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE)
    await _wait(hass, freezer, 26)
    await _report(hass, "-2010")
    assert len(action.calls) == 1

    await set_state(hass, PRICE, "unavailable", block=False)
    for index in range(4):
        await _wait(hass, freezer, 20)
        await _report(hass, str(-2020 - index))
    assert len(action.calls) == 1

    await set_state(hass, PRICE, "-0.02", block=False)
    await _wait(hass, freezer, 31)
    await _report(hass, "-2030")
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
    await _wait(hass, freezer, 26)
    await _report(hass, "-2010")
    for _ in range(8):
        await _wait(hass, freezer, 30)
    await set_state(hass, PRICE, "0.03", block=False)
    release.set()
    await _wait(hass, freezer, 31)

    assert started == [12, 100]


async def test_a_limit_that_could_not_be_taken_back_is_taken_back_later(hass, freezer):
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE)
    action.failures = 3

    await set_state(hass, PRICE, "0.03", block=False)
    await _wait(hass, freezer, 31)
    await _wait(hass, freezer, 31)
    assert len(action.calls) == 3
    await pass_time(hass, freezer, minutes=5, seconds=1)

    assert _percents(action) == [100, 100, 100, 100]


async def test_the_attempts_to_take_the_limit_back_end_when_the_price_is_low_again(hass, freezer):
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE)
    action.failures = 3

    await set_state(hass, PRICE, "0.03", block=False)
    await _wait(hass, freezer, 31)
    await _wait(hass, freezer, 31)
    await set_state(hass, PRICE, "-0.02", block=False)
    # The wait for the fourth attempt ends with the low price, and the
    # next tick starts a run: no five minutes without a limit.
    await _wait(hass, freezer, 36)
    await _report(hass, "-2010")
    assert _percents(action) == [100, 100, 100, 12]


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
        await _wait(hass, freezer, 26)
        await _report(hass, "-2000")

    # The fixture's export limit has a step of 0.01 %.
    assert written == [(123, [("WMaxLimPct", 12.5), ("WMaxLim_Ena", 1)])]


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
