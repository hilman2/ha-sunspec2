"""Tests for the SunSpec 2 diagnostics platform."""

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
