"""The blueprint that sets the export limit to what the house uses.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

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
    default period of 30 seconds the next tick is 20 seconds away.
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


def _new_reading(hass, entity_id, value=None):
    """Give a sensor a new reading, as a meter does when it reports.

    Args:
        value: The new state. None keeps the state and changes an
            attribute, which is a new reading with the same rounded value.
    """
    state = hass.states.get(entity_id)
    attributes = {**state.attributes, "reading": state.attributes.get("reading", 0) + 1}
    hass.states.async_set(entity_id, state.state if value is None else value, attributes)


def _to_the_next_tick(hass, freezer):
    """Move the clock to the next full or half minute and fire what is due.

    The clock only ever goes forward: the event loop runs on it as well.
    """
    second = dt_util.utcnow().second + dt_util.utcnow().microsecond / 1e6
    freezer.tick(timedelta(seconds=30 - second % 30))
    async_fire_time_changed(hass, dt_util.utcnow())


async def _tick(hass, freezer, grid=None):
    """Go to the next tick and let the meter report two seconds into it.

    The run of a tick waits for a new value of the meter.
    """
    _to_the_next_tick(hass, freezer)
    await settle(hass)
    freezer.tick(timedelta(seconds=2))
    _new_reading(hass, GRID, grid)
    await hass.async_block_till_done()


async def _silent_tick(hass, freezer):
    """A tick during which the meter reports nothing: 25 seconds of waiting, then nothing."""
    _to_the_next_tick(hass, freezer)
    await settle(hass)
    await pass_time(hass, freezer, seconds=26)


async def test_the_limit_is_what_the_house_uses(hass, freezer):
    """3000 W delivered and 2000 W fed in: the house uses 1000 W, 12.5 % of 8000 W."""
    action = await _start(hass, freezer)

    await pass_time(hass, freezer, seconds=19)
    assert action.calls == []

    # The tick, and the run waits for the meter.
    await pass_time(hass, freezer, block=False, seconds=1)
    assert action.calls == []

    _new_reading(hass, GRID)
    await hass.async_block_till_done()
    # Rounded down to the whole percent the entity takes.
    assert action.calls == [{"config_entry_id": ENTRY_ID, "percent": 12, "enable": True}]


async def test_the_limit_is_written_on_every_tick(hass, freezer):
    """Also an unchanged limit: the write restarts the inverter's revert timer."""
    action = await _start(hass, freezer)

    await _tick(hass, freezer)
    await _tick(hass, freezer)
    await _tick(hass, freezer)

    assert [call["percent"] for call in action.calls] == [12, 12, 12]


async def test_power_taken_from_the_grid_raises_the_limit(hass, freezer):
    """Limited to 960 W while the house takes another 2040 W from the grid."""
    action = await _start(hass, freezer, inverter="960", grid="2040")

    await _tick(hass, freezer)

    assert [call["percent"] for call in action.calls] == [37]


async def test_a_meter_that_counts_feed_in_as_positive(hass, freezer):
    action = await _start(hass, freezer, grid="2000", grid_direction="export")

    await _tick(hass, freezer)

    assert [call["percent"] for call in action.calls] == [12]


async def test_readings_in_kilowatts(hass, freezer):
    action = await _start(hass, freezer)
    await set_state(hass, INVERTER, "3.0", unit_of_measurement="kW")
    await set_state(hass, GRID, "-2.0", unit_of_measurement="kW")

    await _tick(hass, freezer)

    assert [call["percent"] for call in action.calls] == [12]


async def test_the_allowed_feed_in_is_added(hass, freezer):
    """1000 W for the house and 600 W for the grid are 20 % of 8000 W."""
    action = await _start(hass, freezer, max_export=600)

    await _tick(hass, freezer)

    assert [call["percent"] for call in action.calls] == [20]


async def test_the_limit_uses_the_step_of_the_entity(hass, freezer):
    action = await _start(hass, freezer, step=0.1)

    await _tick(hass, freezer)

    assert [call["percent"] for call in action.calls] == [12.5]


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
    action = await _start(hass, freezer, inverter=inverter, grid=grid, min_percent=5)

    await _tick(hass, freezer)

    assert [call["percent"] for call in action.calls] == [expected]


@pytest.mark.parametrize("missing", [INVERTER, GRID])
async def test_without_a_reading_the_fallback_is_written(hass, freezer, missing):
    action = await _start(hass, freezer, fallback_percent=0)
    await set_state(hass, missing, "unavailable")

    # Without a grid power there is nothing to wait for.
    if missing == GRID:
        await pass_time(hass, freezer, seconds=30)
    else:
        await _tick(hass, freezer)

    assert [call["percent"] for call in action.calls] == [0]


async def test_without_a_new_value_of_the_meter_a_tick_writes_nothing(hass, freezer):
    """A meter that reports once a minute gets a new limit once a minute."""
    action = await _start(hass, freezer)
    await _tick(hass, freezer)
    assert len(action.calls) == 1

    await _silent_tick(hass, freezer)
    assert len(action.calls) == 1

    await _tick(hass, freezer)
    assert len(action.calls) == 2


async def test_the_new_value_and_the_inverter_power_of_that_moment_make_the_limit(hass, freezer):
    action = await _start(hass, freezer)

    await _tick(hass, freezer, grid="-1500")

    # 3000 W delivered, 1500 W fed in: 1500 W for the house, 18.75 % of 8000 W.
    assert [call["percent"] for call in action.calls] == [18]


async def test_a_report_without_a_new_value_does_not_count(hass, freezer):
    """update_entity moves the time of the last report of a sensor that has nothing new."""
    action = await _start(hass, freezer)
    await _tick(hass, freezer)

    _to_the_next_tick(hass, freezer)
    await settle(hass)
    freezer.tick(timedelta(seconds=2))
    state = hass.states.get(GRID)
    # The same state with the same attributes: only last_reported moves.
    hass.states.async_set(GRID, state.state, state.attributes)
    await settle(hass)
    assert hass.states.get(GRID).last_reported > hass.states.get(GRID).last_updated
    await pass_time(hass, freezer, seconds=26)

    assert len(action.calls) == 1


class _Meter(SensorEntity):
    """A grid meter as an entity, so that ``homeassistant.update_entity`` finds it."""

    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER

    def __init__(self, polled: bool) -> None:
        self.entity_id = GRID
        self._attr_should_poll = polled
        self._attr_native_value = -2000.0
        #: What the next poll finds, for a polled meter.
        self.at_the_device = -2000.0

    async def async_update(self) -> None:
        self._attr_native_value = self.at_the_device


async def _start_with_meter(hass, freezer, meter):
    freezer.move_to("2030-06-01 12:00:10+00:00")
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "sensor", {})
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
    return action


async def test_a_meter_that_pushes_is_not_made_new_by_asking_it(hass, freezer):
    """The automation asks the meter for an update, and a meter that pushes has none."""
    action = await _start_with_meter(hass, freezer, _Meter(polled=False))
    reported = hass.states.get(GRID).last_reported

    _to_the_next_tick(hass, freezer)
    await settle(hass)
    # Asked, and the time of the last report moved. Still no new value.
    assert hass.states.get(GRID).last_reported > reported
    await pass_time(hass, freezer, seconds=26)

    assert action.calls == []


async def test_a_meter_that_is_polled_answers_when_it_is_asked(hass, freezer):
    """Its new value is there before the wait begins, so the limit is written at once."""
    meter = _Meter(polled=True)
    action = await _start_with_meter(hass, freezer, meter)
    meter.at_the_device = -1500.0

    _to_the_next_tick(hass, freezer)
    await hass.async_block_till_done()

    # 3000 W delivered, 1500 W fed in: 18.75 % of 8000 W.
    assert [call["percent"] for call in action.calls] == [18]


async def test_a_meter_that_hangs_counts_as_no_reading(hass, freezer):
    """After ten minutes with the same value the fallback is written, without a wait.

    With a tick every minute a run waits 55 seconds for the meter, so
    the steps below are a tick, the end of its wait, the next tick.
    """
    action = await _start(hass, freezer, fallback_percent=0, interval="0")
    await pass_time(hass, freezer, block=False, seconds=50)
    freezer.tick(timedelta(seconds=2))
    _new_reading(hass, GRID)
    await hass.async_block_till_done()
    # Written at 12:01:02, with the last new value of the meter.
    assert [call["percent"] for call in action.calls] == [12]

    # The ticks of 12:09, 12:10 and 12:11 wait for the meter in vain.
    await pass_time(hass, freezer, block=False, minutes=7, seconds=58)
    await pass_time(hass, freezer, seconds=56)
    for _ in range(2):
        await pass_time(hass, freezer, block=False, seconds=4)
        await pass_time(hass, freezer, seconds=56)
    assert len(action.calls) == 1

    # 12:12: the value is older than ten minutes.
    await pass_time(hass, freezer, seconds=4)
    assert [call["percent"] for call in action.calls] == [12, 0]


async def test_ticks_do_not_pile_up_behind_a_write_that_takes_long(hass, freezer):
    """The run that ends the limit still finds a place in the queue."""
    hass.states.async_set(PRICE, "-0.01")
    await _start(hass, freezer, price=PRICE)
    release = asyncio.Event()
    started = []

    async def slow(call):
        started.append(call.data["percent"])
        await release.wait()

    hass.services.async_register("sunspec2", "set_export_limit", slow)

    # The first tick gets its reading and hangs in the write.
    _to_the_next_tick(hass, freezer)
    await settle(hass)
    _new_reading(hass, GRID)
    await settle(hass)
    for _ in range(8):
        await pass_time(hass, freezer, block=False, seconds=30)
    await set_state(hass, PRICE, "0.03", block=False)
    release.set()
    await hass.async_block_till_done()

    assert started == [12, 100]


async def test_while_the_price_has_no_value_nothing_is_written(hass, freezer):
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE)
    await _tick(hass, freezer)
    assert len(action.calls) == 1

    await set_state(hass, PRICE, "unavailable")
    await _tick(hass, freezer)
    await _tick(hass, freezer)
    assert len(action.calls) == 1

    await set_state(hass, PRICE, "-0.02")
    await _tick(hass, freezer)
    assert [call["percent"] for call in action.calls] == [12, 12]


async def test_a_limit_that_could_not_be_taken_back_is_taken_back_later(hass, freezer):
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE)
    action.failures = 3

    await set_state(hass, PRICE, "0.03", block=False)
    await pass_time(hass, freezer, block=False, seconds=31)
    await pass_time(hass, freezer, block=False, seconds=31)
    assert len(action.calls) == 3
    await pass_time(hass, freezer, minutes=5, seconds=1)

    assert [call["percent"] for call in action.calls] == [100, 100, 100, 100]


async def test_the_attempts_to_take_the_limit_back_end_when_the_price_is_low_again(hass, freezer):
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE)
    action.failures = 3

    await set_state(hass, PRICE, "0.03", block=False)
    await pass_time(hass, freezer, block=False, seconds=31)
    await pass_time(hass, freezer, block=False, seconds=31)
    await set_state(hass, PRICE, "-0.02", block=False)
    await pass_time(hass, freezer, minutes=5, seconds=1)

    # No fourth 100 %. The ticks that came due in those five minutes
    # found the automation busy, the next one sets the limit.
    assert [call["percent"] for call in action.calls] == [100, 100, 100]
    await _tick(hass, freezer)
    assert [call["percent"] for call in action.calls] == [100, 100, 100, 12]


async def test_an_inverter_that_is_off_gets_no_write(hass, freezer):
    action = await _start(hass, freezer)
    await set_state(hass, LIMIT, "unavailable")

    await _tick(hass, freezer)

    assert action.calls == []


async def test_with_a_price_the_limit_only_follows_while_the_price_is_low(hass, freezer):
    hass.states.async_set(PRICE, "0.05", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("notify.phone", "unknown")
    messages = RecordedAction(hass, "notify", "send_message")
    action = await _start(hass, freezer, price=PRICE, notify=["notify.phone"])

    await _tick(hass, freezer)
    assert action.calls == []

    await set_state(hass, PRICE, "-0.01")
    assert action.calls == []
    assert [call["message"] for call in messages.calls] == [
        "The price is -0.01 EUR/kWh. The feed-in is limited to 0 W."
    ]
    await _tick(hass, freezer)
    assert [call["percent"] for call in action.calls] == [12]

    # The price recovers: full power once, and the ticks stop writing.
    await set_state(hass, PRICE, "0.02")
    await _tick(hass, freezer)
    await _tick(hass, freezer)
    assert [call["percent"] for call in action.calls] == [12, 100]
    assert len(messages.calls) == 2
    assert messages.calls[1]["message"].startswith("Export limit set to 100 %.")


async def test_an_hour_without_a_price_ends_the_limit(hass, freezer):
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE, interval="0")
    await set_state(hass, PRICE, "unavailable")

    await pass_time(hass, freezer, minutes=59)
    assert action.calls == []

    await pass_time(hass, freezer, minutes=2)
    assert [call["percent"] for call in action.calls] == [100]


async def test_the_real_action_takes_what_the_blueprint_sends(
    hass, freezer, sunspec_write_client_mock
):
    """End to end: a tick, the integration's own action, the write to the device."""
    freezer.move_to("2030-06-01 12:00:10+00:00")
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    assert await async_setup_component(hass, "homeassistant", {})
    hass.states.async_set(INVERTER, "3000", {"unit_of_measurement": "W"})
    hass.states.async_set(GRID, "-2000", {"unit_of_measurement": "W"})
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
        await _tick(hass, freezer)

    # The fixture's export limit has a step of 0.01 %.
    assert written == [(123, [("WMaxLimPct", 12.5), ("WMaxLim_Ena", 1)])]
