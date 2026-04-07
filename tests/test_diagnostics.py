"""Tests for the SunSpec 2 diagnostics platform."""

from unittest.mock import Mock, patch

from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.diagnostics import (
    async_get_config_entry_diagnostics,
)

from . import setup_mock_sunspec_config_entry


async def test_diagnostics_basic_shape(hass, sunspec_client_mock):
    """Diagnostics dump must have the expected top-level shape."""
    entry = await setup_mock_sunspec_config_entry(hass)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert set(diag.keys()) == {
        "config",
        "options",
        "scanned_models",
        "latest_values",
        "recent_errors",
        "consecutive_failures",
        "raw_captures",
        "versions",
    }


async def test_diagnostics_redacts_host(hass, sunspec_client_mock):
    """The host field must be redacted; port and unit_id stay visible."""
    entry = await setup_mock_sunspec_config_entry(hass)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["config"]["host"] == "**REDACTED**"
    assert diag["config"]["port"] == 123
    assert diag["config"]["unit_id"] == 1


async def test_diagnostics_includes_versions(hass, sunspec_client_mock):
    """Versions block must include HA, pysunspec2 and our integration."""
    entry = await setup_mock_sunspec_config_entry(hass)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    versions = diag["versions"]
    assert versions["pysunspec2"] == "1.3.3"
    assert versions["sunspec2_integration"]
    assert versions["homeassistant"]


async def test_diagnostics_includes_scanned_models(hass, sunspec_client_mock):
    """The scanned_models list must contain at least the configured models."""
    entry = await setup_mock_sunspec_config_entry(hass)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    model_ids = {m["model_id"] for m in diag["scanned_models"]}
    # MOCK_CONFIG enables 103 and 160; either model name from
    # pysunspec2 1.3.3 will be present in the inverter.json fixture.
    assert 103 in model_ids
    assert "103" in diag["latest_values"]
    assert len(diag["latest_values"]["103"]) > 0


async def test_diagnostics_recent_errors_starts_empty(hass, sunspec_client_mock):
    """A clean setup has all four per-category buffers empty."""
    entry = await setup_mock_sunspec_config_entry(hass)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["recent_errors"] == {
        "transport": [],
        "protocol": [],
        "device": [],
        "transient": [],
    }


async def test_diagnostics_consecutive_failures_starts_zero(
    hass, sunspec_client_mock
):
    """A clean setup has all four consecutive_failures counters at zero."""
    entry = await setup_mock_sunspec_config_entry(hass)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["consecutive_failures"] == {
        "transport": 0,
        "protocol": 0,
        "device": 0,
        "transient": 0,
    }


async def test_diagnostics_raw_captures_starts_empty(hass, sunspec_client_mock):
    """Without capture enabled, raw_captures is an empty list."""
    entry = await setup_mock_sunspec_config_entry(hass)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["raw_captures"] == []


async def test_capture_disabled_by_default(hass):
    """A SunSpecApiClient created without capture_enabled does not wrap reads."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    assert api._capture_enabled is False
    assert api._captured_reads == []


async def test_capture_wraps_client_read(hass):
    """When capture_enabled, modbus_connect wraps client.read so every call lands in _captured_reads."""
    SunSpecApiClient.CLIENT_CACHE = {}
    api = SunSpecApiClient(
        host="test", port=123, unit_id=1, hass=hass, capture_enabled=True
    )

    fake_client = Mock()
    fake_client.read.return_value = b"\x12\x34\x56\x78"
    fake_client.is_connected.return_value = True

    with patch(
        "sunspec2.modbus.client.SunSpecModbusClientDeviceTCP",
        return_value=fake_client,
    ), patch.object(SunSpecApiClient, "check_port", return_value=True):
        client = api.modbus_connect()

    # The wrap replaced client.read with our capturing version. The Mock's
    # original read is still called underneath, so the bytes propagate.
    result = client.read(40000, 3)

    assert result == b"\x12\x34\x56\x78"
    assert len(api._captured_reads) == 1
    captured = api._captured_reads[0]
    assert captured["addr"] == 40000
    assert captured["count"] == 3
    assert captured["hex"] == "12345678"
    assert "ts" in captured

    SunSpecApiClient.CLIENT_CACHE = {}


async def test_capture_appears_in_diagnostics_dump(hass, sunspec_client_mock):
    """When the api has captured reads, they show up in the diagnostics dump."""
    entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = hass.data["sunspec2"][entry.entry_id]
    # Inject a synthetic captured read so we don't need to plumb through a
    # real wrap path in the file-client mock fixture.
    coordinator.api._captured_reads.append(
        {"ts": 1700000000.0, "addr": 40000, "count": 3, "hex": "12345678"}
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert len(diag["raw_captures"]) == 1
    assert diag["raw_captures"][0]["addr"] == 40000
    assert diag["raw_captures"][0]["hex"] == "12345678"


async def test_close_calls_disconnect_on_cached_client(hass):
    """api.close() must call disconnect() on the cached client.

    Regression: pysunspec2's SunSpecModbusClientDevice.close() is a no-op
    stub. Our api.close() previously called client.close() which therefore
    did nothing, leaving the TCP socket open across update cycles and
    across config entry reloads. KACO Powador only allows one TCP
    connection per slave at a time, so a leftover socket would block the
    next reconnect.
    """
    SunSpecApiClient.CLIENT_CACHE = {}
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)
    fake_cached_client = Mock()
    SunSpecApiClient.CLIENT_CACHE[api._client_key] = fake_cached_client

    api.close()

    fake_cached_client.disconnect.assert_called_once_with()
    SunSpecApiClient.CLIENT_CACHE = {}


async def test_close_is_a_noop_when_cache_empty(hass):
    """If nothing is cached, close() must not crash and must not call out."""
    SunSpecApiClient.CLIENT_CACHE = {}
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)

    api.close()  # must not raise


async def test_close_swallows_disconnect_errors(hass):
    """Cleanup must not propagate exceptions from the underlying client."""
    SunSpecApiClient.CLIENT_CACHE = {}
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)
    fake_cached_client = Mock()
    fake_cached_client.disconnect.side_effect = OSError("socket already gone")
    SunSpecApiClient.CLIENT_CACHE[api._client_key] = fake_cached_client

    api.close()  # must not raise

    fake_cached_client.disconnect.assert_called_once_with()
    SunSpecApiClient.CLIENT_CACHE = {}
