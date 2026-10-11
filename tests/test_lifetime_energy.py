"""The lifetime total counted from a daily energy counter."""

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.lifetime_energy import MAX_DAILY_WH
from custom_components.sunspec2.lifetime_energy import CountedLifetimeEnergySensor
from custom_components.sunspec2.lifetime_energy import DailyCounterLedger

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
