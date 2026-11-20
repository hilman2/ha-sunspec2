"""The blueprint that sets the export limit to what the house uses.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

from unittest.mock import patch

import pytest
from homeassistant.setup import async_setup_component

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


async def _tick(hass, freezer):
    await pass_time(hass, freezer, seconds=30)


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
