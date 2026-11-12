"""The entities over the Fronius web interface, where the interface has little to say or refuses.

The inverter is played by Home Assistant's aiohttp mocker, as in
tests/test_fronius_web.py, whose endpoints and helpers these tests use.
An endpoint registered before ``_mock_web`` answers in place of the
one ``_mock_web`` registers.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.sunspec2.fronius_web_entities import MeterLocationSensor
from custom_components.sunspec2.fronius_web_entities import ModbusResetButton
from custom_components.sunspec2.fronius_web_entities import WebSwitch

from .test_fronius_web import BASE
from .test_fronius_web import _entities
from .test_fronius_web import _mock_web
from .test_fronius_web import _posts
from .test_fronius_web import _web_entry
from .test_fronius_web import challenged


def _switches(hass):
    return {switch.translation_key: switch for switch in _entities(hass, "switch", WebSwitch)}


async def test_without_a_smart_meter_the_location_is_unknown(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    aioclient_mock.get(
        f"{BASE}/api/components/PowerMeter/readable",
        side_effect=challenged({"Body": {"Data": {}}}),
    )
    _mock_web(aioclient_mock)
    await _web_entry(hass)

    location = _entities(hass, "sensor", MeterLocationSensor)[0]
    assert location.native_value is None
    assert location.extra_state_attributes == {"meters": []}
    assert hass.states.get(location.entity_id).state == "unknown"


async def test_the_ac_charging_switch_takes_grid_charging_down_with_it(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    """Without AC charging there is no grid charging; switching it on promises nothing about it."""
    _mock_web(aioclient_mock)
    await _web_entry(hass)
    ac_charging = _switches(hass)["battery_charge_from_ac"]

    await ac_charging.async_turn_on()
    assert _posts(aioclient_mock, "/api/config/batteries")[-1] == {"HYB_BM_CHARGEFROMAC": True}

    await ac_charging.async_turn_off()
    assert _posts(aioclient_mock, "/api/config/batteries")[-1] == {
        "HYB_EVU_CHARGEFROMGRID": False,
        "HYB_BM_CHARGEFROMAC": False,
    }


async def test_a_flag_the_web_interface_does_not_take_is_an_error_for_the_user(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    aioclient_mock.post(f"{BASE}/api/config/batteries", side_effect=challenged({}, status=500))
    _mock_web(aioclient_mock)
    await _web_entry(hass)

    with pytest.raises(HomeAssistantError) as refused:
        await _switches(hass)["battery_charge_from_grid"].async_turn_on()

    assert refused.value.translation_key == "web_refused"
    assert refused.value.translation_placeholders == {"error": "/api/config/batteries: HTTP 500"}


async def test_a_reset_the_web_interface_does_not_take_is_an_error_for_the_user(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    aioclient_mock.post(f"{BASE}/api/commands/ModbusReset", side_effect=challenged({}, status=503))
    _mock_web(aioclient_mock)
    await _web_entry(hass)

    with pytest.raises(HomeAssistantError) as refused:
        await _entities(hass, "button", ModbusResetButton)[0].async_press()

    assert refused.value.translation_key == "web_refused"
    assert refused.value.translation_placeholders == {
        "error": "/api/commands/ModbusReset: HTTP 503"
    }
