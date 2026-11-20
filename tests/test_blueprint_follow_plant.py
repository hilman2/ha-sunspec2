"""The blueprint that follows the house consumption, in a closed loop with a house.

``test_blueprint_follow_consumption.py`` checks one round at a time,
with readings a test sets by hand. That cannot show what the loop does
to itself: a limit changes what the meter reads next. Here a small
model of a house answers every write, second by second. The inverter
power comes through a ``DataUpdateCoordinator`` as it does in the
integration: polled every 30 seconds, read again ten seconds after a
write, never twice within ten seconds, and a read takes its time.

Every defect this loop had was a reading from the wrong side of a
change of the limit, and each showed up here as a limit far outside
what the house uses.

A normal run takes a few kinds of meter, each with one kind of
inverter. After a change to the loop, run every meter with every
inverter:

    docker compose -f tests/docker/compose.yml run --rm \
        -e SUNSPEC2_PLANT_GRID=full tests \
        pytest tests/test_blueprint_follow_plant.py -q -n auto --no-cov

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

import asyncio
import logging
import os
import random
from collections import deque
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
    """A house with solar power: what it uses, what the sun gives, what the inverter may deliver."""

    def __init__(self, follow_seconds: float, noise_w: float) -> None:
        self.load_w = 1000.0
        self.sun_w = 6000.0
        self.limit_percent = 100.0
        #: How long the inverter takes to follow a new limit.
        self.follow_seconds = follow_seconds
        #: How long a read of the inverter takes. 0 until the test is set up.
        self.read_seconds = 0.0
        self._noise_w = noise_w
        self._noise = 0.0
        # The same noise in every run, so that a failure can be repeated.
        self._random = random.Random(20261120)
        self._on_its_way: tuple[float, float] | None = None

    def write(self, percent: float, at: float) -> None:
        self._on_its_way = (at + self.follow_seconds, percent)

    def tick(self, at: float) -> None:
        self._noise = self._random.uniform(-self._noise_w, self._noise_w)
        if self._on_its_way is not None and at >= self._on_its_way[0]:
            self.limit_percent = self._on_its_way[1]
            self._on_its_way = None

    @property
    def inverter_w(self) -> float:
        return min(self.sun_w, self.limit_percent / 100 * RATED_W)

    @property
    def import_w(self) -> float:
        """Positive while the house takes power from the grid, negative while it feeds in."""
        return self.load_w + self._noise - self.inverter_w


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


async def _run(
    hass,
    freezer,
    seconds,
    interval="30",
    meter_seconds=1,
    phase=0,
    resolution_w=1.0,
    lag_seconds=0,
    read_seconds=0.0,
    follow_seconds=2.0,
    noise_w=0.0,
    events=None,
    count_from=120,
):
    """Run the automation against the house for ``seconds`` and return what happened.

    Args:
        interval: The blueprint's input: the period in seconds, as a string.
        meter_seconds: How often the meter reports.
        phase: The second within ``meter_seconds`` at which the meter reports.
        resolution_w: The step of the meter's readings.
        lag_seconds: How old the power is that the meter reports.
        read_seconds: How long a read of the inverter takes. The value
            read is the one from the beginning of the read.
        follow_seconds: How long the inverter takes to follow a new limit.
        noise_w: How far the consumption strays from its value, either way.
        events: Second of the run to a change of the house, as attribute name and value.
        count_from: The second from which the energy fed in is counted.

    Returns:
        tuple[list[tuple[int, float]], float]: A two-way tuple (writes,
        fed_in_wh). ``writes`` holds the second of the run and the
        percentage of every write. ``fed_in_wh`` is the energy fed into
        the grid from ``count_from`` on.
    """
    freezer.move_to("2030-06-01 12:00:00+00:00")
    house = _House(follow_seconds, noise_w)
    start = dt_util.utcnow().timestamp()

    async def read() -> float:
        value = house.inverter_w
        if house.read_seconds:
            await asyncio.sleep(house.read_seconds)
        return value

    coordinator = DataUpdateCoordinator(
        hass,
        logging.getLogger(__name__),
        config_entry=None,
        name="house",
        update_method=read,
        update_interval=timedelta(seconds=30),
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
    house.read_seconds = read_seconds

    fed_in_ws = 0.0
    # What the meter will report: the power of the last seconds.
    seen = deque([house.import_w], maxlen=lag_seconds + 1)
    for second in range(1, seconds + 1):
        freezer.tick(timedelta(seconds=1))
        for name, value in (events or {}).get(second, {}).items():
            setattr(house, name, value)
        house.tick(dt_util.utcnow().timestamp())
        seen.append(house.import_w)
        if second % meter_seconds == phase:
            meter.report(seen[0])
        async_fire_time_changed(hass, dt_util.utcnow())
        await settle(hass)
        if second > count_from:
            fed_in_ws += max(0.0, -house.import_w)
    # Its timers would outlive the test.
    await coordinator.async_shutdown()
    return writes, fed_in_ws / 3600


def _longest_gap(writes, seconds):
    """The longest time without a write, from the first write to the end of the run."""
    times = [second for second, _ in writes] + [seconds]
    return max(later - earlier for earlier, later in zip(times, times[1:], strict=False))


# Meters from one report a second to one per period, in every phase
# against the tick, fine and coarse.
_METERS = [
    {"meter_seconds": 1},
    {"meter_seconds": 1, "resolution_w": 10.0},
    {"meter_seconds": 1, "resolution_w": 100.0},
    {"meter_seconds": 2, "phase": 1, "lag_seconds": 3},
    {"meter_seconds": 5, "phase": 3, "resolution_w": 10.0},
    {"meter_seconds": 10, "phase": 4},
    {"meter_seconds": 10, "phase": 9, "resolution_w": 100.0},
    {"meter_seconds": 15, "phase": 7},
    {"meter_seconds": 30, "phase": 0},
    {"meter_seconds": 30, "phase": 1},
    {"meter_seconds": 30, "phase": 2, "resolution_w": 10.0},
    {"meter_seconds": 30, "phase": 6},
    {"meter_seconds": 30, "phase": 12},
    {"meter_seconds": 30, "phase": 27},
    {"meter_seconds": 30, "phase": 29},
]
# Inverters: quick to read and to follow, slow to read, slow to follow, both.
_INVERTERS = [
    {},
    {"read_seconds": 3.0},
    {"read_seconds": 4.0, "follow_seconds": 5.0},
    {"follow_seconds": 8.0},
]


def _around(percent, meter):
    """The limits that fit a consumption, given what the meter can resolve.

    A meter in steps of 100 W is up to 50 W off, which is more than
    half a percent of the 8000 W, so the limit can be one percent off.
    """
    off = 1 if meter.get("resolution_w", 1.0) >= 100 else 0
    return set(range(percent - off, percent + off + 1))


def _unseen_w(meter):
    """The feed-in a meter cannot see: half a step of its readings."""
    resolution_w = meter.get("resolution_w", 1.0)
    return resolution_w / 2 if resolution_w >= 100 else 0


def _name(case):
    return ",".join(f"{key}={value}" for key, value in case.items()) or "plain"


def _cases(meters, first_inverter=0):
    """The pairs of a meter and an inverter a test runs with.

    Every meter with every inverter when SUNSPEC2_PLANT_GRID is "full".
    Otherwise every third meter with one inverter, taken in turn
    beginning with ``first_inverter``: the suite runs on every change
    to the integration, and a second of this model costs as much as a
    whole test elsewhere.
    """
    if os.environ.get("SUNSPEC2_PLANT_GRID") == "full":
        pairs = [(meter, inverter) for meter in meters for inverter in _INVERTERS]
    else:
        pairs = [
            (meter, _INVERTERS[(index + first_inverter) % len(_INVERTERS)])
            for index, meter in enumerate(meters[1::3])
        ]
    return [
        pytest.param(meter, inverter, id=f"{_name(meter)}|{_name(inverter)}")
        for meter, inverter in pairs
    ]


@pytest.mark.parametrize(("meter", "inverter"), _cases(_METERS))
async def test_a_steady_house_gets_a_steady_limit(hass, freezer, meter, inverter):
    """1000 W of consumption under 6000 W of sun: 12 % of 8000 W, and it stays there."""
    writes, fed_in_wh = await _run(hass, freezer, 420, noise_w=15.0, **meter, **inverter)

    # The first limit is there after two periods at the latest.
    assert writes and writes[0][0] <= 30 + 2 * 30, writes[:3]
    assert {percent for _, percent in writes} <= _around(12, meter), writes
    # 960 W delivered for about 1000 W of consumption: nothing goes
    # into the grid, or what a coarse meter cannot see.
    assert fed_in_wh <= _unseen_w(meter) * 300 / 3600
    # A limit in every period or the one after, so that an inverter
    # with a revert time of three periods never runs free.
    assert _longest_gap(writes, 420) < 2 * 30 + 10, writes


@pytest.mark.parametrize(("meter", "inverter"), _cases(_METERS, first_inverter=1))
async def test_the_limit_follows_a_change_of_the_consumption_and_settles(
    hass, freezer, meter, inverter
):
    """The oven goes on after four minutes and off after eight: 1000 W, 3000 W, 1000 W."""
    writes, _ = await _run(
        hass,
        freezer,
        720,
        noise_w=15.0,
        events={240: {"load_w": 3000.0}, 480: {"load_w": 1000.0}},
        **meter,
        **inverter,
    )
    # Three periods for the automation to see a change, to write the
    # limit that fits it and to see that one confirmed.
    settled = 90 + 10

    def limits(first, last):
        return {percent for second, percent in writes if first <= second < last}

    assert limits(settled, 240) <= _around(12, meter), writes
    # 3000 W of 8000 W are 37.5 %.
    assert limits(240 + settled, 480) <= _around(37, meter), writes
    assert limits(480 + settled, 720) <= _around(12, meter), writes
    # Every period had its limit.
    assert limits(settled, 240) and limits(240 + settled, 480) and limits(480 + settled, 720)
    # On the way from one to the other the limit never leaves the
    # range between the two. A reading from the wrong side of a change
    # would send it far outside.
    assert limits(settled, 720) <= set(range(11, 39)), writes


@pytest.mark.parametrize(
    ("meter", "inverter"),
    _cases(
        [
            {"meter_seconds": 60, "phase": 40},
            {"meter_seconds": 60, "phase": 3},
            {"meter_seconds": 60, "phase": 58},
            {"meter_seconds": 20, "phase": 11},
            {"meter_seconds": 60, "phase": 20, "resolution_w": 10.0},
        ],
        first_inverter=2,
    ),
)
async def test_a_meter_that_reports_once_a_minute_with_a_period_of_a_minute(
    hass, freezer, meter, inverter
):
    writes, fed_in_wh = await _run(
        hass,
        freezer,
        900,
        interval="60",
        events={420: {"load_w": 3000.0}},
        # The first limit can take two periods.
        count_from=240,
        **meter,
        **inverter,
    )

    assert {percent for second, percent in writes if second < 420} == {12}, writes
    assert {percent for second, percent in writes if second > 420 + 200} == {37}, writes
    assert {percent for _, percent in writes} <= set(range(12, 38)), writes
    assert fed_in_wh == 0
    assert _longest_gap(writes, 900) < 2 * 60 + 10, writes


@pytest.mark.parametrize(
    ("meter", "inverter"),
    _cases([_METERS[0], _METERS[4], _METERS[10], _METERS[5], _METERS[13]], first_inverter=1),
)
async def test_a_cloud_does_not_wind_the_limit_up(hass, freezer, meter, inverter):
    """The sun drops below what the house uses and comes back.

    While the sun is gone the limit does not creep up, so the inverter
    does not start at full power when the sun is back.

    The two readings of a round are seconds apart. A change of the sun
    that falls between them makes one limit that fits neither before
    nor after, and the next round puts it right.
    """
    writes, fed_in_wh = await _run(
        hass,
        freezer,
        720,
        events={240: {"sun_w": 400.0}, 480: {"sun_w": 6000.0}},
        **meter,
        **inverter,
    )

    def limits(first, last):
        return {percent for second, percent in writes if first <= second < last}

    # 400 W from the roof and 600 W from the grid are still 1000 W.
    for first, last in ((100, 240), (240 + 75, 480), (480 + 75, 720)):
        assert limits(first, last) == {12}, writes
    # Never far from what the house uses: 400 W to 1600 W.
    assert limits(100, 720) <= set(range(5, 21)), writes
    # At most 600 W too much, for one period and until the meter has
    # reported it.
    assert fed_in_wh <= 600 * (30 + meter["meter_seconds"] + 15) / 3600
