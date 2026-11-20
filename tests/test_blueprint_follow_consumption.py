"""The blueprint that sets the export limit to what the house uses.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
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


def _report(hass, *entity_ids):
    """Let a sensor report the value it has, as a meter does with every message."""
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        hass.states.async_set(entity_id, state.state, state.attributes)


async def _tick(hass, freezer, report=(INVERTER, GRID), block=True):
    """Pass 30 seconds, to the next tick. The sensors report just before it.

    Args:
        report: The sensors with a new reading. A sensor left out has
            the reading from before the last write.
        block: False when the run is left waiting for a reading.
    """
    freezer.tick(timedelta(seconds=30))
    _report(hass, *report)
    async_fire_time_changed(hass, dt_util.utcnow())
    await (hass.async_block_till_done() if block else settle(hass))


async def test_the_limit_is_what_the_house_uses(hass, freezer):
    """3000 W delivered and 2000 W fed in: the house uses 1000 W, 12.5 % of 8000 W."""
    action = await _start(hass, freezer)

    await pass_time(hass, freezer, seconds=19)
    assert action.calls == []

    await pass_time(hass, freezer, seconds=1)
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

    await _tick(hass, freezer)

    assert [call["percent"] for call in action.calls] == [0]


async def test_a_reading_from_before_the_last_write_is_not_used(hass, freezer):
    """A meter that reports once a minute gets a new limit once a minute."""
    action = await _start(hass, freezer)
    await _tick(hass, freezer)
    assert len(action.calls) == 1

    # The inverter has a new reading, the meter has not.
    await _tick(hass, freezer, report=(INVERTER,), block=False)
    assert len(action.calls) == 1
    # The run waits ten seconds for the meter and then lets the tick go.
    await pass_time(hass, freezer, seconds=11)
    assert len(action.calls) == 1

    await _tick(hass, freezer)
    assert len(action.calls) == 2


async def test_a_reading_that_arrives_just_after_the_tick_is_waited_for(hass, freezer):
    action = await _start(hass, freezer)
    await _tick(hass, freezer)

    await _tick(hass, freezer, report=(INVERTER,), block=False)
    assert len(action.calls) == 1
    freezer.tick(timedelta(seconds=2))
    await set_state(hass, GRID, "-1500")

    # 3000 W delivered, 1500 W fed in: 1500 W for the house, 18.75 % of 8000 W.
    assert [call["percent"] for call in action.calls] == [12, 18]


async def test_a_sensor_that_hangs_counts_as_no_reading(hass, freezer):
    """After ten minutes without a report the fallback is written."""
    action = await _start(hass, freezer, fallback_percent=0, interval="0")
    await pass_time(hass, freezer, seconds=50)
    assert [call["percent"] for call in action.calls] == [12]

    # Five minutes on, the meter has said nothing since. That is an
    # old reading, and nothing is written.
    freezer.tick(timedelta(minutes=5))
    _report(hass, INVERTER)
    async_fire_time_changed(hass, dt_util.utcnow())
    await settle(hass)
    await pass_time(hass, freezer, seconds=11)
    assert len(action.calls) == 1

    # Another six minutes on it counts as hanging. The run still waits
    # its ten seconds for a reading first.
    freezer.tick(timedelta(minutes=5, seconds=49))
    _report(hass, INVERTER)
    async_fire_time_changed(hass, dt_util.utcnow())
    await settle(hass)
    await pass_time(hass, freezer, seconds=11)
    assert [call["percent"] for call in action.calls] == [12, 0]


async def test_ticks_do_not_pile_up_behind_a_write_that_takes_long(hass, freezer):
    """The run that ends the limit still finds a place in the queue."""
    hass.states.async_set(PRICE, "-0.01")
    action = await _start(hass, freezer, price=PRICE)
    release = asyncio.Event()
    started = []

    async def slow(call):
        started.append(call.data["percent"])
        await release.wait()

    hass.services.async_register("sunspec2", "set_export_limit", slow)

    for _ in range(8):
        await _tick(hass, freezer, block=False)
    await set_state(hass, PRICE, "0.03", block=False)
    release.set()
    await hass.async_block_till_done()

    assert action.calls == []
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

    # No fourth 100 %. The tick that came due in those five minutes
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
