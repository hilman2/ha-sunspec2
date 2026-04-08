"""Tests for SunSpec integration."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2 import DOMAIN
from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.api import SunSpecApiClient

from .const import MOCK_CONFIG

TEST_CONFIG_ENTRY_ID = "77889900aa"
TEST_SERIAL_NO = "abc123"
# Sensor entity_ids in v0.8.1 onwards: ``has_entity_name = True``
# means the slug is built as ``sensor.<device-slug>_<entity-slug>``,
# where ``<device-slug>`` comes from the inverter's Md field (test
# fixture: "Test-1547-1" -> ``test_1547_1``) and ``<entity-slug>``
# is the per-point label without any group prefix. For prefixed
# installs the device-slug is replaced by the user prefix.
TEST_INVERTER_SENSOR_STATE_ENTITY_ID = "sensor.test_1547_1_operating_state"
TEST_INVERTER_SENSOR_POWER_ENTITY_ID = "sensor.test_1547_1_watts"
TEST_INVERTER_SENSOR_VAR_ID = "sensor.test_1547_1_var"
TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID = "sensor.test_1547_1_watthours"
TEST_INVERTER_MM_SENSOR_STATE_ENTITY_ID = "sensor.test_1547_1_1_operating_state"
TEST_INVERTER_MM_SENSOR_POWER_ENTITY_ID = "sensor.test_1547_1_1_active_power"
TEST_INVERTER_RG_SENSOR_INCLX_ENTITY_ID = "sensor.test_1547_1_incl_2_x_axis_inclination"
TEST_INVERTER_SENSOR_DC_ENTITY_ID = "sensor.test_1547_1_module_0_dc_current"
TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID = "sensor.test_module_0_dc_current"


def create_mock_sunspec_client(hass: HomeAssistant):
    """Create a mock modubs client"""
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    return api


def create_mock_sunspec_config_entry(
    hass: HomeAssistant,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> ConfigEntry:
    """Add a test config entry."""
    config_entry: MockConfigEntry = MockConfigEntry(
        entry_id=TEST_CONFIG_ENTRY_ID,
        domain=DOMAIN,
        data=data or MOCK_CONFIG,
        title="",
        options=options or {},
    )
    config_entry.add_to_hass(hass)
    return config_entry


async def setup_mock_sunspec_config_entry(
    hass: HomeAssistant,
    data: dict[str, Any] | None = None,
    config_entry: ConfigEntry | None = None,
    client: Mock | None = None,
) -> ConfigEntry:
    """Add a mock sunspec config entry to hass."""
    config_entry = config_entry or create_mock_sunspec_config_entry(hass, data)
    client = client or create_mock_sunspec_client(hass)

    with patch(
        "custom_components.sunspec2.SunSpecApiClient",
        return_value=client,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    return config_entry


def register_test_entity(
    hass: HomeAssistant,
    platform: str,
    entity_id: str,
    key: str,
    model_id: str,
    model_index: int,
) -> None:
    """Register a test entity."""

    unique_id = get_sunspec_unique_id(TEST_CONFIG_ENTRY_ID, key, model_id, model_index)
    entity_id = entity_id.split(".")[1]

    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        platform,
        DOMAIN,
        unique_id,
        suggested_object_id=entity_id,
        disabled_by=None,
    )


def get_sunspec_device_identifier(serial_no: str) -> tuple[str, str]:
    """Get the identifiers for a SunSpec device."""
    return (DOMAIN, serial_no)


class MockSunSpecDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass, models) -> None:
        """Initialize."""
        self.api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
        self.option_model_filter = set(map(lambda m: int(m), models))
        # Pre-populate the same cache the real coordinator builds during
        # its first successful update cycle. The options-flow form reads
        # this attribute to render its model multi-select; without it the
        # form would fall back to api.known_models() which always returns
        # [] for this stub (no live client) and the test schema would
        # validate against an empty allowed-values set.
        self.detected_models = set(map(lambda m: int(m), models))

    async def _async_update_data(self):
        """Update data via library."""
