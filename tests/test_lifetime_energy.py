"""The lifetime total counted from a daily energy counter."""

from datetime import datetime
from datetime import timedelta
from itertools import pairwise

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.lifetime_energy import CONFIRM_SECONDS
from custom_components.sunspec2.lifetime_energy import MAX_DAILY_WH
from custom_components.sunspec2.lifetime_energy import CountedDailyEnergySensor
from custom_components.sunspec2.lifetime_energy import CountedLifetimeEnergySensor
from custom_components.sunspec2.lifetime_energy import DailyCounterLedger
from custom_components.sunspec2.lifetime_energy import counter_day

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG

#: Long enough for the next poll to come from a later ECU round.
ROUND = timedelta(seconds=CONFIRM_SECONDS)


def _at(day: int, hour: int = 14, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second)


def _feed(ledger, readings, start=None, producing=True):
    """One reading per ECU round, from ``start`` on. Returns the moment after the last."""
    now = start or _at(11)
    for reading in readings:
        ledger.record(reading, now, producing)
        now += ROUND
    return now


def _book_statistics(states):
    """What Home Assistant's statistics make of a total_increasing sensor.

    The rule is ``reset_detected`` and the sum in ``compile_statistics`` of
    ``homeassistant.components.sensor.recorder``: the first state is the
    zero point, a drop below 90 % restarts from 0 so the new value counts in
    full, and any other change counts by its difference.
    """
    booked = 0.0
    reference = None
    for state in states:
        if state is None:
            continue
        if reference is not None:
            booked += state if state < 0.9 * reference else state - reference
        reference = state
    return booked


def _day_of_polls(ledger, script):
    """Feed ``(moment, reading, producing)`` every 30 s, as the default poll does.

    Returns the sensor's state after every poll. Each entry of ``script``
    holds until the moment of the next one.
    """
    shown = [ledger.today]
    for (start, reading, producing), (stop, _, _) in pairwise(script):
        now = start
        while now < stop:
            ledger.record(reading, now, producing)
            shown.append(ledger.today)
            now += timedelta(seconds=30)
    return shown


def test_a_rising_counter_is_the_total():
    ledger = DailyCounterLedger()
    _feed(ledger, [100, 150, 150, 400])
    assert ledger.total == 400


def test_a_new_day_adds_the_finished_day():
    ledger = DailyCounterLedger()
    _feed(ledger, [1000, 3000])
    # The ECU restarts in the morning; the sun comes up and the counter climbs.
    next_poll = _feed(ledger, [0, 0], _at(12, 8))
    assert ledger.total == 3000
    _feed(ledger, [20, 60], next_poll)
    assert ledger.total == 3060


def test_a_stray_drop_does_not_start_a_day():
    ledger = DailyCounterLedger()
    _feed(ledger, [2000, 2100, 30, 2100, 2150])
    assert ledger.total == 2150


def test_a_stray_high_reading_does_not_stick():
    ledger = DailyCounterLedger()
    _feed(ledger, [500, 600, 40000, 650])
    assert ledger.total == 650


@pytest.mark.parametrize(
    ("readings", "expected"),
    [
        # #162, measured on the release before.
        ([3800, 9000, 9000, 3805], 3805),
        # The same stray, from the first and from the last poll of its round.
        ([3800, 30, 30, 3805], 3805),
        ([3800, 30, 3805], 3805),
    ],
)
def test_a_stray_repeated_through_its_round_stays_out(readings, expected):
    """The default poll reads one ECU round ten times."""
    ledger = DailyCounterLedger()
    now = _at(11)
    for reading in readings:
        ledger.record(reading, now, True)
        now += timedelta(seconds=30)
    assert ledger.total == expected


def test_a_dip_over_several_rounds_is_no_new_day():
    """#162: two low strays, the second above the first, one round each."""
    ledger = DailyCounterLedger()
    _feed(ledger, [3800, 3790, 3795, 3800])
    assert ledger.total == 3800


def test_a_large_jump_counts_once_a_later_round_confirms_it():
    ledger = DailyCounterLedger()
    start = _feed(ledger, [500])
    ledger.record(4000, start, True)
    ledger.record(4000, start + timedelta(seconds=30), True)
    assert ledger.total == 500
    ledger.record(4050, start + ROUND, True)
    assert ledger.total == 4050


def test_a_stray_is_read_again_after_a_restart_of_home_assistant():
    ledger = DailyCounterLedger()
    start = _feed(ledger, [3800])
    ledger.record(9000, start, True)
    restored = DailyCounterLedger.from_dict(ledger.as_dict())
    # The time of the stray is gone, so this poll only starts the wait.
    restored.record(9000, start + ROUND, True)
    assert restored.total == 3800
    restored.record(3805, start + ROUND + timedelta(seconds=30), True)
    assert restored.total == 3805


def test_a_home_assistant_that_was_off_over_a_night_books_yesterday():
    ledger = DailyCounterLedger()
    _feed(ledger, [3000])
    # Back the next day at noon, with a counter already above yesterday's
    # by more than one step: still a new day.
    _feed(ledger, [5800, 5850], _at(12))
    assert ledger.total == 3000 + 5850


def test_a_restart_in_the_morning_books_yesterday():
    ledger = DailyCounterLedger()
    _feed(ledger, [3000])
    _feed(ledger, [40, 90], _at(12, 9))
    assert ledger.total == 3090


def test_a_reading_is_yesterdays_until_the_inverter_produces_or_it_is_noon():
    assert counter_day(datetime(2026, 10, 29, 1, 0), producing=False) == "2026-10-28"
    assert counter_day(datetime(2026, 10, 29, 8, 0), producing=True) == "2026-10-29"
    assert counter_day(datetime(2026, 10, 29, 14, 0), producing=False) == "2026-10-29"


def test_a_jump_seen_at_night_is_still_the_same_day():
    """#160: the ECU keeps yesterday's counter until the inverter reports in the morning.

    Home Assistant saw 500 Wh in the morning and nothing more until 01:00,
    when the counter still showed the 3800 Wh of the whole day.
    """
    ledger = DailyCounterLedger()
    ledger.record(500, datetime(2026, 10, 28, 9, 0), True)
    ledger.record(3800, datetime(2026, 10, 29, 1, 0), False)
    ledger.record(3800, datetime(2026, 10, 29, 1, 6), False)
    ledger.record(0, datetime(2026, 10, 29, 7, 40), False)
    ledger.record(1000, datetime(2026, 10, 29, 12, 30), True)
    assert ledger.total == 3800 + 1000


def test_an_outage_from_the_night_into_the_afternoon_books_yesterday():
    ledger = DailyCounterLedger()
    ledger.record(1000, datetime(2026, 10, 28, 17, 0), False)
    # The last poll before the outage, after midnight: yesterday's counter.
    ledger.record(1000, datetime(2026, 10, 29, 3, 0), False)
    ledger.record(3500, datetime(2026, 10, 29, 14, 0), True)
    ledger.record(3500, datetime(2026, 10, 29, 14, 6), True)
    assert ledger.total == 1000 + 3500


def test_a_cloud_in_the_morning_does_not_take_the_day_back():
    ledger = DailyCounterLedger()
    ledger.record(3000, datetime(2026, 10, 28, 18, 0), False)
    ledger.record(0, datetime(2026, 10, 29, 7, 30), False)
    ledger.record(100, datetime(2026, 10, 29, 8, 0), True)
    # Back after an outage with a jump, confirmed under a cloud at 4 W.
    ledger.record(2300, datetime(2026, 10, 29, 10, 30), True)
    ledger.record(2310, datetime(2026, 10, 29, 10, 36), False)
    assert ledger.total == 3000 + 2310


def test_a_jump_confirmed_under_a_cloud_keeps_the_day_it_was_seen_on():
    ledger = DailyCounterLedger()
    ledger.record(1000, datetime(2026, 10, 28, 17, 0), False)
    ledger.record(1000, datetime(2026, 10, 29, 3, 0), False)
    ledger.record(3500, datetime(2026, 10, 29, 10, 30), True)
    ledger.record(3510, datetime(2026, 10, 29, 10, 36), False)
    assert ledger.total == 1000 + 3510


def test_impossible_values_are_left_out():
    ledger = DailyCounterLedger()
    _feed(ledger, [100, float("nan"), -5, MAX_DAILY_WH + 1, float("inf")])
    assert ledger.total == 100


def _produced_today(script, yesterday=3800):
    """Run ``script`` after a day that ended at ``yesterday`` at 23:59.

    Returns the state at the end and what the statistics booked after the
    zero point, which is that day.
    """
    ledger = DailyCounterLedger()
    ledger.record(yesterday, _at(27, 23, 59), False)
    shown = [ledger.today, *_day_of_polls(ledger, script)]
    return shown[-1], _book_statistics(shown)


NIGHT = _at(28, 0, 0, 10)
END = _at(28, 23, 30)


@pytest.mark.parametrize(
    ("script", "today"),
    [
        pytest.param(
            [
                (NIGHT, 3800, False),
                (_at(28, 7, 40), 0, False),
                (_at(28, 7, 45), 20, True),
                (_at(28, 12), 2000, True),
                (_at(28, 18), 3000, False),
                (END, 3000, False),
            ],
            3000,
            id="a normal day",
        ),
        pytest.param(
            [
                (NIGHT, 3800, False),
                (_at(28, 2), 9000, False),
                (_at(28, 2, 5), 3800, False),
                (_at(28, 3), 30, False),
                (_at(28, 3, 5), 3800, False),
                (_at(28, 7, 40), 0, False),
                (_at(28, 7, 45), 20, True),
                (_at(28, 10), 1000, True),
                (_at(28, 10, 5), 9000, True),
                (_at(28, 10, 10), 1020, True),
                (_at(28, 18), 3000, False),
                (END, 3000, False),
            ],
            3000,
            id="strays for a whole round, at night and by day",
        ),
        pytest.param(
            [
                (NIGHT, 3800, False),
                (_at(28, 14), 3800, False),
                (_at(28, 14, 5), 9000, False),
                (_at(28, 14, 10), 3800, False),
                (_at(28, 15), 30, False),
                (_at(28, 15, 5), 3800, False),
                (END, 3800, False),
            ],
            0,
            id="a day without production, with strays",
        ),
        pytest.param(
            [(_at(28, 14), 30, False), (_at(28, 14, 10), 3800, False), (END, 3800, False)],
            0,
            id="an outage that ends in a stray for two rounds, on a day without production",
        ),
        pytest.param(
            [
                (NIGHT, 3800, False),
                (_at(28, 3), 30, False),
                (_at(28, 3, 40), 3800, False),
                (END, 3800, False),
            ],
            0,
            id="a low reading for half an hour at night",
        ),
        pytest.param(
            [
                (NIGHT, 3800, False),
                (_at(28, 2), 4000, False),
                (_at(28, 2, 5), 3800, False),
                (END, 3800, False),
            ],
            0,
            id="a small stray at night before a day without production",
        ),
        pytest.param(
            [(_at(28, 9), 300, True), (_at(28, 9, 5), 400, True), (END, 400, False)],
            400,
            id="an outage that ends in the morning",
        ),
        pytest.param(
            [(_at(28, 14), 3500, True), (_at(28, 14, 5), 3550, True), (END, 3550, False)],
            3550,
            id="an outage that ends in the afternoon, just under yesterday",
        ),
        pytest.param(
            [(_at(28, 14), 4500, True), (_at(28, 14, 5), 4550, True), (END, 4550, False)],
            4550,
            id="an outage that ends in the afternoon, just above yesterday",
        ),
        pytest.param(
            [(_at(28, 20), 3000, False), (END, 3000, False)],
            3000,
            id="an outage that ends in the evening, below yesterday",
        ),
        pytest.param(
            [(_at(28, 20), 4500, False), (END, 4500, False)],
            4500,
            id="an outage that ends in the evening, just above yesterday",
        ),
        pytest.param(
            [(_at(28, 20), 3500, False), (END, 3500, False)],
            3500,
            id="an outage that ends in the evening, just under yesterday",
        ),
        pytest.param(
            [(_at(28, 20), 6500, False), (END, 6500, False)],
            6500,
            id="an outage that ends in the evening, far above yesterday",
        ),
        pytest.param(
            [
                (_at(28, 0, 1), 3810, True),
                (_at(28, 0, 30), 3900, True),
                (_at(28, 1), 3950, False),
                (END, 3950, False),
            ],
            150,
            id="production at midnight, without a restart",
        ),
        pytest.param(
            [
                (_at(28, 0, 1), 3810, True),
                (_at(28, 0, 30), 3900, True),
                (_at(28, 1), 0, False),
                (_at(28, 8), 20, True),
                (_at(28, 12), 1020, True),
                (END, 1020, False),
            ],
            100 + 1020,
            id="production at midnight, with a later restart",
        ),
        pytest.param(
            [
                (_at(28, 0, 1), 3810, True),
                (_at(28, 1), 3950, True),
                (_at(28, 5), 4000, True),
                (END, 4000, False),
            ],
            200,
            id="production at midnight, and a restart of Home Assistant in it",
        ),
    ],
)
def test_todays_energy_books_the_day_once(script, today):
    """Every scenario of #163, read the way Home Assistant's statistics read the sensor."""
    shown, booked = _produced_today(script)
    assert shown == today
    assert booked == today


def test_today_is_zero_until_the_counter_restarts():
    ledger = DailyCounterLedger()
    ledger.record(3000, _at(27, 18), False)
    assert ledger.today == 3000
    ledger.record(3000, _at(28, 0, 1), False)
    assert ledger.today == 0
    ledger.record(0, _at(28, 7, 40), False)
    ledger.record(20, _at(28, 7, 46), True)
    assert ledger.today == 20


def test_the_first_reading_ever_at_night_is_not_today():
    ledger = DailyCounterLedger()
    ledger.record(3000, _at(28, 2), False)
    assert ledger.today == 0
    assert ledger.total == 3000


def test_the_first_reading_ever_by_day_is_today():
    ledger = DailyCounterLedger()
    ledger.record(3000, _at(28, 14), False)
    assert ledger.today == 3000


def test_today_is_kept_across_a_restart_of_home_assistant_at_night():
    ledger = DailyCounterLedger()
    ledger.record(3000, _at(27, 18), False)
    ledger.record(3000, _at(28, 0, 1), False)
    restored = DailyCounterLedger.from_dict(ledger.as_dict())
    assert restored.today == 0
    restored.record(3000, _at(28, 3), False)
    assert restored.today == 0


def test_a_home_assistant_that_was_off_over_midnight_starts_today_at_zero():
    ledger = DailyCounterLedger()
    ledger.record(3000, _at(27, 18), False)
    restored = DailyCounterLedger.from_dict(ledger.as_dict())
    restored.record(3000, _at(28, 3), False)
    assert restored.today == 0


def test_a_ledger_stored_before_today_was_kept_stays_readable():
    stored = {
        "revision": 1,
        "base": 1000.0,
        "last": 500.0,
        "last_day": "2026-10-27",
        "pending": None,
    }
    ledger = DailyCounterLedger.from_dict(stored)
    assert ledger.total == 1500
    ledger.record(600, _at(27, 16), True)
    assert ledger.total == 1600


def test_the_ledger_survives_its_stored_form():
    ledger = DailyCounterLedger()
    _feed(ledger, [1000, 3000, 20, 0])
    restored = DailyCounterLedger.from_dict(ledger.as_dict())
    assert restored == ledger


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"revision": 99, "base": 5, "last": 5},
        {"revision": 1, "base": "lots", "last": 5},
        {"revision": 1, "base": 1.0, "last": True},
    ],
)
def test_a_payload_that_does_not_fit_starts_a_new_ledger(payload):
    assert DailyCounterLedger.from_dict(payload) == DailyCounterLedger()


async def _entry(hass):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    client = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)
    return entry


def _counted(hass):
    component = hass.data["entity_components"]["sensor"]
    return next(e for e in component.entities if isinstance(e, CountedLifetimeEnergySensor))


async def test_a_daily_counter_gets_a_lifetime_total(hass, sunspec_apsystems_client_mock):
    entry = await _entry(hass)
    sensor = _counted(hass)
    wh = entry.runtime_data.data[103].getPoint("WH")

    assert isinstance(sensor, CountedLifetimeEnergySensor)
    assert sensor.unique_id == get_sunspec_unique_id(entry.entry_id, "lifetime:WH", 103, 0)
    assert sensor.state_class == SensorStateClass.TOTAL_INCREASING
    # The fixture's counter reads 100 kWh, which no micro inverter makes in a
    # day, so it is not counted.
    assert sensor.native_value is None

    wh.value = 2000
    entry.runtime_data.async_update_listeners()
    assert sensor.native_value == 2.0

    # The daily counter climbs by 250 Wh; the total follows in kWh.
    wh.value = 2250
    entry.runtime_data.async_update_listeners()
    assert sensor.native_value == 2.25


async def test_a_device_with_a_lifetime_counter_gets_no_counted_total(hass, sunspec_client_mock):
    await _entry(hass)
    component = hass.data["entity_components"]["sensor"]
    assert not any(isinstance(e, CountedLifetimeEnergySensor) for e in component.entities)


async def test_the_total_is_kept_across_a_restart(hass, sunspec_apsystems_client_mock):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    registry = er.async_get(hass)
    registered = registry.async_get_or_create(
        "sensor",
        "sunspec2",
        get_sunspec_unique_id(entry.entry_id, "lifetime:WH", 103, 0),
        config_entry=entry,
        suggested_object_id="counted_total",
    )
    stored = DailyCounterLedger(base=1_000_000.0, last=0.0, last_day="2020-01-01")
    mock_restore_cache_with_extra_data(
        hass, ((State(registered.entity_id, "1000"), stored.as_dict()),)
    )
    client = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)

    sensor = _counted(hass)
    assert sensor.native_value == 1000.0

    # Today's counter starts from zero and is added to the stored days.
    entry.runtime_data.data[103].getPoint("WH").value = 1500
    entry.runtime_data.async_update_listeners()
    assert sensor.native_value == 1001.5


def _local(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in October 2026 in Home Assistant's time zone."""
    return datetime(2026, 10, day, hour, minute, tzinfo=dt_util.get_default_time_zone())


def _poll(entry, watt_hours, watts):
    """What a poll leaves in the coordinator: the daily counter and the AC power."""
    model = entry.runtime_data.data[103]
    model.getPoint("WH").value = watt_hours
    model.getPoint("W").value = watts
    entry.runtime_data.async_update_listeners()


async def test_the_total_books_a_night_reading_for_the_day_before(
    hass, sunspec_apsystems_client_mock, freezer
):
    """#160: back at 01:00 after missing the afternoon, the counter is still yesterday's."""
    freezer.move_to(_local(28, 9))
    entry = await _entry(hass)
    sensor = _counted(hass)
    _poll(entry, 500, 300)

    freezer.move_to(_local(29, 1))
    _poll(entry, 3800, 0)
    freezer.move_to(_local(29, 1, 6))
    _poll(entry, 3800, 0)
    # At dawn the micro inverter reports, the counter starts again and climbs.
    freezer.move_to(_local(29, 7, 40))
    _poll(entry, 0, 2)
    freezer.move_to(_local(29, 12, 30))
    _poll(entry, 1000, 250)

    assert sensor.native_value == 4.8


def _daily(hass):
    component = hass.data["entity_components"]["sensor"]
    return next(e for e in component.entities if isinstance(e, CountedDailyEnergySensor))


async def test_energy_produced_today_shows_0_until_the_counter_restarts(
    hass, sunspec_apsystems_client_mock, freezer
):
    """#163: the sensor drops at midnight and rises only with today's energy."""
    freezer.move_to(_local(28, 16))
    entry = await _entry(hass)
    sensor = _daily(hass)
    _poll(entry, 3800, 300)
    assert sensor.native_value == 3800

    freezer.move_to(_local(29, 0, 1))
    _poll(entry, 3800, 0)
    assert sensor.native_value == 0

    freezer.move_to(_local(29, 7, 40))
    _poll(entry, 0, 2)
    freezer.move_to(_local(29, 7, 46))
    _poll(entry, 20, 250)
    assert sensor.native_value == 20

    state = hass.states.get(sensor.entity_id)
    assert state is not None
    assert state.state == "20.0"
    assert sensor.state_class == SensorStateClass.TOTAL_INCREASING


async def test_energy_produced_today_stays_out_of_the_way_of_the_entity_id(
    hass, sunspec_apsystems_client_mock
):
    entry = await _entry(hass)
    sensor = _daily(hass)
    assert sensor.unique_id == get_sunspec_unique_id(entry.entry_id, "WH", 103, 0)


async def test_a_failed_poll_is_no_later_round(hass, sunspec_apsystems_client_mock, freezer):
    """After a failed poll the coordinator repeats the last values."""
    freezer.move_to(_local(28, 16))
    entry = await _entry(hass)
    sensor = _counted(hass)
    _poll(entry, 3800, 300)

    _poll(entry, 9000, 300)
    entry.runtime_data.consecutive_failed_cycles = 1
    freezer.move_to(_local(28, 16, 10))
    _poll(entry, 9000, 300)
    assert sensor.native_value == 3.8

    entry.runtime_data.consecutive_failed_cycles = 0
    freezer.move_to(_local(28, 16, 11))
    _poll(entry, 3805, 300)
    assert sensor.native_value == 3.805
