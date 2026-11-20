"""The two blueprints that select a battery mode: for a running consumer, for a low price.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

from unittest.mock import patch

import pytest

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .blueprint_helpers import RecordedAction
from .blueprint_helpers import pass_time
from .blueprint_helpers import set_state
from .blueprint_helpers import start_automation
from .blueprint_helpers import sunspec_entity
from .const import MOCK_CONFIG_WRITE

MODE = "select.battery_mode"
MODES = [
    "auto",
    "pv_charge_limit",
    "discharge_limit",
    "charge_and_discharge_limit",
    "charge_from_grid",
    "discharge_to_grid",
    "block_discharging",
    "block_charging",
]
CHARGER = "switch.car_charger"
PRICE = "sensor.price"
SOC = "sensor.battery_level"

pytestmark = pytest.mark.usefixtures("repository_blueprints")


@pytest.fixture(autouse=True)
def two_past_noon(freezer):
    """Start every test at 12:02.

    Both blueprints select their mode again every three minutes, at
    one, four, seven minutes past the hour and so on. With a fixed
    start a test knows which of its minutes are among them.
    """
    freezer.move_to("2030-06-01 12:02:00+00:00")


class Battery(RecordedAction):
    """The mode select of a battery that does what it is told."""

    def __init__(self, hass, mode="auto"):
        super().__init__(hass, "select", "select_option")
        self._hass = hass
        sunspec_entity(hass, MODE, mode, friendly_name="Battery mode", options=MODES)

    async def _handle(self, call):
        await super()._handle(call)
        assert call.data["entity_id"] == [MODE]
        self._hass.states.async_set(
            MODE, call.data["option"], {"friendly_name": "Battery mode", "options": MODES}
        )

    @property
    def selected(self):
        return [call["option"] for call in self.calls]


# ---------------------------------------------------------------------------
# Block battery discharging while a large consumer runs
# ---------------------------------------------------------------------------


async def _block(hass, mode="auto", load=CHARGER, **inputs):
    battery = Battery(hass, mode)
    if hass.states.get(load) is None:
        hass.states.async_set(load, "off", {"friendly_name": "Car charger"})
    await start_automation(
        hass, "block_battery_discharge", {"battery_mode": MODE, "load": load, **inputs}
    )
    return battery


async def test_the_battery_is_held_while_the_consumer_runs(hass, freezer):
    battery = await _block(hass)

    await set_state(hass, CHARGER, "on")
    assert battery.selected == ["block_discharging"]

    await set_state(hass, CHARGER, "off")
    await pass_time(hass, freezer, minutes=4)
    assert battery.selected == ["block_discharging"]

    await pass_time(hass, freezer, minutes=2)
    assert battery.selected == ["block_discharging", "auto"]


async def test_a_pause_shorter_than_the_delay_keeps_the_battery_held(hass, freezer):
    battery = await _block(hass)
    await set_state(hass, CHARGER, "on")

    await set_state(hass, CHARGER, "off")
    await pass_time(hass, freezer, minutes=3)
    await set_state(hass, CHARGER, "on")
    await pass_time(hass, freezer, minutes=10)

    assert battery.selected == ["block_discharging"]


@pytest.mark.parametrize("mode", ["charge_from_grid", "discharge_limit"])
async def test_a_mode_chosen_elsewhere_is_left_alone(hass, freezer, mode):
    """A battery plan or a manual choice is neither replaced nor ended."""
    battery = await _block(hass, mode=mode)

    await set_state(hass, CHARGER, "on")
    await set_state(hass, CHARGER, "off")
    await pass_time(hass, freezer, minutes=6)

    assert battery.selected == []


async def test_the_mode_is_selected_again_before_the_inverter_drops_it(hass, freezer):
    battery = await _block(hass)
    await set_state(hass, CHARGER, "on")

    for _ in range(6):
        await pass_time(hass, freezer, minutes=1)
    # 12:04 and 12:07.
    assert battery.selected == ["block_discharging"] * 3

    await set_state(hass, CHARGER, "off")
    for _ in range(4):
        await pass_time(hass, freezer, minutes=1)
    assert battery.selected == ["block_discharging"] * 3


async def test_a_consumer_that_starts_in_the_minute_of_a_refresh_selects_once(hass, freezer):
    """Both triggers fire at 12:04, and the second run finds its work done."""
    freezer.move_to("2030-06-01 12:04:00+00:00")
    battery = await _block(hass)

    await set_state(hass, CHARGER, "on")

    assert battery.selected == ["block_discharging"]


async def test_an_inverter_that_dropped_the_mode_gets_it_again(hass, freezer):
    battery = await _block(hass, refresh="5")
    await set_state(hass, CHARGER, "on")

    # The revert time ran out: the inverter reports Automatic.
    await set_state(hass, MODE, "auto")
    for _ in range(4):
        await pass_time(hass, freezer, minutes=1)

    assert battery.selected == ["block_discharging", "block_discharging"]
    assert hass.states.get(MODE).state == "block_discharging"


async def test_a_power_sensor_counts_as_running_above_the_threshold(hass, freezer):
    hass.states.async_set("sensor.wallbox", "0.2", {"unit_of_measurement": "kW"})
    battery = await _block(hass, load="sensor.wallbox", power_threshold=1500, stop_delay=1)

    await set_state(hass, "sensor.wallbox", "1.4")
    assert battery.selected == []

    await set_state(hass, "sensor.wallbox", "7.2")
    assert battery.selected == ["block_discharging"]

    await set_state(hass, "sensor.wallbox", "unavailable")
    await pass_time(hass, freezer, minutes=2)
    assert battery.selected == ["block_discharging", "auto"]


async def test_the_discharge_limit_can_be_the_mode(hass, freezer):
    battery = await _block(hass, hold_mode="discharge_limit")

    await set_state(hass, CHARGER, "on")
    await set_state(hass, CHARGER, "off")
    await pass_time(hass, freezer, minutes=6)

    assert battery.selected == ["discharge_limit", "auto"]


async def test_home_assistant_starting_releases_a_battery_left_held(hass):
    battery = await _block(hass, mode="block_discharging")

    hass.bus.async_fire("homeassistant_started")
    await hass.async_block_till_done()

    assert battery.selected == ["auto"]


async def test_an_entity_without_the_mode_is_refused(hass, caplog):
    battery = await _block(hass)
    await set_state(hass, MODE, "auto", options=["off", "charge", "discharge", "both"])

    await set_state(hass, CHARGER, "on")

    assert battery.selected == []
    assert "does not offer this mode" in caplog.text


async def test_a_failed_selection_is_tried_again_and_then_reported(hass, freezer):
    hass.states.async_set("notify.phone", "unknown")
    messages = RecordedAction(hass, "notify", "send_message")
    battery = await _block(hass, notify=["notify.phone"])
    battery.failures = 3

    await set_state(hass, CHARGER, "on", block=False)
    await pass_time(hass, freezer, block=False, seconds=31)
    await pass_time(hass, freezer, seconds=31)

    assert len(battery.calls) == 3
    assert [call["message"] for call in messages.calls] == [
        "The battery mode could not be set. The inverter did not take it in three attempts."
    ]


async def test_the_messages_name_the_consumer(hass, freezer):
    hass.states.async_set("notify.phone", "unknown")
    messages = RecordedAction(hass, "notify", "send_message")
    await _block(hass, notify=["notify.phone"], stop_delay=0)

    await set_state(hass, CHARGER, "on")
    await set_state(hass, CHARGER, "off")

    assert [call["message"] for call in messages.calls] == [
        "The battery does not discharge while Car charger runs.",
        "The battery is back to automatic: Car charger is off.",
    ]


async def test_the_real_select_takes_what_the_blueprint_sends(hass, sunspec_fronius_client_mock):
    """End to end on the Fronius fixture: the blueprint, the Battery mode entity, the device."""
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    (mode_entity,) = [
        state.entity_id
        for state in hass.states.async_all("select")
        if "block_discharging" in state.attributes["options"]
    ]
    assert hass.states.get(mode_entity).state == "auto"
    hass.states.async_set(CHARGER, "off")
    await start_automation(
        hass, "block_battery_discharge", {"battery_mode": mode_entity, "load": CHARGER}
    )
    coordinator = entry.runtime_data
    written = []

    async def spy(model_id, points):
        written.append((model_id, points))

    with (
        patch.object(coordinator.api, "async_write_points", side_effect=spy),
        patch.object(coordinator, "async_request_refresh"),
    ):
        await set_state(hass, CHARGER, "on")

    # No discharge, charging free, and both rates in force.
    assert written == [
        (124, [("OutWRte", 0.0), ("InWRte", 100.0)]),
        (124, [("StorCtl_Mod", 3)]),
    ]


# ---------------------------------------------------------------------------
# Charge the battery from the grid while the price is low
# ---------------------------------------------------------------------------


async def _charge(hass, mode="auto", price="0.30", soc="40", **inputs):
    battery = Battery(hass, mode)
    hass.states.async_set(PRICE, price, {"unit_of_measurement": "EUR/kWh"})
    if soc is not None:
        hass.states.async_set(SOC, soc, {"unit_of_measurement": "%"})
        inputs = {"soc": SOC, **inputs}
    await start_automation(
        hass,
        "charge_battery_at_low_price",
        {"battery_mode": MODE, "price": PRICE, "threshold": 0.10, **inputs},
    )
    return battery


async def test_the_battery_charges_while_the_price_is_low(hass):
    battery = await _charge(hass)

    await set_state(hass, PRICE, "0.08")
    assert battery.selected == ["charge_from_grid"]

    await set_state(hass, PRICE, "0.05")
    assert battery.selected == ["charge_from_grid"]

    await set_state(hass, PRICE, "0.12")
    assert battery.selected == ["charge_from_grid", "auto"]


async def test_charging_stops_at_the_target_and_resumes_below_the_margin(hass):
    battery = await _charge(hass, price="0.05", soc="88")
    await set_state(hass, SOC, "84")
    assert battery.selected == ["charge_from_grid"]

    await set_state(hass, SOC, "90")
    assert battery.selected == ["charge_from_grid", "auto"]

    # 86 % is inside the margin of 5 below the target of 90.
    await set_state(hass, SOC, "86")
    assert len(battery.selected) == 2

    await set_state(hass, SOC, "84.5")
    assert battery.selected == ["charge_from_grid", "auto", "charge_from_grid"]


async def test_a_full_battery_is_not_charged(hass):
    battery = await _charge(hass, soc="95")

    await set_state(hass, PRICE, "0.02")

    assert battery.selected == []


async def test_without_a_level_sensor_the_price_alone_decides(hass):
    battery = await _charge(hass, soc=None)

    await set_state(hass, PRICE, "0.02")
    await set_state(hass, PRICE, "0.20")

    assert battery.selected == ["charge_from_grid", "auto"]


async def test_a_mode_chosen_elsewhere_is_not_replaced(hass):
    battery = await _charge(hass, mode="block_discharging")

    await set_state(hass, PRICE, "0.02")

    assert battery.selected == []


async def test_a_charge_started_elsewhere_survives_a_price_update(hass):
    """A battery plan charging at a normal price is not ended by the next price."""
    battery = await _charge(hass, mode="charge_from_grid", price="0.30")

    await set_state(hass, PRICE, "0.28")

    assert battery.selected == []


async def test_an_hour_without_a_price_ends_the_charge(hass, freezer):
    battery = await _charge(hass)
    await set_state(hass, PRICE, "0.05")
    await set_state(hass, PRICE, "unavailable")

    await pass_time(hass, freezer, minutes=59)
    assert battery.selected == ["charge_from_grid"]

    await pass_time(hass, freezer, minutes=2)
    assert battery.selected == ["charge_from_grid", "auto"]


async def test_a_price_that_returns_high_after_a_gap_ends_the_charge(hass):
    battery = await _charge(hass)
    await set_state(hass, PRICE, "0.05")

    await set_state(hass, PRICE, "unavailable")
    await set_state(hass, PRICE, "0.25")

    assert battery.selected == ["charge_from_grid", "auto"]


async def test_the_charge_is_selected_again_before_the_inverter_drops_it(hass, freezer):
    battery = await _charge(hass, price="0.06", soc="50")
    await set_state(hass, PRICE, "0.05")
    assert battery.selected == ["charge_from_grid"]

    for _ in range(6):
        await pass_time(hass, freezer, minutes=1)
    # 12:04 and 12:07.
    assert battery.selected == ["charge_from_grid"] * 3

    await set_state(hass, PRICE, "0.30")
    for _ in range(4):
        await pass_time(hass, freezer, minutes=1)
    assert battery.selected == ["charge_from_grid"] * 3 + ["auto"]


async def test_the_messages_name_the_price_and_the_level(hass):
    hass.states.async_set("notify.phone", "unknown")
    messages = RecordedAction(hass, "notify", "send_message")
    await _charge(hass, notify=["notify.phone"])

    await set_state(hass, PRICE, "0.05")
    await set_state(hass, PRICE, "0.20")

    assert [call["message"] for call in messages.calls] == [
        "The battery charges from the grid (0.05 EUR/kWh, battery at 40.0 %).",
        "The battery is back to automatic (0.2 EUR/kWh, battery at 40.0 %).",
    ]
