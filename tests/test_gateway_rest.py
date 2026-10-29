"""The rest of a gateway that left a request unanswered, and the longer wait for an answer.

An APsystems ECU answers one request at a time and goes on working on one
the client gave up on (#160). After such a request nothing goes to it, from
any entry behind it, until its next collection round has passed.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2 import SunSpecDataUpdateCoordinator
from custom_components.sunspec2 import gateway_rest_until
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.api import is_unanswered
from custom_components.sunspec2.connection import SharedConnection
from custom_components.sunspec2.const import CONF_UNIT_ID
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.errors import TransientError
from custom_components.sunspec2.errors import TransportError
from custom_components.sunspec2.pysunspec2.modbus.modbus import ModbusClientTimeout
from custom_components.sunspec2.vendors.apsystems import APSYSTEMS

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG
from .const import MOCK_CONFIG_PREFIX
from .fake_unit import FakeUnit
from .test_connection import PARAMS
from .test_connection import _entry as _connection_entry

NOW = datetime(2026, 10, 29, 10, 2, 30, tzinfo=UTC)


def _unanswered() -> TransientError:
    """A model read that timed out, as api.py hands it to the coordinator."""
    err = TransientError("Modbus read timeout for model 103")
    err.__cause__ = ModbusClientTimeout("no answer")
    return err


async def _ecu_entry(hass, entry=None, unit_id=1):
    entry = entry or create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    client = SunSpecApiClient(host="test", port=123, unit_id=unit_id, hass=hass)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)
    return entry


@pytest.mark.parametrize(
    ("now", "until"),
    [
        # The round at 10:05 is more than a minute away: a minute after it.
        ("10:02:30", "10:06:00"),
        # Less than a minute to 10:05: the round after it.
        ("10:04:30", "10:11:00"),
        ("10:05:00", "10:11:00"),
    ],
)
def test_the_rest_ends_a_minute_into_the_next_round(now, until):
    day = "2026-10-29T{}+00:00"
    assert gateway_rest_until(datetime.fromisoformat(day.format(now)), 300) == (
        datetime.fromisoformat(day.format(until))
    )


async def test_an_unanswered_request_rests_the_gateway_instead_of_retrying(
    hass, sunspec_apsystems_client_mock, freezer, monkeypatch
):
    monkeypatch.setattr("custom_components.sunspec2.INTERVAL_RETRY_DELAY_SECONDS", 0)
    freezer.move_to(NOW)
    entry = await _ecu_entry(hass)
    coordinator = entry.runtime_data

    with patch.object(coordinator.api, "async_get_data", side_effect=_unanswered()) as get_data:
        await coordinator.async_refresh()
    assert get_data.call_count == 1
    assert coordinator.consecutive_failed_cycles == 1
    assert coordinator.gateway_rest_end() == datetime(2026, 10, 29, 10, 6, tzinfo=UTC)

    # A poll during the rest sends nothing and counts as a failed cycle.
    with patch.object(coordinator.api, "async_get_data") as get_data:
        await coordinator.async_refresh()
    get_data.assert_not_called()
    assert coordinator.consecutive_failed_cycles == 2
    assert not coordinator.last_update_success

    freezer.move_to(datetime(2026, 10, 29, 10, 6, 1, tzinfo=UTC))
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.consecutive_failed_cycles == 0
    assert coordinator.gateway_rest_end() is None


async def test_the_rest_holds_every_entry_behind_the_gateway(
    hass, sunspec_apsystems_client_mock, freezer, monkeypatch
):
    monkeypatch.setattr("custom_components.sunspec2.INTERVAL_RETRY_DELAY_SECONDS", 0)
    freezer.move_to(NOW)
    first = await _ecu_entry(hass)
    second_entry = MockConfigEntry(
        domain=DOMAIN, data={**MOCK_CONFIG, CONF_UNIT_ID: 2}, entry_id="second_micro"
    )
    second_entry.add_to_hass(hass)
    second = await _ecu_entry(hass, second_entry, unit_id=2)

    with patch.object(first.runtime_data.api, "async_get_data", side_effect=_unanswered()):
        await first.runtime_data.async_refresh()

    with patch.object(second.runtime_data.api, "async_get_data") as get_data:
        await second.runtime_data.async_refresh()
    get_data.assert_not_called()
    assert second.runtime_data.consecutive_failed_cycles == 1


async def test_a_device_without_rounds_still_retries_an_unanswered_request(
    hass, sunspec_client_mock, monkeypatch
):
    monkeypatch.setattr("custom_components.sunspec2.INTERVAL_RETRY_DELAY_SECONDS", 0)
    entry = await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG_PREFIX)
    coordinator = entry.runtime_data

    with patch.object(coordinator.api, "async_get_data", side_effect=_unanswered()) as get_data:
        await coordinator.async_refresh()

    assert get_data.call_count == 2
    assert coordinator.gateway_rest_end() is None


async def test_a_refused_connection_does_not_rest_the_gateway(
    hass, sunspec_apsystems_client_mock, monkeypatch
):
    """Only a request that went out and got no answer keeps the ECU busy."""
    monkeypatch.setattr("custom_components.sunspec2.INTERVAL_RETRY_DELAY_SECONDS", 0)
    entry = await _ecu_entry(hass)
    coordinator = entry.runtime_data

    with patch.object(
        coordinator.api, "async_get_data", side_effect=TransportError("refused")
    ) as get_data:
        await coordinator.async_refresh()

    assert get_data.call_count == 2
    assert coordinator.gateway_rest_end() is None


async def test_a_write_during_the_rest_is_refused_without_touching_the_device(
    hass, sunspec_apsystems_controls_client_mock
):
    entry = await _ecu_entry(hass)
    coordinator = entry.runtime_data
    SunSpecDataUpdateCoordinator._GATEWAY_RESTS[("test_host", 123)] = dt_util.utcnow() + (
        timedelta(minutes=3)
    )

    with (
        patch.object(coordinator.api, "async_write_points") as write,
        pytest.raises(TransientError, match="try again after"),
    ):
        await coordinator.async_write_points_locked(123, [("WMaxLimPct", 50)])

    write.assert_not_called()
    assert not coordinator._gateway_lock.locked()


async def test_an_unanswered_write_rests_the_gateway(hass, sunspec_apsystems_controls_client_mock):
    entry = await _ecu_entry(hass)
    coordinator = entry.runtime_data

    with (
        patch.object(coordinator.api, "async_write_points", side_effect=_unanswered()),
        pytest.raises(TransientError),
    ):
        await coordinator.async_write_points_locked(123, [("WMaxLimPct", 50)])

    assert coordinator.gateway_rest_end() is not None


async def test_the_ecu_gets_a_minute_to_answer(hass, sunspec_apsystems_client_mock):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    client = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    with patch.object(
        client, "async_require_timeout", AsyncMock(wraps=client.async_require_timeout)
    ) as require:
        await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)

    require.assert_awaited_with(60.0)


async def test_a_restart_knows_the_vendor_before_the_first_request(
    hass, sunspec_apsystems_client_mock
):
    """The identity stored with the layout names the vendor, so the first cycle is already gentle."""
    entry = await _ecu_entry(hass)
    coordinator = entry.runtime_data
    coordinator.vendor = None
    coordinator._persisted_identity = {"Mn": "APsystems", "Md": "DS3"}

    assert coordinator._link_profile() is APSYSTEMS


async def test_a_raised_timeout_drops_a_live_link_once(hass):
    unit = FakeUnit()
    with patch("custom_components.sunspec2.connection.async_get_unit", return_value=unit):
        connection = SharedConnection(hass, PARAMS, _connection_entry(hass))
        connection.for_unit(1)

        await connection.async_require_timeout(1, 60.0)
        await connection.async_require_timeout(1, 60.0)
        connection.withdraw_timeout(1)

    assert unit.timeouts == [60.0, None]
    assert unit.disconnects == 1


def test_a_timeout_anywhere_in_the_chain_is_unanswered():
    try:
        try:
            raise ModbusClientTimeout("no answer")
        except ModbusClientTimeout:
            raise TransportError("connect failed") from None
    except TransportError as err:
        wrapped = TransientError("cycle failed")
        wrapped.__context__ = err
        # ``from None`` hides the timeout from __cause__, not from __context__.
        assert is_unanswered(err)
        assert is_unanswered(wrapped)
    assert not is_unanswered(TransportError("refused"))


async def test_a_rest_that_starts_during_the_retry_wait_stops_the_retry(
    hass, sunspec_apsystems_client_mock, monkeypatch
):
    monkeypatch.setattr("custom_components.sunspec2.INTERVAL_RETRY_DELAY_SECONDS", 0)
    entry = await _ecu_entry(hass)
    coordinator = entry.runtime_data

    def refused_while_another_entry_goes_unanswered(model_id):
        SunSpecDataUpdateCoordinator._GATEWAY_RESTS[("test_host", 123)] = (
            dt_util.utcnow() + timedelta(minutes=3)
        )
        raise TransportError("refused")

    with patch.object(
        coordinator.api,
        "async_get_data",
        side_effect=refused_while_another_entry_goes_unanswered,
    ) as get_data:
        await coordinator.async_refresh()

    assert get_data.call_count == 1
    assert coordinator.consecutive_failed_cycles == 1


async def test_after_a_restart_the_first_request_already_gets_a_minute(
    hass, sunspec_apsystems_client_mock
):
    entry = await _ecu_entry(hass)
    coordinator = entry.runtime_data
    # What a restart starts from: the vendor known only from the stored identity.
    coordinator.vendor = None
    coordinator._persisted_identity = {"Mn": "APsystems", "Md": "DS3"}
    coordinator.api._request_timeout = None
    calls = []
    real_get_models = coordinator.api.async_get_models

    async def require(seconds):
        calls.append(("timeout", seconds))

    async def get_models():
        calls.append(("request", None))
        return await real_get_models()

    with (
        patch.object(coordinator.api, "async_require_timeout", side_effect=require),
        patch.object(coordinator.api, "async_get_models", side_effect=get_models),
    ):
        await coordinator.async_refresh()

    assert calls[:2] == [("timeout", 60.0), ("request", None)]


async def test_a_second_unit_on_the_same_gateway_does_not_drop_the_link_again(hass):
    first, second = FakeUnit(unit_id=1), FakeUnit(unit_id=2)
    with patch("custom_components.sunspec2.connection.async_get_unit", side_effect=[first, second]):
        one = SharedConnection(hass, PARAMS, _connection_entry(hass))
        other = SharedConnection(hass, PARAMS, _connection_entry(hass))
        one.for_unit(1)
        other.for_unit(2)

        await one.async_require_timeout(1, 60.0)
        await other.async_require_timeout(2, 60.0)

    assert first.disconnects == 1
    assert second.disconnects == 0
    assert second.timeouts == [60.0]
