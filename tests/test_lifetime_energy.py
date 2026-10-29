"""The lifetime total counted from a daily energy counter."""

from datetime import datetime

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.lifetime_energy import MAX_DAILY_WH
from custom_components.sunspec2.lifetime_energy import CountedLifetimeEnergySensor
from custom_components.sunspec2.lifetime_energy import DailyCounterDisplay
from custom_components.sunspec2.lifetime_energy import DailyCounterLedger
from custom_components.sunspec2.lifetime_energy import counter_day
from custom_components.sunspec2.sensor import DailyEnergySensor

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG

DAY_1 = "2026-10-11"
DAY_2 = "2026-10-12"


def _feed(ledger: DailyCounterLedger, readings, day=DAY_1):
    for reading in readings:
        ledger.record(reading, day)


def test_a_rising_counter_is_the_total():
    ledger = DailyCounterLedger()
    _feed(ledger, [100, 150, 150, 400])
    assert ledger.total == 400


def test_a_new_day_adds_the_finished_day():
    ledger = DailyCounterLedger()
    _feed(ledger, [1000, 3000])
    # The ECU resets in the night; the sun comes up and the counter climbs.
    _feed(ledger, [0, 0], DAY_2)
    assert ledger.total == 3000
    _feed(ledger, [20, 60], DAY_2)
    assert ledger.total == 3060


def test_a_stray_drop_does_not_start_a_day():
    ledger = DailyCounterLedger()
    _feed(ledger, [2000, 2100])
    # At dusk the ECU can report 30 Wh for one poll.
    _feed(ledger, [30, 2100, 2150])
    assert ledger.total == 2150


def test_a_stray_high_reading_does_not_stick():
    ledger = DailyCounterLedger()
    _feed(ledger, [500, 600])
    _feed(ledger, [40000, 650])
    assert ledger.total == 650


def test_a_large_jump_counts_once_the_next_reading_confirms_it():
    ledger = DailyCounterLedger()
    _feed(ledger, [500])
    _feed(ledger, [4000, 4050])
    assert ledger.total == 4050


def test_a_home_assistant_that_was_off_over_a_night_books_yesterday():
    ledger = DailyCounterLedger()
    _feed(ledger, [3000])
    # Back the next day at noon, with a counter already above yesterday's
    # by more than one step: still a new day.
    _feed(ledger, [5800, 5850], DAY_2)
    assert ledger.total == 3000 + 5850


def test_a_restart_in_the_morning_books_yesterday():
    ledger = DailyCounterLedger()
    _feed(ledger, [3000])
    _feed(ledger, [40, 90], DAY_2)
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
    ledger.record(500, counter_day(datetime(2026, 10, 28, 9, 0), producing=True))
    ledger.record(3800, counter_day(datetime(2026, 10, 29, 1, 0), producing=False))
    ledger.record(3800, counter_day(datetime(2026, 10, 29, 1, 5), producing=False))
    ledger.record(0, counter_day(datetime(2026, 10, 29, 7, 40), producing=False))
    ledger.record(1000, counter_day(datetime(2026, 10, 29, 12, 30), producing=True))
    assert ledger.total == 3800 + 1000


def test_an_outage_from_the_night_into_the_afternoon_books_yesterday():
    ledger = DailyCounterLedger()
    ledger.record(1000, counter_day(datetime(2026, 10, 28, 17, 0), producing=False))
    # The last poll before the outage, after midnight: yesterday's counter.
    ledger.record(1000, counter_day(datetime(2026, 10, 29, 3, 0), producing=False))
    ledger.record(3500, counter_day(datetime(2026, 10, 29, 14, 0), producing=True))
    ledger.record(3500, counter_day(datetime(2026, 10, 29, 14, 5), producing=True))
    assert ledger.total == 1000 + 3500


def test_a_cloud_in_the_morning_does_not_take_the_day_back():
    ledger = DailyCounterLedger()
    ledger.record(3000, counter_day(datetime(2026, 10, 28, 18, 0), producing=False))
    ledger.record(0, counter_day(datetime(2026, 10, 29, 7, 30), producing=False))
    ledger.record(100, counter_day(datetime(2026, 10, 29, 8, 0), producing=True))
    # Back after an outage with a jump, confirmed under a cloud at 4 W.
    ledger.record(2300, counter_day(datetime(2026, 10, 29, 10, 30), producing=True))
    ledger.record(2310, counter_day(datetime(2026, 10, 29, 10, 35), producing=False))
    assert ledger.total == 3000 + 2310


def test_a_jump_confirmed_under_a_cloud_keeps_the_day_it_was_seen_on():
    ledger = DailyCounterLedger()
    ledger.record(1000, counter_day(datetime(2026, 10, 28, 17, 0), producing=False))
    ledger.record(1000, counter_day(datetime(2026, 10, 29, 3, 0), producing=False))
    ledger.record(3500, counter_day(datetime(2026, 10, 29, 10, 30), producing=True))
    ledger.record(3510, counter_day(datetime(2026, 10, 29, 10, 35), producing=False))
    assert ledger.total == 1000 + 3510


def test_impossible_values_are_left_out():
    ledger = DailyCounterLedger()
    _feed(ledger, [100, float("nan"), -5, MAX_DAILY_WH + 1, float("inf")])
    assert ledger.total == 100


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
    freezer.move_to(_local(29, 1, 5))
    _poll(entry, 3800, 0)
    # At dawn the micro inverter reports, the counter starts again and climbs.
    freezer.move_to(_local(29, 7, 40))
    _poll(entry, 0, 2)
    freezer.move_to(_local(29, 12, 30))
    _poll(entry, 1000, 250)

    assert sensor.native_value == 4.8


def _daily(hass):
    component = hass.data["entity_components"]["sensor"]
    return next(e for e in component.entities if isinstance(e, DailyEnergySensor))


def _read(display, readings, date, day):
    """Record readings taken on ``date`` for counter ``day``; return what is shown."""
    shown = []
    for reading in readings:
        display.record(reading, date, day)
        shown.append(display.shown(reading))
    return shown


def _past_midnight(yesterday=3800):
    """A display that has carried yesterday's value into the night."""
    display = DailyCounterDisplay()
    display.record(yesterday, DAY_1, DAY_1)
    display.record(yesterday, DAY_2, DAY_1)
    return display


def test_yesterdays_counter_shows_zero_until_it_moves():
    display = DailyCounterDisplay()
    assert _read(display, [2300], DAY_1, DAY_1) == [2300]
    assert _read(display, [2300], DAY_2, DAY_1) == [0]
    # The micro inverter reports: the counter starts again and climbs.
    assert _read(display, [0], DAY_2, DAY_1) == [0]
    assert _read(display, [40], DAY_2, DAY_2) == [40]


def test_a_dull_morning_restart_ends_the_carried_value_by_its_climb():
    display = _past_midnight()
    assert _read(display, [0, 0, 3, 8], DAY_2, DAY_1) == [0, 0, 3, 8]


def test_a_counter_that_moved_overnight_is_shown_as_read():
    """Back after an outage with yesterday's counter, higher than last seen: zero would lose it."""
    display = DailyCounterDisplay()
    display.record(60, DAY_1, DAY_1)
    assert _read(display, [1650], DAY_2, DAY_1) == [1650]


def test_a_day_that_reaches_yesterdays_value_does_not_drop_to_zero():
    display = _past_midnight(2300)
    _read(display, [0], DAY_2, DAY_1)
    assert _read(display, [15, 900, 2300], DAY_2, DAY_2) == [15, 900, 2300]


@pytest.mark.parametrize(
    "night",
    [
        # The ECU repeats a reading for its whole 5 minute round.
        [3790, 3790, 3790, 3800],
        # Two strays, the second above the first: no restart, which starts lower.
        [3790, 3795, 3800],
        [0, 0, 3800],
        [3810, 3810, 3800],
        [4200, 4200, 3800],
    ],
)
def test_strays_at_night_never_bring_yesterdays_value_back(night):
    """Home Assistant would count a rise from zero to 3790 as new energy."""
    display = _past_midnight()
    assert _read(display, night, DAY_2, DAY_1) == [0] * len(night)


def test_a_counter_running_past_midnight_shows_what_it_added():
    """Midnight sun: the micro inverter produces, the counter has not restarted yet."""
    display = _past_midnight()
    assert _read(display, [3802, 3825], DAY_2, DAY_2) == [2, 25]


def test_back_in_the_afternoon_shows_what_the_counter_added():
    """Out from 01:00 to 14:00, today's counter past yesterday's: not lost, not counted twice."""
    display = _past_midnight()
    assert _read(display, [4500, 4600], DAY_2, DAY_2) == [700, 800]


def test_the_display_survives_its_stored_form():
    display = _past_midnight()
    assert DailyCounterDisplay.from_dict(display.as_dict()) == display


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"revision": 99, "carried": 5}, {"revision": 1, "last": "lots", "carried": 5}],
)
def test_a_display_payload_that_does_not_fit_starts_afresh(payload):
    assert DailyCounterDisplay.from_dict(payload) == DailyCounterDisplay()


async def test_the_daily_sensor_shows_zero_after_midnight(
    hass, sunspec_apsystems_client_mock, freezer
):
    freezer.move_to(_local(28, 17))
    entry = await _entry(hass)
    sensor = _daily(hass)
    assert sensor.unique_id == get_sunspec_unique_id(entry.entry_id, "WH", 103, 0)
    _poll(entry, 2300, 0)
    assert sensor.native_value == 2300

    freezer.move_to(_local(29, 0, 10))
    _poll(entry, 2300, 0)
    assert hass.states.get(sensor.entity_id).state == "0.0"

    freezer.move_to(_local(29, 7, 40))
    _poll(entry, 0, 3)
    _poll(entry, 15, 40)
    assert sensor.native_value == 15


async def test_a_restart_at_night_keeps_the_daily_sensor_at_zero(
    hass, sunspec_apsystems_client_mock, freezer
):
    """Showing yesterday's value again after a zero would count it twice in the statistics."""
    freezer.move_to(_local(29, 2))
    sunspec_apsystems_client_mock.models[103][0].points["WH"].value = 2300
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    registered = er.async_get(hass).async_get_or_create(
        "sensor",
        "sunspec2",
        get_sunspec_unique_id(entry.entry_id, "WH", 103, 0),
        config_entry=entry,
        suggested_object_id="energy_today",
    )
    stored = DailyCounterDisplay(last=2300.0, last_date="2026-10-29", carried=2300.0)
    mock_restore_cache_with_extra_data(
        hass, ((State(registered.entity_id, "0.0"), stored.as_dict()),)
    )
    client = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)

    assert hass.states.get(registered.entity_id).state == "0.0"
