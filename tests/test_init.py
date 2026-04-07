"""Test SunSpec setup process."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2 import SunSpecDataUpdateCoordinator
from custom_components.sunspec2 import async_setup_entry
from custom_components.sunspec2.const import (
    CONF_CAPTURE_RAW,
    CONF_ENABLED_MODELS,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.sunspec2.migration import CJNE_DOMAIN

from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG


def set_entry_setup_in_progress(hass, config_entry: MockConfigEntry) -> None:
    """Mirror the state Home Assistant uses while invoking async_setup_entry directly."""
    config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)


# We can pass fixtures as defined in conftest.py to tell pytest to use the fixture
# for a given test. We can also leverage fixtures and mocks that are available in
# Home Assistant using the pytest_homeassistant_custom_component plugin.
# Assertions allow you to verify that the return value of whatever is on the left
# side of the assertion matches with the right side.
async def test_setup_unload_and_reload_entry(hass, sunspec_client_mock):
    """Test entry setup and unload."""
    # Create a mock entry so we don't have to go through config flow
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    config_entry.add_to_hass(hass)

    # Use the config entries manager so entry state transitions match real setup.
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert (
        type(hass.data[DOMAIN][config_entry.entry_id]) is SunSpecDataUpdateCoordinator
    )

    # Reload the entry and assert that the data from above is still there.
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert (
        type(hass.data[DOMAIN][config_entry.entry_id]) is SunSpecDataUpdateCoordinator
    )

    # Unload the entry and verify that the data has been removed.
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.entry_id not in hass.data[DOMAIN]


async def test_options_update_triggers_clean_reload(hass, sunspec_client_mock):
    """Updating entry options must trigger a clean reload through HA's
    state machine, not crash with ConfigEntryError on first_refresh.

    Regression for the Phase-4 hot-reload bug. The cjne pattern was:

        async def async_reload_entry(hass, entry):
            await async_unload_entry(hass, entry)
            await async_setup_entry(hass, entry)

    This stopped working in HA 2026.x because async_setup_entry calls
    coordinator.async_config_entry_first_refresh(), which now strictly
    requires the entry state to be SETUP_IN_PROGRESS. Calling
    async_setup_entry directly from the update listener leaves the entry
    in LOADED state and first_refresh raises ConfigEntryError. The
    user-visible symptom was: toggle ANY option in the options flow ->
    sensors stay 'unavailable' until HA is restarted.

    test_setup_unload_and_reload_entry above does NOT catch this bug
    because it calls hass.config_entries.async_reload() directly, which
    drives the state machine correctly. THIS test goes through the
    update_listener via async_update_entry, the same code path the user
    hits when they save the options form.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, entry_id="test_reload_via_options"
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state == ConfigEntryState.LOADED

    # The user toggling capture_raw_registers in the options flow is
    # internally an async_update_entry call. That fires the update
    # listener registered by the coordinator (async_reload_entry), which
    # in Phase 4 dispatches to hass.config_entries.async_reload() instead
    # of doing unload+setup by hand.
    hass.config_entries.async_update_entry(
        config_entry,
        options={
            CONF_CAPTURE_RAW: True,
            CONF_ENABLED_MODELS: [103, 160],
            CONF_SCAN_INTERVAL: 10,
        },
    )
    await hass.async_block_till_done()

    # If the bug were back, the entry would be in SETUP_ERROR or some
    # other failure state and there would be no coordinator in hass.data.
    assert config_entry.state == ConfigEntryState.LOADED
    assert config_entry.entry_id in hass.data[DOMAIN]
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    assert isinstance(coordinator, SunSpecDataUpdateCoordinator)
    # The new (post-reload) coordinator picked up the new option.
    assert coordinator.api._capture_enabled is True


async def test_setup_runs_cjne_migration_when_entries_present(
    hass, sunspec_client_mock
):
    """async_setup_entry calls the cjne migration helper.

    Phase 5 integration test: pre-populate the entity registry with an
    orphan cjne entity matching our config, then run our normal setup,
    and assert the entity has been retargeted to sunspec2 after setup.
    Verifies that the migration helper is wired into the setup path.
    """
    # Stand up a fake cjne config entry + a registered entity in its
    # platform namespace, BEFORE our setup runs.
    cjne_entry = MockConfigEntry(
        domain=CJNE_DOMAIN,
        data={"host": "test_host", "port": 123, "unit_id": 1},
        entry_id="cjne_existing",
    )
    cjne_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    cjne_eid = registry.async_get_or_create(
        "sensor",
        CJNE_DOMAIN,
        "cjne_existing_W-103-0",
        suggested_object_id="inverter_three_phase_watts",
        config_entry=cjne_entry,
    ).entity_id

    # Now run our normal setup.
    our_entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, entry_id="ours_with_migration"
    )
    our_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(our_entry.entry_id)
    await hass.async_block_till_done()

    # The previously-cjne entity is now under our platform.
    re_after = registry.async_get(cjne_eid)
    assert re_after is not None
    assert re_after.platform == "sunspec2"
    assert re_after.config_entry_id == our_entry.entry_id
    assert re_after.unique_id == f"{our_entry.entry_id}_W-103-0"
    # entity_id (and therefore Recorder history) survived
    assert re_after.entity_id == cjne_eid


async def test_setup_entry_exception(hass, error_on_get_data):
    """Test ConfigEntryNotReady when API raises an exception during entry setup."""
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    config_entry.add_to_hass(hass)

    # In this case we are testing the condition where async_setup_entry raises
    # ConfigEntryNotReady using the `error_on_get_data` fixture which simulates
    # an error.
    set_entry_setup_in_progress(hass, config_entry)
    with pytest.raises(ConfigEntryNotReady):
        assert await async_setup_entry(hass, config_entry)


async def test_fetch_data_timeout(hass, timeout_error_on_get_data):
    """Test ConfigEntryNotReady when API raises an exception during entry setup."""
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    config_entry.add_to_hass(hass)

    # In this case we are testing the condition where async_setup_entry raises
    # ConfigEntryNotReady using the `error_on_get_data` fixture which simulates
    # an error.
    set_entry_setup_in_progress(hass, config_entry)
    with pytest.raises(ConfigEntryNotReady):
        assert await async_setup_entry(hass, config_entry)


async def test_fetch_data_connect_error(hass, connect_error_on_get_data):
    """Test ConfigEntryNotReady when API raises an exception during entry setup."""
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    config_entry.add_to_hass(hass)

    # In this case we are testing the condition where async_setup_entry raises
    # ConfigEntryNotReady using the `error_on_get_data` fixture which simulates
    # an error.
    set_entry_setup_in_progress(hass, config_entry)
    with pytest.raises(ConfigEntryNotReady):
        assert await async_setup_entry(hass, config_entry)


async def test_client_reconnect(hass, sunspec_client_mock_not_connected) -> None:
    await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG)


async def test_migrate_entry_from_v1_to_v2_with_slave_id(hass):
    """Test migration from version 1 to version 2 with slave_id key."""
    from custom_components.sunspec2 import async_migrate_entry

    # Create a version 1 config entry with slave_id
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "192.168.1.100",
            "port": 502,
            "slave_id": 5,
            "models_enabled": [103, 160],
            "scan_interval": 30,
        },
        entry_id="test_migration",
        version=1,
    )
    config_entry.add_to_hass(hass)

    # Run the migration
    result = await async_migrate_entry(hass, config_entry)

    # Verify migration was successful
    assert result is True
    assert config_entry.version == 2
    assert "unit_id" in config_entry.data
    assert config_entry.data["unit_id"] == 5
    assert "slave_id" not in config_entry.data
    assert config_entry.data["host"] == "192.168.1.100"
    assert config_entry.data["port"] == 502


async def test_migrate_entry_from_v1_to_v2_already_has_unit_id(hass):
    """Test migration from version 1 to version 2 when unit_id already exists."""
    from custom_components.sunspec2 import async_migrate_entry

    # Create a version 1 config entry that already has unit_id (edge case)
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "192.168.1.100",
            "port": 502,
            "unit_id": 3,
            "models_enabled": [103, 160],
            "scan_interval": 30,
        },
        entry_id="test_migration_already_migrated",
        version=1,
    )
    config_entry.add_to_hass(hass)

    # Run the migration
    result = await async_migrate_entry(hass, config_entry)

    # Verify migration was successful
    assert result is True
    assert config_entry.version == 2
    assert "unit_id" in config_entry.data
    assert config_entry.data["unit_id"] == 3
    assert "slave_id" not in config_entry.data


async def test_migrate_entry_from_v1_to_v2_with_both_keys(hass):
    """Test migration when both slave_id and unit_id exist (prefer unit_id)."""
    from custom_components.sunspec2 import async_migrate_entry

    # Create a version 1 config entry with both keys (edge case)
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "192.168.1.100",
            "port": 502,
            "slave_id": 5,
            "unit_id": 3,
            "models_enabled": [103, 160],
            "scan_interval": 30,
        },
        entry_id="test_migration_both_keys",
        version=1,
    )
    config_entry.add_to_hass(hass)

    # Run the migration
    result = await async_migrate_entry(hass, config_entry)

    # Verify migration was successful and unit_id was preserved
    assert result is True
    assert config_entry.version == 2
    assert "unit_id" in config_entry.data
    assert config_entry.data["unit_id"] == 3
    assert "slave_id" not in config_entry.data


async def test_migrate_entry_version_2_no_migration_needed(hass):
    """Test that version 2 entries don't get migrated."""
    from custom_components.sunspec2 import async_migrate_entry

    # Create a version 2 config entry (already migrated)
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "192.168.1.100",
            "port": 502,
            "unit_id": 5,
            "models_enabled": [103, 160],
            "scan_interval": 30,
        },
        entry_id="test_no_migration",
        version=2,
    )
    config_entry.add_to_hass(hass)

    # Run the migration
    result = await async_migrate_entry(hass, config_entry)

    # Verify no migration occurred
    assert result is True
    assert config_entry.version == 2
    assert "unit_id" in config_entry.data
    assert config_entry.data["unit_id"] == 5
