"""Tests for the SunSpec 2 diagnostics platform."""

from unittest.mock import Mock
from unittest.mock import patch

from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.diagnostics import async_get_config_entry_diagnostics

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


async def test_diagnostics_consecutive_failures_starts_zero(hass, sunspec_client_mock):
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
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass, capture_enabled=True)

    fake_client = Mock()
    fake_client.read.return_value = b"\x12\x34\x56\x78"
    fake_client.is_connected.return_value = True

    with (
        patch(
            "sunspec2.modbus.client.SunSpecModbusClientDeviceTCP",
            return_value=fake_client,
        ),
        patch.object(SunSpecApiClient, "check_port", return_value=True),
    ):
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


async def test_close_calls_disconnect_on_active_client(hass):
    """api.close() must call disconnect() on the instance-scoped client.

    Regression: pysunspec2's SunSpecModbusClientDevice.close() is a no-op
    stub. Our api.close() routes to client.disconnect() (which is real)
    so the TCP socket is actually torn down. KACO Powador only allows
    one Modbus TCP slot at a time, so a leftover socket would block the
    next reconnect after a config-entry reload.
    """
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)
    fake_active_client = Mock()
    api._client = fake_active_client

    api.close()

    fake_active_client.disconnect.assert_called_once_with()
    # Reference is dropped so the next get_client() builds a fresh client.
    assert api._client is None


async def test_close_is_a_noop_when_no_client(hass):
    """If no client has been built yet, close() must not crash."""
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)
    assert api._client is None

    api.close()  # must not raise

    assert api._client is None


async def test_close_swallows_disconnect_errors(hass):
    """Cleanup must not propagate exceptions from the underlying client."""
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)
    fake_active_client = Mock()
    fake_active_client.disconnect.side_effect = OSError("socket already gone")
    api._client = fake_active_client

    api.close()  # must not raise

    fake_active_client.disconnect.assert_called_once_with()
    # Reference is still dropped even if disconnect() blew up.
    assert api._client is None


async def test_known_models_returns_empty_when_no_client(hass):
    """known_models must return [] before any client is built (Phase 4)."""
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)

    assert api.known_models() == []


async def test_known_models_returns_int_keys_only(hass):
    """known_models filters out non-integer keys from the pysunspec2 client."""
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)
    fake_client = Mock()
    fake_client.models = {1: "common", 103: "inverter", "common": "alias"}
    api._client = fake_client

    assert api.known_models() == [1, 103]
