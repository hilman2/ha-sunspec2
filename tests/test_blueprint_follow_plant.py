"""The blueprint that follows the house consumption, in a closed loop with a house.

``test_blueprint_follow_consumption.py`` checks one round at a time,
with readings a test sets by hand. That cannot show what the loop does
to itself: a limit changes what the meter reads next. Here a small
model of a house answers every write, second by second, and the
inverter power comes through a ``DataUpdateCoordinator`` as it does in
the integration, with Home Assistant's ten seconds between two reads.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

import logging
from datetime import timedelta

import pytest
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .blueprint_helpers import settle
from .blueprint_helpers import start_automation
from .blueprint_helpers import sunspec_entity

LIMIT = "number.inverter_export_limit"
INVERTER = "sensor.inverter_power"
GRID = "sensor.grid_power"
RATED_W = 8000

pytestmark = pytest.mark.usefixtures("repository_blueprints")


class _House:
    """A house with solar power: what it uses, what the sun gives, what the inverter may deliver.

    The inverter follows a new limit two seconds after the write.
    """

    def __init__(self) -> None:
        self.load_w = 1000.0
        self.sun_w = 6000.0
        self.limit_percent = 100.0
        self._on_its_way: tuple[float, float] | None = None

    def write(self, percent: float, at: float) -> None:
        self._on_its_way = (at + 2.0, percent)

    def tick(self, at: float) -> None:
        if self._on_its_way is not None and at >= self._on_its_way[0]:
            self.limit_percent = self._on_its_way[1]
            self._on_its_way = None

    @property
    def inverter_w(self) -> float:
        return min(self.sun_w, self.limit_percent / 100 * RATED_W)

    @property
    def import_w(self) -> float:
        """Positive while the house takes power from the grid, negative while it feeds in."""
        return self.load_w - self.inverter_w


class _InverterPower(CoordinatorEntity[DataUpdateCoordinator[float]], SensorEntity):
    """The inverter power the way the integration offers it: read by a coordinator."""

    _attr_native_unit_of_measurement = "W"

    def __init__(self, coordinator: DataUpdateCoordinator[float]) -> None:
        super().__init__(coordinator)
        self.entity_id = INVERTER

    @property
    def native_value(self) -> float:
        return float(self.coordinator.data)


class _Meter(SensorEntity):
    """A meter that pushes its readings, in steps of ``resolution_w``."""

    _attr_native_unit_of_measurement = "W"
    _attr_should_poll = False

    def __init__(self, resolution_w: float) -> None:
        self.entity_id = GRID
        self._resolution_w = resolution_w
        self._attr_native_value = 0.0

    def report(self, import_w: float) -> None:
        self._attr_native_value = round(import_w / self._resolution_w) * self._resolution_w
        self.async_write_ha_state()


async def _run(hass, freezer, interval, meter_seconds, resolution_w, phase, seconds, events=None):
    """Run the automation against the house for ``seconds`` and return what happened.

    Args:
        interval: The blueprint's input, ``"/15"``, ``"/30"`` or ``"0"`` for a minute.
        meter_seconds: How often the meter reports.
        resolution_w: The step of the meter's readings.
        phase: The second within ``meter_seconds`` at which the meter reports.
        events: Second of the run to a change of the house, as attribute name and value.

    Returns:
        tuple[list[tuple[int, float]], float]: A two-way tuple (writes,
        fed_in_wh). ``writes`` holds the second of the run and the
        percentage of every write. ``fed_in_wh`` is the energy fed into
        the grid from the third minute on.
    """
    freezer.move_to("2030-06-01 12:00:00+00:00")
    house = _House()
    start = dt_util.utcnow().timestamp()

    async def read() -> float:
        return house.inverter_w

    coordinator = DataUpdateCoordinator(
        hass, logging.getLogger(__name__), config_entry=None, name="house", update_method=read
    )
    await coordinator.async_refresh()
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "sensor", {})
    meter = _Meter(resolution_w)
    await hass.data["sensor"].async_add_entities([_InverterPower(coordinator), meter])
    meter.report(house.import_w)
    sunspec_entity(hass, LIMIT, "100", step=1.0)

    writes: list[tuple[int, float]] = []

    async def set_export_limit(call) -> None:
        now = dt_util.utcnow().timestamp()
        writes.append((round(now - start), call.data["percent"]))
        house.write(call.data["percent"], now)
        # What the integration's action does after every write.
        await coordinator.async_request_refresh()

    hass.services.async_register("sunspec2", "set_export_limit", set_export_limit)
    await start_automation(
        hass,
        "follow_house_consumption",
        {
            "export_limit": LIMIT,
            "inverter_power": INVERTER,
            "grid_power": GRID,
            "rated_power": RATED_W,
            "interval": interval,
        },
    )

    fed_in_ws = 0.0
    for second in range(1, seconds + 1):
        freezer.tick(timedelta(seconds=1))
        for name, value in (events or {}).get(second, {}).items():
            setattr(house, name, value)
        house.tick(dt_util.utcnow().timestamp())
        if second % meter_seconds == phase:
            meter.report(house.import_w)
        async_fire_time_changed(hass, dt_util.utcnow())
        await settle(hass)
        if second > 120:
            fed_in_ws += max(0.0, -house.import_w)
    # Its timer for the ten seconds between two reads would outlive the test.
    await coordinator.async_shutdown()
    return writes, fed_in_ws / 3600


@pytest.mark.parametrize(
    ("interval", "meter_seconds", "resolution_w", "phase"),
    [
        # A fast meter with a fine and with a coarse resolution.
        ("/30", 1, 1.0, 0),
        ("/30", 1, 10.0, 0),
        ("/30", 5, 10.0, 3),
        # A meter that reports with the period, at every point of it.
        ("/30", 30, 1.0, 0),
        ("/30", 30, 1.0, 1),
        ("/30", 30, 1.0, 2),
        ("/30", 30, 10.0, 4),
        ("/30", 30, 1.0, 6),
        ("/30", 30, 1.0, 12),
        ("/30", 30, 1.0, 27),
        ("/30", 30, 1.0, 29),
        # A short period with a meter that reports every ten seconds.
        ("/15", 10, 1.0, 4),
        ("/20", 10, 10.0, 4),
        # A meter that reports once a minute.
        ("0", 60, 1.0, 40),
    ],
)
async def test_a_steady_house_gets_a_steady_limit(
    hass, freezer, interval, meter_seconds, resolution_w, phase
):
    """1000 W of consumption under 6000 W of sun: 12 % of 8000 W, and it stays there."""
    writes, fed_in_wh = await _run(
        hass, freezer, interval, meter_seconds, resolution_w, phase, seconds=600
    )

    # The first limit is there after two rounds at the latest.
    period = 60 if interval == "0" else int(interval[1:])
    assert writes and writes[0][0] <= 2 * period + 2, writes[:3]
    assert {percent for _, percent in writes} == {12}, writes
    # 960 W delivered for 1000 W of consumption: nothing goes into the grid.
    assert fed_in_wh == 0
    # A limit in every round, for as long as the meter reports once per round.
    rounds = (600 - writes[0][0]) // max(period, meter_seconds)
    assert len(writes) >= rounds, (len(writes), rounds)


@pytest.mark.parametrize(
    ("interval", "meter_seconds", "resolution_w", "phase"),
    [
        ("/30", 1, 10.0, 0),
        ("/30", 5, 1.0, 3),
        ("/15", 10, 1.0, 4),
        ("/30", 30, 1.0, 27),
    ],
)
async def test_the_limit_follows_a_change_of_the_consumption_and_settles(
    hass, freezer, interval, meter_seconds, resolution_w, phase
):
    """The oven goes on after five minutes and off after ten: 1000 W, 3000 W, 1000 W."""
    writes, _ = await _run(
        hass,
        freezer,
        interval,
        meter_seconds,
        resolution_w,
        phase,
        seconds=900,
        events={300: {"load_w": 3000.0}, 600: {"load_w": 1000.0}},
    )
    period = int(interval[1:])
    # Two rounds for the automation to see a change and one more to
    # have written the limit that fits it.
    settled = 3 * max(period, meter_seconds)

    def limits(first, last):
        return {percent for second, percent in writes if first <= second < last}

    assert limits(settled, 300) == {12}, writes
    # 3000 W of 8000 W are 37.5 %.
    assert limits(300 + settled, 600) == {37}, writes
    assert limits(600 + settled, 900) == {12}, writes
    # On the way from one to the other the limit never leaves the
    # range between the two. A reading from the wrong side of a write
    # would send it far outside.
    assert limits(settled, 900) <= set(range(12, 38)), writes


@pytest.mark.parametrize(("meter_seconds", "resolution_w"), [(1, 1.0), (5, 10.0), (30, 1.0)])
async def test_a_cloud_does_not_wind_the_limit_up(hass, freezer, meter_seconds, resolution_w):
    """The sun drops below what the house uses and comes back: nothing is fed in then."""
    writes, fed_in_wh = await _run(
        hass,
        freezer,
        "/30",
        meter_seconds,
        resolution_w,
        phase=0,
        seconds=900,
        events={300: {"sun_w": 400.0}, 600: {"sun_w": 6000.0}},
    )

    # 400 W from the roof and 600 W from the grid are still 1000 W.
    assert {percent for second, percent in writes if second > 90} == {12}, writes
    assert fed_in_wh == 0


async def test_without_the_wait_after_a_change_the_first_limit_is_wrong(hass, freezer):
    """What the two waits in the blueprint are for, told by the house itself.

    The meter reports every 30 seconds, one second after the limit is
    written, and so before the inverter has followed. That reading is
    still the 5000 W fed in at full power. With the 960 W the inverter
    delivers by then it would make a house that uses less than nothing.
    """
    writes, _ = await _run(hass, freezer, "/30", 30, 1.0, phase=2, seconds=300)

    assert 0 not in {percent for _, percent in writes}, writes
