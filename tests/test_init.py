"""Test SunSpec setup process."""

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
from custom_components.sunspec2.const import CONF_SCAN_INTERVAL
from custom_components.sunspec2.const import DEFAULT_MODELS
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.const import STALE_DATA_TOLERANCE_CYCLES
from custom_components.sunspec2.errors import TransportError
from custom_components.sunspec2.migration import CJNE_DOMAIN

from . import TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID
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
    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is SunSpecDataUpdateCoordinator

    # Reload the entry and assert that the data from above is still there.
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is SunSpecDataUpdateCoordinator

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
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
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
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
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
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
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

    coordinator = hass.data[DOMAIN][config_entry.entry_id]
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
    coordinator = hass.data[DOMAIN][config_entry.entry_id]

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
