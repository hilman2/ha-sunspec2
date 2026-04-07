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
    """A clean setup has an empty recent_errors list."""
    entry = await setup_mock_sunspec_config_entry(hass)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["recent_errors"] == []


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


async def test_invalidate_cache_drops_key(hass):
    """invalidate_cache pops exactly the client_key for this api instance."""
    SunSpecApiClient.CLIENT_CACHE = {}
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)
    SunSpecApiClient.CLIENT_CACHE[api._client_key] = "fake_cached_client"
    other_key = "other:1502:9"
    SunSpecApiClient.CLIENT_CACHE[other_key] = "other_cached_client"

    api.invalidate_cache()

    assert api._client_key not in SunSpecApiClient.CLIENT_CACHE
    # Other entries are untouched
    assert SunSpecApiClient.CLIENT_CACHE[other_key] == "other_cached_client"
    SunSpecApiClient.CLIENT_CACHE = {}


async def test_capture_takes_effect_after_reload_via_invalidate(hass):
    """Regression: a config-entry reload that flips capture_enabled must
    actually rebuild the underlying client. Without invalidate_cache, the
    old non-wrapped client survives in CLIENT_CACHE and the toggle is a
    no-op until the next HA restart.
    """
    SunSpecApiClient.CLIENT_CACHE = {}

    # First lifecycle: capture disabled. modbus_connect populates the cache
    # with a non-wrapped client.
    api1 = SunSpecApiClient(
        host="test", port=502, unit_id=1, hass=hass, capture_enabled=False
    )
    fake_v1 = Mock()
    fake_v1.read.return_value = b"\x00\x01"
    fake_v1.is_connected.return_value = True
    with patch(
        "sunspec2.modbus.client.SunSpecModbusClientDeviceTCP", return_value=fake_v1
    ), patch.object(SunSpecApiClient, "check_port", return_value=True):
        api1.get_client()
    assert api1._client_key in SunSpecApiClient.CLIENT_CACHE

    # Simulate the unload step of a reload.
    api1.invalidate_cache()
    assert api1._client_key not in SunSpecApiClient.CLIENT_CACHE

    # Second lifecycle: capture enabled. get_client must build a fresh
    # wrapped client, not return the previous fake_v1.
    api2 = SunSpecApiClient(
        host="test", port=502, unit_id=1, hass=hass, capture_enabled=True
    )
    fake_v2 = Mock()
    fake_v2.read.return_value = b"\xab\xcd"
    fake_v2.is_connected.return_value = True
    with patch(
        "sunspec2.modbus.client.SunSpecModbusClientDeviceTCP", return_value=fake_v2
    ), patch.object(SunSpecApiClient, "check_port", return_value=True):
        client = api2.get_client()

    # The wrap is in place: calling client.read populates _captured_reads.
    client.read(40000, 1)
    assert len(api2._captured_reads) == 1
    assert api2._captured_reads[0]["hex"] == "abcd"

    SunSpecApiClient.CLIENT_CACHE = {}
