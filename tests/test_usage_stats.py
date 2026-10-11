"""The daily usage ping: what it sends, when it stays quiet, and that it never raises."""

import re
from unittest.mock import MagicMock
from unittest.mock import patch

import aiohttp
from homeassistant.helpers.storage import Store

from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.const import CONF_USAGE_STATS
from custom_components.sunspec2.const import VERSION
from custom_components.sunspec2.usage_stats import STATS_SITE_ID
from custom_components.sunspec2.usage_stats import STATS_URL
from custom_components.sunspec2.usage_stats import STORAGE_KEY
from custom_components.sunspec2.usage_stats import async_ping
from custom_components.sunspec2.usage_stats import async_start_usage_stats

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG


async def _entry(hass, **options):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG, options=options)
    client = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)
    return entry


def _store(hass) -> Store[dict[str, str]]:
    return Store(hass, 1, STORAGE_KEY)


def _query(aioclient_mock) -> dict[str, str]:
    (_method, url, _data, _headers) = aioclient_mock.mock_calls[0]
    return dict(url.query)


async def test_the_ping_carries_the_count_and_nothing_that_identifies_the_device(
    hass, sunspec_client_mock, aioclient_mock
):
    aioclient_mock.get(STATS_URL, status=204)
    await _entry(hass)

    await async_ping(hass)

    assert aioclient_mock.call_count == 1
    query = _query(aioclient_mock)
    assert query["idsite"] == str(STATS_SITE_ID)
    assert re.fullmatch(r"[0-9a-f]{16}", query["_id"])
    assert query["e_a"] == VERSION
    assert "entries=1" in query["url"]
    sent = " ".join(query.values())
    # The host of the mock entry is not in it.
    assert "test_host" not in sent


async def test_a_second_ping_within_the_day_is_skipped(hass, sunspec_client_mock, aioclient_mock):
    aioclient_mock.get(STATS_URL, status=204)
    await _entry(hass)

    await async_ping(hass)
    await async_ping(hass)

    assert aioclient_mock.call_count == 1


async def test_the_install_id_is_made_once_and_kept(hass, sunspec_client_mock, aioclient_mock):
    aioclient_mock.get(STATS_URL, status=204)
    await _entry(hass)

    await async_ping(hass)
    hass.data.pop("sunspec2_usage_stats")
    await async_ping(hass)

    first, second = (dict(call[1].query)["_id"] for call in aioclient_mock.mock_calls)
    assert first == second
    stored = await _store(hass).async_load()
    assert stored == {"install_id": first}


async def test_a_stored_id_that_does_not_fit_is_replaced(hass, sunspec_client_mock, aioclient_mock):
    aioclient_mock.get(STATS_URL, status=204)
    await _store(hass).async_save({"install_id": "not hex"})
    await _entry(hass)

    await async_ping(hass)

    assert re.fullmatch(r"[0-9a-f]{16}", _query(aioclient_mock)["_id"])


async def test_an_entry_that_turned_it_off_sends_nothing(hass, sunspec_client_mock, aioclient_mock):
    aioclient_mock.get(STATS_URL, status=204)
    await _entry(hass, **{CONF_USAGE_STATS: False})

    await async_ping(hass)

    assert aioclient_mock.call_count == 0


async def test_a_failing_server_does_not_raise(hass, sunspec_client_mock, aioclient_mock):
    aioclient_mock.get(STATS_URL, exc=aiohttp.ClientError("down"))
    await _entry(hass)

    await async_ping(hass)

    assert aioclient_mock.call_count == 1


async def test_the_timers_stop_when_the_entry_unloads(hass, sunspec_client_mock):
    entry = await _entry(hass)
    cancel_first, cancel_daily = MagicMock(), MagicMock()
    with (
        patch(
            "custom_components.sunspec2.usage_stats.async_call_later", return_value=cancel_first
        ) as call_later,
        patch(
            "custom_components.sunspec2.usage_stats.async_track_time_interval",
            return_value=cancel_daily,
        ) as track,
    ):
        async_start_usage_stats(hass, entry)
        assert call_later.call_count == 1
        assert track.call_count == 1

        await hass.config_entries.async_unload(entry.entry_id)

    cancel_first.assert_called_once()
    cancel_daily.assert_called_once()


async def test_no_timer_is_set_for_an_entry_that_turned_it_off(hass, sunspec_client_mock):
    entry = await _entry(hass, **{CONF_USAGE_STATS: False})
    with patch("custom_components.sunspec2.usage_stats.async_call_later") as call_later:
        async_start_usage_stats(hass, entry)
    call_later.assert_not_called()
