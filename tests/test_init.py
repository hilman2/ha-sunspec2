"""Test SunSpec setup process."""

from unittest.mock import AsyncMock as _AsyncMock
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2 import SunSpecDataUpdateCoordinator
from custom_components.sunspec2 import async_setup_entry
from custom_components.sunspec2.const import CONF_CAPTURE_RAW
from custom_components.sunspec2.const import CONF_ENABLED_MODELS
from custom_components.sunspec2.const import CONF_SCAN_DELAY
from custom_components.sunspec2.const import CONF_SCAN_INTERVAL
from custom_components.sunspec2.const import DEFAULT_MODELS
from custom_components.sunspec2.const import DEFAULT_SCAN_DELAY_SECONDS
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.const import MIN_SCAN_INTERVAL_SECONDS
from custom_components.sunspec2.const import STALE_DATA_TOLERANCE_CYCLES
from custom_components.sunspec2.const import STRUCTURE_STORAGE_KEY
from custom_components.sunspec2.const import STRUCTURE_STORAGE_VERSION
from custom_components.sunspec2.errors import TransportError
from custom_components.sunspec2.migration import CJNE_DOMAIN

from . import TEST_CONFIG_ENTRY_ID
from . import TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID
from . import create_mock_sunspec_client
from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG
from .const import MOCK_CONFIG_PREFIX


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
    assert type(config_entry.runtime_data) is SunSpecDataUpdateCoordinator

    # Reload the entry and assert that the runtime_data still holds a coordinator.
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert type(config_entry.runtime_data) is SunSpecDataUpdateCoordinator

    # Unload the entry. HA clears runtime_data on unload, so the
    # attribute either no longer exists or is None.
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert getattr(config_entry, "runtime_data", None) is None


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
    # other failure state and runtime_data would not have been
    # populated by the reload's async_setup_entry call.
    assert config_entry.state == ConfigEntryState.LOADED
    coordinator = config_entry.runtime_data
    assert isinstance(coordinator, SunSpecDataUpdateCoordinator)
    # The new (post-reload) coordinator picked up the new option.
    assert coordinator.api._capture_enabled is True


async def test_setup_blocked_when_cjne_actively_loaded(hass, sunspec_client_mock):
    """If cjne/ha-sunspec is currently loaded for the same host, our setup
    must refuse with ConfigEntryNotReady AND raise a Repairs panel issue.

    HA will then retry our setup automatically (exponential backoff)
    once the cjne entry is no longer loaded - which the user achieves
    by uninstalling cjne via HACS and restarting HA.
    """
    # Stand up an "active" cjne entry: matches our host/port/unit_id
    # AND is in LOADED state (simulating cjne currently running).
    cjne_entry = MockConfigEntry(
        domain="sunspec",
        data={"host": "test_host", "port": 123, "unit_id": 1},
        entry_id="cjne_active",
    )
    cjne_entry.add_to_hass(hass)
    cjne_entry.mock_state(hass, ConfigEntryState.LOADED)

    our_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="ours_blocked")
    our_entry.add_to_hass(hass)

    # async_setup raises ConfigEntryNotReady internally; the public
    # config_entries.async_setup() returns False rather than re-raising,
    # but the entry state will be SETUP_RETRY.
    result = await hass.config_entries.async_setup(our_entry.entry_id)
    await hass.async_block_till_done()
    assert result is False
    assert our_entry.state == ConfigEntryState.SETUP_RETRY

    # Repairs issue exists.
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{our_entry.entry_id}_cjne_conflict")
    assert issue is not None
    assert issue.translation_key == "cjne_conflict"
    assert issue.translation_placeholders["host"] == "test_host"


async def test_setup_clears_cjne_conflict_issue_after_resolution(hass, sunspec_client_mock):
    """After cjne is gone, a successful setup must clear any leftover
    cjne_conflict Repairs issue from a previous failed attempt.
    """
    # Pre-create the issue as if a previous setup attempt had failed.
    our_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="ours_recovered")
    our_entry.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{our_entry.entry_id}_cjne_conflict",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="cjne_conflict",
        translation_placeholders={"host": "test_host", "port": "123", "unit_id": "1"},
    )
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"{our_entry.entry_id}_cjne_conflict")
        is not None
    )

    # Now run the setup. cjne is not in hass.config_entries at all, so
    # the conflict guard passes, the issue is cleared, and setup succeeds.
    assert await hass.config_entries.async_setup(our_entry.entry_id)
    await hass.async_block_till_done()
    assert our_entry.state == ConfigEntryState.LOADED

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{our_entry.entry_id}_cjne_conflict") is None


async def test_gateway_lock_shared_per_host_port(hass):
    """Coordinators sharing the same TCP endpoint must share one lock.

    Several inverters and Modbus TCP gateways (notably SolarEdge) only
    accept a single TCP connection at a time. The class-level
    ``_GATEWAY_LOCKS`` dict + ``_get_gateway_lock`` ensure that two
    coordinators behind the same gateway will serialise their reads
    instead of fighting over the socket.
    """
    SunSpecDataUpdateCoordinator._GATEWAY_LOCKS.clear()

    a = SunSpecDataUpdateCoordinator._get_gateway_lock("10.0.0.1", 502)
    b = SunSpecDataUpdateCoordinator._get_gateway_lock("10.0.0.1", 502)
    c = SunSpecDataUpdateCoordinator._get_gateway_lock("10.0.0.1", 503)
    d = SunSpecDataUpdateCoordinator._get_gateway_lock("10.0.0.2", 502)

    # Same (host, port) -> same lock instance
    assert a is b
    # Different port -> different lock
    assert a is not c
    # Different host -> different lock
    assert a is not d

    SunSpecDataUpdateCoordinator._GATEWAY_LOCKS.clear()


async def test_gateway_lock_shared_for_rtu_serial_port(hass):
    """Two RTU coordinators on the same serial port must share the lock.

    cjne issue #317 (multi-unit-id behind one connection): the same
    per-(host, port) gateway lock that serialises TCP coordinators
    also covers RTU because RTU config entries store the serial
    port path as CONF_HOST and the baud rate as CONF_PORT (the
    "synthetic" key from the v0.11.0 config flow). So two RTU
    coordinators on /dev/ttyUSB0 @ 9600 with different unit IDs
    automatically share lock ("/dev/ttyUSB0", 9600) and never open
    the serial port concurrently. This test pins that contract.
    """
    SunSpecDataUpdateCoordinator._GATEWAY_LOCKS.clear()

    bus_a_unit_1 = SunSpecDataUpdateCoordinator._get_gateway_lock("/dev/ttyUSB0", 9600)
    bus_a_unit_2 = SunSpecDataUpdateCoordinator._get_gateway_lock("/dev/ttyUSB0", 9600)
    bus_a_other_baud = SunSpecDataUpdateCoordinator._get_gateway_lock("/dev/ttyUSB0", 19200)
    bus_b = SunSpecDataUpdateCoordinator._get_gateway_lock("/dev/ttyUSB1", 9600)

    # Same serial port + baud -> same lock (covers two unit IDs on
    # the same RS-485 bus).
    assert bus_a_unit_1 is bus_a_unit_2
    # Same port at a different baud rate is technically a different
    # bus configuration -> different lock.
    assert bus_a_unit_1 is not bus_a_other_baud
    # Different physical serial port -> different lock.
    assert bus_a_unit_1 is not bus_b

    SunSpecDataUpdateCoordinator._GATEWAY_LOCKS.clear()


async def test_setup_runs_cjne_migration_when_entries_present(hass, sunspec_client_mock):
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
    our_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="ours_with_migration")
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


# ---------------------------------------------------------------------------
# Resilience: in-cycle retry + stale-data tolerance
# ---------------------------------------------------------------------------
#
# Inverters frequently see flaky network connectivity. The coordinator
# absorbs that by (a) retrying a failed cycle once after a short pause and
# (b) keeping the entity "available" with the last good value through up
# to STALE_DATA_TOLERANCE_CYCLES consecutive failures. The four tests
# below pin every branch of that contract.


async def test_in_cycle_retry_recovers_after_first_failure(hass, sunspec_client_mock, monkeypatch):
    """First read of a cycle fails, retry succeeds -> cycle counts as good.

    The whole point of the in-cycle retry is to swallow a single transient
    blip without burning a coordinator failure or bumping any of the
    Repairs-panel counters. Uses the model-160-only config because the
    full MOCK_CONFIG also enables model 103, whose Evt1 bitfield32 sensor
    has multiple bits set in the test fixture and trips a pre-existing
    HA ENUM-validation error during async_write_ha_state - unrelated to
    the resilience contract under test here.
    """
    # Drop the retry sleep to zero so the test runs in milliseconds
    # instead of waiting on the production 5-second pause.
    monkeypatch.setattr("custom_components.sunspec2.INTERVAL_RETRY_DELAY_SECONDS", 0)

    config_entry = await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG_PREFIX)
    coordinator = config_entry.runtime_data
    assert coordinator.data is not None

    real_get_data = coordinator.api.async_get_data
    fail_state = {"raised": False}

    async def flaky_get_data(model_id):
        if not fail_state["raised"]:
            fail_state["raised"] = True
            raise TransportError("simulated one-shot blip")
        return await real_get_data(model_id)

    with patch.object(coordinator.api, "async_get_data", side_effect=flaky_get_data):
        await coordinator.async_refresh()

    # Cycle is recorded as successful and the stale-data counter never
    # left zero, so no Repairs issue can fire from this.
    assert coordinator.last_update_success is True
    assert coordinator.consecutive_failed_cycles == 0
    assert fail_state["raised"] is True


async def test_in_cycle_retry_exhausted_marks_cycle_failed(hass, sunspec_client_mock, monkeypatch):
    """Both attempts fail -> UpdateFailed and consecutive_failed_cycles bumps.

    Pinned because if the retry path silently swallowed the second
    failure we would never escalate to "unavailable" - the user would
    just see a frozen sensor forever.
    """
    monkeypatch.setattr("custom_components.sunspec2.INTERVAL_RETRY_DELAY_SECONDS", 0)

    config_entry = await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG_PREFIX)
    coordinator = config_entry.runtime_data
    assert coordinator.consecutive_failed_cycles == 0

    with patch.object(
        coordinator.api,
        "async_get_data",
        side_effect=TransportError("permanent blip"),
    ):
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert coordinator.consecutive_failed_cycles == 1


async def test_first_refresh_failure_skips_retry_delay(hass, sunspec_client_mock):
    """A failed first refresh must NOT trigger the in-cycle retry path.

    Setting up against an unreachable inverter has to fail fast so HA
    can raise ConfigEntryNotReady and let its standard exponential
    backoff drive the retry. If the in-cycle retry kicked in here every
    setup attempt would burn an extra INTERVAL_RETRY_DELAY_SECONDS - and
    over the lifetime of HA's backoff that adds up to a lot of needless
    waiting before the user sees the "this is broken" indicator.

    We assert call_count == 1 on async_get_data: a single call followed
    by an immediate fall-through to ConfigEntryNotReady. If the retry
    path leaked into the first-refresh code we would see two calls
    (the original plus the retry attempt).
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, entry_id="first_refresh_fast_fail"
    )
    config_entry.add_to_hass(hass)
    set_entry_setup_in_progress(hass, config_entry)

    with (
        patch(
            "custom_components.sunspec2.SunSpecApiClient.async_get_data",
            side_effect=TransportError("first refresh blip"),
        ) as get_data_mock,
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, config_entry)

    assert get_data_mock.call_count == 1


async def test_stale_data_tolerance_keeps_sensor_available(hass, sunspec_client_mock, monkeypatch):
    """Sensors keep the last value while consecutive failures stay <= N.

    Walks the coordinator from a healthy state through
    STALE_DATA_TOLERANCE_CYCLES + 1 consecutive failed cycles. Up to and
    including the threshold the DC current sensor must keep the
    previously read "90" value; the very next failed cycle finally tips
    the entity over to "unavailable". Uses MOCK_CONFIG_PREFIX (model 160
    only) to side-step the unrelated model-103 ENUM bitfield issue.
    """
    monkeypatch.setattr("custom_components.sunspec2.INTERVAL_RETRY_DELAY_SECONDS", 0)

    config_entry = await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG_PREFIX)
    coordinator = config_entry.runtime_data
    # Sanity-check the seed state - the mocked inverter reports DCA=90
    # on the first MPPT module, so that's the value the stale-data path
    # is meant to keep alive across failures.
    initial_state = hass.states.get(TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID)
    assert initial_state is not None
    assert initial_state.state == "90"

    with patch.object(
        coordinator.api,
        "async_get_data",
        side_effect=TransportError("blip"),
    ):
        for n in range(1, STALE_DATA_TOLERANCE_CYCLES + 1):
            await coordinator.async_refresh()
            assert coordinator.last_update_success is False
            assert coordinator.consecutive_failed_cycles == n
            stale_state = hass.states.get(TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID)
            assert stale_state.state == "90", (
                f"sensor flipped to {stale_state.state!r} after only "
                f"{n} failed cycles, expected stale value to survive"
            )

        # One more failure tips us past the tolerance.
        await coordinator.async_refresh()
        assert coordinator.consecutive_failed_cycles == STALE_DATA_TOLERANCE_CYCLES + 1
        unavailable_state = hass.states.get(TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID)
        assert unavailable_state.state == "unavailable"

    # Recovery: the next successful read must immediately reset the
    # stale-data counter and bring the sensor back to a fresh value.
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert coordinator.consecutive_failed_cycles == 0
    recovered_state = hass.states.get(TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID)
    assert recovered_state.state == "90"


# ---------------------------------------------------------------------------
# Defense in depth against the empty models_enabled regression
# ---------------------------------------------------------------------------
#
# v0.7.3 -> v0.7.5: a corrupted options-flow save could persist
# `models_enabled: []` to disk. The next coordinator reload would then
# poll zero models and every sensor on the integration would disappear.
# These two tests pin (a) the coordinator-side fallback that maps an
# empty filter back to DEFAULT_MODELS at init time and (b) the
# coordinator's `detected_models` cache that the options-flow form
# uses instead of api.known_models() so the multi-select can render
# even between cycles when api._client is closed.


async def test_empty_models_filter_falls_back_to_defaults(hass, sunspec_client_mock):
    """Setup with an empty models_enabled filter must fall back to DEFAULT_MODELS.

    Without this fallback the coordinator would happily poll zero
    models and the user would see all sensors disappear with nothing
    actionable in the logs.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        options={CONF_ENABLED_MODELS: []},
        entry_id="test_empty_filter_fallback",
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = config_entry.runtime_data
    # The coordinator must have rewritten the empty filter to the
    # full DEFAULT_MODELS set so subsequent cycles actually poll
    # something.
    assert coordinator.option_model_filter == set(DEFAULT_MODELS)


async def test_detected_models_cached_for_options_flow(hass, sunspec_client_mock):
    """First successful cycle must populate coordinator.detected_models.

    The options-flow form reads `coordinator.detected_models` to render
    its multi-select. If the cache stays empty the form would fall back
    to api.known_models() which returns [] between cycles, and the user
    would see an empty multi-select and silently re-trigger the very
    bug this branch is supposed to fix.
    """
    config_entry = await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG)
    coordinator = config_entry.runtime_data

    # detected_models is populated from api.async_get_models() during
    # the locked update cycle, so it should be non-empty after a
    # successful first refresh and survive the api.close() at cycle end.
    assert coordinator.detected_models, (
        "detected_models should be populated by the first successful cycle"
    )
    # Sanity check: the cache contains the models the test fixture
    # exposes (model 103 inverter and model 160 MPPT are both in
    # tests/test_data/inverter.json).
    assert 103 in coordinator.detected_models
    assert 160 in coordinator.detected_models


# ---------------------------------------------------------------------------
# Auto-detected nameplate AC power (model 120 / 121)
# ---------------------------------------------------------------------------
#
# v0.8.0: the coordinator reads SunSpec model 120 ("WRtg" - continuous AC
# power output) on the first successful cycle, falling back to model 121
# ("WMax") if 120 is missing. The result is exposed as
# coordinator.detected_max_ac_power_kw and the options-flow form uses it
# as a suggested_value for CONF_MAX_AC_POWER_KW so users do not have to
# type their inverter's nameplate by hand.


def _make_coordinator_with_mock_api(hass, model_data: dict):
    """Build a real coordinator with a hand-rolled mock API for nameplate tests.

    ``model_data`` is a {model_id: value_or_None} dict. ``async_get_data``
    on the mock returns a wrapper whose ``getValue`` returns the configured
    value, or raises if the model_id is not in the dict (simulating a
    pysunspec2 KeyError).
    """
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    from custom_components.sunspec2 import SunSpecDataUpdateCoordinator

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)

    api = MagicMock()

    async def fake_get_data(model_id):
        if model_id not in model_data:
            raise KeyError(f"model {model_id} not present")
        wrapper = MagicMock()
        wrapper.getValue.return_value = model_data[model_id]
        return wrapper

    api.async_get_data = AsyncMock(side_effect=fake_get_data)
    return SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)


async def test_nameplate_read_from_model_120(hass):
    """Model 120 WRtg is the canonical SunSpec source for the nameplate.

    Returns the value in kW. The test inverter advertises 7000 W
    capacity, which the coordinator should expose as 7.0 kW.
    """
    coordinator = _make_coordinator_with_mock_api(hass, {120: 7000.0})
    result = await coordinator._read_nameplate({1, 103, 120})
    assert result == 7.0


async def test_nameplate_falls_back_to_model_121_when_120_missing(hass):
    """Model 121 WMax is the fallback when the device doesn't expose model 120.

    Some inverters do not implement the Inverter Nameplate model 120 but
    do expose Inverter Settings model 121. WMax there is the configured
    max output power, which is usually but not always the nameplate.
    """
    coordinator = _make_coordinator_with_mock_api(hass, {121: 8500.0})
    result = await coordinator._read_nameplate({1, 103, 121})
    assert result == 8.5


async def test_nameplate_returns_none_when_neither_model_present(hass):
    """Devices without model 120 or 121 must just yield None.

    The auto-detection is a convenience, never a hard requirement; the
    plausibility filter simply stays unset and the user can configure it
    manually if they care.
    """
    coordinator = _make_coordinator_with_mock_api(hass, {})
    result = await coordinator._read_nameplate({1, 103, 160})
    assert result is None


async def test_nameplate_swallows_read_errors_and_falls_through(hass):
    """A flaky model-120 read must not crash the cycle, just try the next.

    Reading model 120 raises -> log at debug, try model 121 -> succeeds.
    Reading model 121 raises -> log at debug, return None. Either way the
    update cycle continues normally because the auto-detection is wrapped
    in a try/except per model_id.
    """
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    from custom_components.sunspec2 import SunSpecDataUpdateCoordinator

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)

    api = MagicMock()
    api.async_get_data = AsyncMock(side_effect=RuntimeError("simulated read failure"))
    coordinator = SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)

    # Both model 120 and 121 are advertised but the api raises. Must
    # return None without propagating the exception.
    result = await coordinator._read_nameplate({120, 121})
    assert result is None


# ---------------------------------------------------------------------------
# v0.13.0: stale-model tracking (cjne issue #202)
# ---------------------------------------------------------------------------


async def test_stale_model_tracking_increments_then_clears_on_recovery(hass):
    """A model that disappears bumps its counter; recovery clears it.

    Walks the coordinator manually through three transitions:
    1. Initial state: model 103 detected, counter empty
    2. Cycle without model 103: counter at 1
    3. Cycle with model 103 back: counter cleared
    """
    from unittest.mock import MagicMock as _MagicMock

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)
    api = _MagicMock()
    coordinator = SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)

    # Pretend the first cycle saw model 103.
    coordinator.detected_models = {1, 103}

    # Cycle 2: model 103 is gone. _missing_this_cycle would be set
    # by _run_one_update_cycle; we set it directly to skip the
    # connect/scan path.
    coordinator._missing_this_cycle = {103}
    coordinator._new_this_cycle = set()
    coordinator.detected_models = {1}
    coordinator._update_stale_model_tracking()
    assert list(coordinator._model_missing_since) == [103]

    # Cycle 3: model 103 is back.
    coordinator._missing_this_cycle = set()
    coordinator._new_this_cycle = {103}
    coordinator.detected_models = {1, 103}
    coordinator._update_stale_model_tracking()
    assert coordinator._model_missing_since == {}


async def test_stale_model_tracking_raises_repair_issue_after_threshold(hass):
    """A model gone for STALE_MODEL_TOLERANCE_SECONDS raises a Repairs issue."""
    from datetime import timedelta as _timedelta
    from unittest.mock import MagicMock as _MagicMock

    from homeassistant.util import dt as _dt_util

    from custom_components.sunspec2.const import STALE_MODEL_TOLERANCE_SECONDS

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)
    api = _MagicMock()
    coordinator = SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)

    coordinator.detected_models = {1, 103}
    coordinator._missing_this_cycle = {103}
    coordinator._new_this_cycle = set()
    coordinator.detected_models = {1}
    coordinator._update_stale_model_tracking()

    # Below the threshold nothing is raised, however many cycles run.
    for _ in range(50):
        coordinator._missing_this_cycle = {103}
        coordinator._update_stale_model_tracking()
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"{config_entry.entry_id}_stale_model_103") is None

    # Backdate the stamp past the threshold: one more cycle raises it.
    coordinator._model_missing_since[103] = _dt_util.utcnow() - _timedelta(
        seconds=STALE_MODEL_TOLERANCE_SECONDS + 60
    )
    coordinator._missing_this_cycle = {103}
    coordinator._update_stale_model_tracking()

    issue = registry.async_get_issue(DOMAIN, f"{config_entry.entry_id}_stale_model_103")
    assert issue is not None
    assert issue.translation_key == "stale_model"
    assert issue.translation_placeholders["model_id"] == "103"
    assert issue.translation_placeholders["missing_minutes"] == "11"


async def test_stale_stamp_is_not_reset_by_later_cycles(hass):
    """The stamp records when a model went missing, not when it was last seen missing.

    Re-stamping on every cycle would restart the clock forever and the
    threshold could never be reached.
    """
    from datetime import timedelta as _timedelta
    from unittest.mock import MagicMock as _MagicMock

    from homeassistant.util import dt as _dt_util

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)
    coordinator = SunSpecDataUpdateCoordinator(hass, client=_MagicMock(), entry=config_entry)

    coordinator.detected_models = {1}
    coordinator._missing_this_cycle = {103}
    coordinator._update_stale_model_tracking()
    first = coordinator._model_missing_since[103]

    coordinator._model_missing_since[103] = first - _timedelta(seconds=120)
    backdated = coordinator._model_missing_since[103]
    coordinator._missing_this_cycle = {103}
    coordinator._update_stale_model_tracking()

    assert coordinator._model_missing_since[103] == backdated
    assert coordinator._model_missing_since[103] < _dt_util.utcnow()


async def test_scan_delay_option_reaches_the_api_client(hass, sunspec_client_mock):
    """The options value has to survive the trip into SunSpecApiClient.

    Purely wiring, and wiring is exactly what breaks silently: the
    option would still be saved and shown in the form while every poll
    kept using the default pacing.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        options={CONF_SCAN_DELAY: 0.1},
        entry_id="scan_delay_entry",
    )
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.runtime_data.api._scan_delay == 0.1


async def test_scan_delay_defaults_when_option_absent(hass, sunspec_client_mock):
    """An entry saved before this option existed keeps working."""
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="no_scan_delay_entry")
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.runtime_data.api._scan_delay == DEFAULT_SCAN_DELAY_SECONDS


async def test_setup_removes_orphaned_control_model_sensors(hass, sunspec_client_mock):
    """Setup drops sensor rows for models the sensor platform stopped building.

    End to end version of the migration-level tests: a user who ticked
    model 123 before v0.14.0 has 21 registry entries that nothing feeds
    any more, and they render as "Unavailable" forever until something
    removes them.
    """
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="orphan_entry")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    orphan = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{config_entry.entry_id}_WMaxLimPct-123-0",
        suggested_object_id="wmaxlimpct",
        config_entry=config_entry,
    ).entity_id
    # Came back in v0.18.0: a timer point the user needs to see.
    kept = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{config_entry.entry_id}_WMaxLimPct_RmpTms-123-0",
        suggested_object_id="wmaxlimpct_rmptms",
        config_entry=config_entry,
    ).entity_id

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(orphan) is None
    assert registry.async_get(kept) is not None


# ---------------------------------------------------------------------------
# #42: a scan that stops early must not look like models disappearing
# ---------------------------------------------------------------------------


def _make_cycle_coordinator(hass, models: set[int], partial: bool):
    """A coordinator whose API reports ``models`` from a (partial) scan."""
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, options={CONF_ENABLED_MODELS: [103]}
    )
    config_entry.add_to_hass(hass)

    api = MagicMock()
    api.async_get_models = AsyncMock(return_value=sorted(models))
    api.async_get_data = AsyncMock(return_value=MagicMock())
    api.last_scan_was_partial = partial
    return SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)


async def test_truncated_model_list_fails_the_cycle(hass):
    """Losing a known model to a partial scan is a failure, not a reading.

    Reported as #42: v0.20.0 started treating a scan that stopped early
    as a success as long as it had found something. The models behind
    the failure point vanished from the list, their sensors returned
    None while the cycle still counted as successful, and the entities
    stayed "available" throughout - so the stale-data tolerance never
    engaged and the history shows a gap that looks exactly like a
    connection drop.
    """
    from custom_components.sunspec2.errors import TransientError

    coordinator = _make_cycle_coordinator(hass, {1, 103}, partial=True)
    coordinator.detected_models = {1, 103, 160}

    with pytest.raises(TransientError):
        await coordinator._run_one_update_cycle()


async def test_truncated_model_list_is_fine_on_a_first_scan(hass):
    """Nothing known is missing yet, so a short first scan is still usable.

    This is the cjne/ha-sunspec#375 case (SMA STP110-60): one unreadable
    block must not turn into "this inverter is not SunSpec".
    """
    coordinator = _make_cycle_coordinator(hass, {1, 103}, partial=True)

    data = await coordinator._run_one_update_cycle()

    assert 103 in data


async def test_models_missing_from_a_complete_scan_are_still_tracked(hass):
    """The partial-scan guard must not disarm the stale-model tracking.

    A model that is genuinely gone from a scan that ran to the end is
    the case cjne issue #202 is about, and it still has to reach the
    Repairs panel.
    """
    coordinator = _make_cycle_coordinator(hass, {1, 103}, partial=False)
    coordinator.detected_models = {1, 103, 714}

    await coordinator._run_one_update_cycle()

    assert coordinator._missing_this_cycle == {714}


async def test_nameplate_is_probed_once_per_run(hass):
    """A device without model 120 or 121 must not be re-probed forever.

    detected_max_ac_power_kw stays None on such a device, and the guard
    used to be that value alone, so every single cycle went looking
    again for models the scan had already said were not there.
    """
    # The device advertises model 120 but will not serve it, which is the
    # shape that used to loop: the read fails, nothing gets cached, and
    # the next cycle tries again.
    coordinator = _make_coordinator_with_mock_api(hass, {1: "device", 103: 1.0})
    coordinator.api.async_get_models = _AsyncMock(return_value=[1, 103, 120])

    for _ in range(3):
        await coordinator._run_one_update_cycle()

    probed = [call.args[0] for call in coordinator.api.async_get_data.call_args_list]
    assert probed.count(120) == 1


async def test_nameplate_read_timeout_is_not_swallowed(hass):
    """A timed-out read leaves the socket suspect, convenience or not.

    pysunspec2 never checks the Modbus TCP transaction id, so a late
    answer to the request we abandoned is read as the answer to the next
    one. Swallowing the timeout here would carry that off-by-one frame
    into the reads the cycle actually cares about.
    """
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    from custom_components.sunspec2.errors import TransientError

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)

    api = MagicMock()
    api.async_get_data = AsyncMock(side_effect=TransientError("Modbus read timeout"))
    coordinator = SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)

    with pytest.raises(TransientError):
        await coordinator._read_nameplate({120, 121})


# ---------------------------------------------------------------------------
# The persisted SunSpec model layout
# ---------------------------------------------------------------------------


async def test_stored_layout_is_handed_to_the_api_client(hass):
    """What an earlier run discovered has to reach the next one.

    The scan it saves is the one that runs inside Home Assistant's setup
    timeout, on the slowest hardware, at the worst possible moment.
    """
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)

    api = MagicMock()
    api.import_model_structure = MagicMock(return_value=True)
    api.structure_revision = 7
    coordinator = SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)

    stored = {
        "structure": {"base_addr": 40000, "models": [[1, 40002, 66]]},
        "identity": {"SN": "abc"},
    }
    coordinator._structure_store.async_load = AsyncMock(return_value=stored)

    await coordinator.async_load_model_structure()

    api.import_model_structure.assert_called_once_with(stored["structure"])
    assert coordinator._persisted_structure_revision == 7
    assert coordinator._persisted_identity == {"SN": "abc"}


async def test_layout_is_only_written_when_it_changed(hass):
    """The store must not be rewritten on every successful cycle."""
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)

    api = MagicMock()
    api.structure_revision = 1
    api.export_model_structure = MagicMock(
        return_value={"base_addr": 40000, "models": [[1, 40002, 66]]}
    )
    coordinator = SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)
    coordinator._structure_store.async_save = AsyncMock()

    await coordinator._async_save_model_structure()
    await coordinator._async_save_model_structure()
    assert coordinator._structure_store.async_save.call_count == 1

    api.structure_revision = 2
    await coordinator._async_save_model_structure()
    assert coordinator._structure_store.async_save.call_count == 2


async def test_identity_mismatch_discards_the_stored_layout(hass):
    """Another inverter on a recycled IP must not be read at the old offsets.

    The address checks cannot see this one: a different device of a
    different make can lay its model tree out the same way and still
    mean something entirely different by the registers.

    The store file has to go at the same time. This check only ever
    runs on the first cycle of a run, and a failed first cycle is
    retried by Home Assistant with a NEW coordinator that loads the
    store again, so clearing the in-memory copy alone loops forever
    (#49).
    """
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    from custom_components.sunspec2.errors import TransientError

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)

    api = MagicMock()
    coordinator = SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)
    coordinator._persisted_identity = {"Mn": "KACO", "SN": "111"}
    coordinator._structure_store.async_remove = AsyncMock()

    device_info = MagicMock()
    device_info.getValue = lambda point: {"Mn": "SMA", "SN": "222"}.get(point)
    coordinator.device_info = device_info

    with pytest.raises(TransientError):
        await coordinator._check_device_identity()

    api.reconnect_next.assert_called_once()
    coordinator._structure_store.async_remove.assert_awaited_once()
    assert coordinator._persisted_identity is None


async def test_matching_identity_passes_quietly(hass):
    """The common case costs nothing and happens once per run."""
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)

    api = MagicMock()
    coordinator = SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)
    coordinator._persisted_identity = {"Mn": "KACO", "SN": "111"}
    coordinator._structure_store.async_remove = AsyncMock()

    device_info = MagicMock()
    device_info.getValue = lambda point: {"Mn": "KACO", "SN": "111"}.get(point)
    coordinator.device_info = device_info

    assert await coordinator._check_device_identity() is False
    assert await coordinator._check_device_identity() is False

    api.reconnect_next.assert_not_called()
    coordinator._structure_store.async_remove.assert_not_awaited()


async def test_firmware_update_is_the_same_device(hass, caplog):
    """#49: a new version string is not a new inverter.

    Same manufacturer, model and serial, different Vr. The cycle must
    run through with the data it read; what changes is that the next
    connect rescans the model tree, because a firmware update is the
    one event that can legitimately change it.
    """
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)

    api = MagicMock()
    coordinator = SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)
    coordinator._persisted_identity = {
        "Mn": "Fronius",
        "Md": "Symo GEN24 4.0",
        "Vr": "1.41.10-1",
        "SN": "34778633",
    }
    coordinator._structure_store.async_remove = AsyncMock()

    device_info = MagicMock()
    device_info.getValue = lambda point: {
        "Mn": "Fronius",
        "Md": "Symo GEN24 4.0",
        "Vr": "1.41.11-1",
        "SN": "34778633",
    }.get(point)
    coordinator.device_info = device_info

    assert await coordinator._check_device_identity() is True

    # Not a failure: no reconnect from inside the check, no store wipe,
    # no warning. The rescan is the caller's job, after the reads.
    api.reconnect_next.assert_not_called()
    coordinator._structure_store.async_remove.assert_not_awaited()
    assert "not the one the stored SunSpec layout came from" not in caplog.text
    assert "Firmware version changed (1.41.10-1 -> 1.41.11-1)" in caplog.text
    # Forgotten, so the new version string gets written out with the
    # next save instead of being re-reported on every restart.
    assert coordinator._persisted_identity is None


async def test_layout_is_rewritten_when_only_the_identity_moved(hass):
    """After a firmware update the rescan may find the very same layout.

    The revision then does not move, and without the identity in the
    comparison the old version string would stay on disk and the
    "firmware changed" rescan would repeat on every restart.
    """
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    config_entry.add_to_hass(hass)

    api = MagicMock()
    api.structure_revision = 3
    api.export_model_structure = MagicMock(
        return_value={"base_addr": 40000, "models": [[1, 40002, 66]]}
    )
    coordinator = SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)
    coordinator._structure_store.async_save = AsyncMock()
    coordinator._persisted_structure_revision = 3
    coordinator._persisted_identity = {"Mn": "KACO", "SN": "111", "Vr": "1.0"}

    device_info = MagicMock()
    device_info.getValue = lambda point: {"Mn": "KACO", "SN": "111", "Vr": "2.0"}.get(point)
    coordinator.device_info = device_info

    await coordinator._async_save_model_structure()
    coordinator._structure_store.async_save.assert_awaited_once()
    saved = coordinator._structure_store.async_save.await_args.args[0]
    assert saved["identity"] == {"Mn": "KACO", "SN": "111", "Vr": "2.0"}

    # And now it is settled: same revision, same identity, no rewrite.
    await coordinator._async_save_model_structure()
    coordinator._structure_store.async_save.assert_awaited_once()


def _stored_layout(identity: dict) -> dict:
    """A store payload as ``_async_save_model_structure`` writes it."""
    return {
        "version": STRUCTURE_STORAGE_VERSION,
        "minor_version": 1,
        "key": f"{STRUCTURE_STORAGE_KEY}.{TEST_CONFIG_ENTRY_ID}",
        "data": {
            "structure": {"revision": 1, "base_addr": 40000, "models": [[1, 40002, 66]]},
            "identity": identity,
        },
    }


async def test_setup_recovers_from_a_stored_layout_of_another_device(
    hass, hass_storage, sunspec_client_mock
):
    """#49 end to end: the retry must not load the layout it just rejected.

    The first setup attempt fails, as designed, because the store says
    this address used to belong to a different serial. Home Assistant
    retries with a fresh coordinator. Until v0.26.0 that coordinator
    loaded the same store file and failed the same way, forever.
    """
    from homeassistant.config_entries import ConfigEntryState

    key = f"{STRUCTURE_STORAGE_KEY}.{TEST_CONFIG_ENTRY_ID}"
    hass_storage[key] = _stored_layout(
        {"Mn": "SunSpecTest", "Md": "Test-1547-1", "Vr": "1.2.3", "SN": "a-different-one"}
    )

    config_entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    with patch(
        "custom_components.sunspec2.SunSpecApiClient",
        return_value=create_mock_sunspec_client(hass),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    # The offending store file is gone before the retry runs.
    assert key not in hass_storage

    with patch(
        "custom_components.sunspec2.SunSpecApiClient",
        return_value=create_mock_sunspec_client(hass),
    ):
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED


async def test_setup_survives_a_firmware_update(hass, hass_storage, sunspec_client_mock, caplog):
    """#49 as reported: Vr moved from one restart to the next, nothing else.

    Setup has to go straight to LOADED. The only trace is one info line
    and a rescan flagged for the next connect.
    """
    from homeassistant.config_entries import ConfigEntryState

    key = f"{STRUCTURE_STORAGE_KEY}.{TEST_CONFIG_ENTRY_ID}"
    hass_storage[key] = _stored_layout(
        {"Mn": "SunSpecTest", "Md": "Test-1547-1", "Vr": "1.2.2", "SN": "sn-123456789"}
    )

    config_entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    api = create_mock_sunspec_client(hass)
    with patch("custom_components.sunspec2.SunSpecApiClient", return_value=api):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert "belongs to a different device" not in caplog.text
    assert "Firmware version changed (1.2.2 -> 1.2.3)" in caplog.text
    # The rescan is queued for the next connect, not forced mid-cycle.
    assert api._reconnect is True


# ---------------------------------------------------------------------------
# v0.22.0: the Modbus session is held open
# ---------------------------------------------------------------------------


def _keepalive_coordinator(hass, options=None):
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, options=options or {CONF_ENABLED_MODELS: [103]}
    )
    config_entry.add_to_hass(hass)

    api = MagicMock()
    api.async_get_models = AsyncMock(return_value=[1, 103])
    api.async_get_data = AsyncMock(return_value=MagicMock())
    api.last_scan_was_partial = False
    return config_entry, SunSpecDataUpdateCoordinator(hass, client=api, entry=config_entry)


async def test_cycle_keeps_the_session_open(hass):
    """The default is one session, not one session per poll.

    Measured on a KACO Powador 7.8 TL3 at a 30 s interval: a fresh
    session per poll failed 5 of 6 cycles, while a single session held
    open served 20 of 20 at a steady 1.6 s. Modbus TCP is built around
    a session that stays up, and an embedded stack asked to rebuild one
    every 30 seconds is the case it handles worst.
    """
    _, coordinator = _keepalive_coordinator(hass)

    await coordinator._run_one_update_cycle()

    coordinator.api.close.assert_not_called()


async def test_cycle_releases_the_slot_when_asked_to(hass):
    """The option exists for a reader outside HA that cannot use a proxy."""
    from custom_components.sunspec2.const import CONF_RELEASE_SLOT

    _, coordinator = _keepalive_coordinator(
        hass, options={CONF_ENABLED_MODELS: [103], CONF_RELEASE_SLOT: True}
    )

    await coordinator._run_one_update_cycle()

    coordinator.api.close.assert_called_once()


async def test_a_shared_gateway_releases_the_slot_without_being_told(hass):
    """Two entries on one endpoint must not need a checkbox.

    This is the SolarEdge-style gateway the per-gateway lock already
    serialises. Holding the session open there would starve the second
    entry permanently, and the user has no reason to know that an
    option governs it.
    """
    _, coordinator = _keepalive_coordinator(hass)
    assert coordinator.release_slot_between_polls is False

    neighbour = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options={})
    neighbour.add_to_hass(hass)

    assert coordinator.release_slot_between_polls is True

    await coordinator._run_one_update_cycle()
    coordinator.api.close.assert_called_once()


async def test_a_failed_cycle_drops_the_session_hard(hass):
    """A session that already misbehaved does not get to stay.

    pysunspec2 never checks the Modbus TCP transaction id, so a late
    answer on a socket we gave up on is read as the answer to the next
    request, and every register after it lands in the wrong field.
    """
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.sunspec2.errors import TransportError

    _, coordinator = _keepalive_coordinator(hass)
    # The bookkeeping ends by re-raising as UpdateFailed, which is how
    # HA learns the cycle failed. What matters here is what it did on
    # the way out.
    with pytest.raises(UpdateFailed):
        coordinator._after_failed_cycle(TransportError("boom"))

    coordinator.api.close.assert_called_once_with(force=True)
    coordinator.api.reconnect_next.assert_called_once()


@pytest.mark.parametrize("stored_interval", [0, -5, "not a number"])
async def test_coordinator_clamps_an_unusable_stored_scan_interval(
    hass, sunspec_client_mock, stored_interval, caplog
):
    """An entry saved before the range check must repair itself on reload.

    The config flow now refuses 0 and negatives, but that does nothing
    for an entry already on disk, and neither value survives contact
    with HA's coordinator: ``timedelta(seconds=0)`` is falsy, so
    ``update_interval`` becomes None and ``_schedule_refresh`` returns
    early - polling stops silently and forever, with no failed cycle to
    make it visible. A negative value is truthy and puts the next
    refresh in the past, hot-looping the coordinator.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG, CONF_SCAN_INTERVAL: stored_interval},
        entry_id="test_bad_interval",
    )
    entry.add_to_hass(hass)

    caplog.clear()
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    coordinator = entry.runtime_data

    assert coordinator.update_interval is not None
    assert coordinator.update_interval.total_seconds() >= MIN_SCAN_INTERVAL_SECONDS
    if stored_interval != "not a number":
        assert any(
            "below the" in r.getMessage() and "minimum" in r.getMessage() for r in caplog.records
        )
