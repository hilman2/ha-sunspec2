"""Tests for the cjne→sunspec2 migration helper (Phase 5)."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.migration import CJNE_DOMAIN
from custom_components.sunspec2.migration import cleanup_excluded_sensor_entities
from custom_components.sunspec2.migration import find_blocking_cjne_entries
from custom_components.sunspec2.migration import migrate_from_cjne_sync

from .const import MOCK_CONFIG

_LOG = logging.getLogger(__name__)


# ---------- helpers ---------------------------------------------------------


def _our_entry(hass) -> MockConfigEntry:
    """A live sunspec2 config entry for the standard MOCK_CONFIG host."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="ours_aaa")
    entry.add_to_hass(hass)
    return entry


def _make_cjne_entry(
    hass,
    *,
    entry_id: str = "cjne_xyz",
    host: str = "test_host",
    port: int = 123,
    unit_id: int | None = 1,
    use_slave_id_field: bool = False,
) -> MockConfigEntry:
    """Add a fake cjne config entry to hass.

    If ``use_slave_id_field`` is True the entry data uses the legacy
    ``slave_id`` field name instead of ``unit_id`` (cjne pre-Phase-0).
    """
    data = {"host": host, "port": port}
    if use_slave_id_field:
        data["slave_id"] = unit_id
    else:
        data["unit_id"] = unit_id
    entry = MockConfigEntry(domain=CJNE_DOMAIN, data=data, entry_id=entry_id)
    entry.add_to_hass(hass)
    return entry


def _register_cjne_entity(
    hass,
    cjne_entry: MockConfigEntry,
    suffix: str,
    object_id: str | None = None,
) -> str:
    """Register an entity in the entity registry under the cjne platform.

    Returns the entity_id. The unique_id follows the cjne format
    ``{cjne_entry_id}_{suffix}`` so the migration's prefix-swap can
    operate on it.
    """
    registry = er.async_get(hass)
    unique_id = f"{cjne_entry.entry_id}_{suffix}"
    entry = registry.async_get_or_create(
        "sensor",
        CJNE_DOMAIN,
        unique_id,
        suggested_object_id=object_id or suffix.lower().replace("-", "_"),
        config_entry=cjne_entry,
    )
    return entry.entity_id


# ---------- tests -----------------------------------------------------------


async def test_migrate_no_cjne_installed(hass):
    """No cjne config entries -> migration is a quiet no-op."""
    entry = _our_entry(hass)

    migrated, skipped, errors = migrate_from_cjne_sync(hass, entry, _LOG)

    assert migrated == 0
    assert skipped == []
    assert errors == []


async def test_migrate_no_matching_host(hass):
    """cjne entry exists but for a different host -> no-op."""
    entry = _our_entry(hass)
    cjne = _make_cjne_entry(hass, host="some_other_host", port=502, unit_id=1)
    _register_cjne_entity(hass, cjne, "W-103-0")

    migrated, skipped, errors = migrate_from_cjne_sync(hass, entry, _LOG)

    assert migrated == 0
    assert skipped == []
    assert errors == []


async def test_migrate_orphan_entities_succeeds(hass):
    """Matching cjne orphan entities are retargeted to sunspec2."""
    entry = _our_entry(hass)
    cjne = _make_cjne_entry(hass)
    eid_w = _register_cjne_entity(hass, cjne, "W-103-0", object_id="inverter_three_phase_watts")
    eid_a = _register_cjne_entity(hass, cjne, "A-103-0", object_id="inverter_three_phase_amps")

    migrated, skipped, errors = migrate_from_cjne_sync(hass, entry, _LOG)

    assert migrated == 2
    assert skipped == []
    assert errors == []

    registry = er.async_get(hass)
    re_w = registry.async_get(eid_w)
    re_a = registry.async_get(eid_a)
    assert re_w.platform == "sunspec2"
    assert re_w.config_entry_id == entry.entry_id
    assert re_w.unique_id == f"{entry.entry_id}_W-103-0"
    assert re_a.platform == "sunspec2"
    assert re_a.unique_id == f"{entry.entry_id}_A-103-0"


async def test_migrate_preserves_entity_id(hass):
    """The entity_id (and therefore Recorder history) survives migration."""
    entry = _our_entry(hass)
    cjne = _make_cjne_entry(hass)
    eid_before = _register_cjne_entity(
        hass, cjne, "W-103-0", object_id="inverter_three_phase_watts"
    )

    migrate_from_cjne_sync(hass, entry, _LOG)

    registry = er.async_get(hass)
    re_after = registry.async_get(eid_before)
    assert re_after is not None, "entity_id must survive migration"
    assert re_after.entity_id == eid_before


async def test_migrate_skips_loaded_entities(hass):
    """Entities with a live state in hass.states cannot be migrated."""
    entry = _our_entry(hass)
    cjne = _make_cjne_entry(hass)
    eid = _register_cjne_entity(hass, cjne, "W-103-0")

    # Simulate cjne still actively running this entity by writing a state.
    hass.states.async_set(eid, "1234")

    migrated, skipped, errors = migrate_from_cjne_sync(hass, entry, _LOG)

    assert migrated == 0
    assert eid in skipped
    assert errors == []


async def test_migrate_handles_malformed_unique_id(hass):
    """A cjne entity whose unique_id does not match the expected prefix is
    reported as an error and the migration continues with other entities.
    """
    entry = _our_entry(hass)
    cjne = _make_cjne_entry(hass)
    # Healthy entity
    healthy_eid = _register_cjne_entity(hass, cjne, "W-103-0")
    # Malformed entity: does not start with "{cjne_entry_id}_"
    registry = er.async_get(hass)
    bad_entry = registry.async_get_or_create(
        "sensor",
        CJNE_DOMAIN,
        "completely_unrelated_id_format",
        suggested_object_id="something_weird",
        config_entry=cjne,
    )

    migrated, skipped, errors = migrate_from_cjne_sync(hass, entry, _LOG)

    assert migrated == 1  # the healthy one
    assert skipped == []
    assert len(errors) == 1
    assert bad_entry.entity_id in errors[0]
    # Healthy one was migrated successfully
    assert registry.async_get(healthy_eid).platform == "sunspec2"


async def test_migrate_multi_inverter_only_matching(hass):
    """Two cjne entries for different hosts: only the matching one migrates."""
    entry = _our_entry(hass)
    matching_cjne = _make_cjne_entry(
        hass, entry_id="cjne_match", host="test_host", port=123, unit_id=1
    )
    other_cjne = _make_cjne_entry(
        hass, entry_id="cjne_other", host="another_host", port=502, unit_id=1
    )
    matching_eid = _register_cjne_entity(hass, matching_cjne, "W-103-0")
    other_eid = _register_cjne_entity(
        hass, other_cjne, "W-103-0", object_id="another_inverter_watts"
    )

    migrated, _, _ = migrate_from_cjne_sync(hass, entry, _LOG)

    assert migrated == 1

    registry = er.async_get(hass)
    assert registry.async_get(matching_eid).platform == "sunspec2"
    # The unrelated cjne entity is untouched.
    assert registry.async_get(other_eid).platform == CJNE_DOMAIN


async def test_migrate_handles_legacy_slave_id_field(hass):
    """A cjne entry that still uses the legacy 'slave_id' data field
    instead of 'unit_id' must still match and migrate."""
    entry = _our_entry(hass)
    cjne = _make_cjne_entry(hass, use_slave_id_field=True)
    eid = _register_cjne_entity(hass, cjne, "W-103-0")

    migrated, _, _ = migrate_from_cjne_sync(hass, entry, _LOG)

    assert migrated == 1
    registry = er.async_get(hass)
    assert registry.async_get(eid).platform == "sunspec2"


# ---------- find_blocking_cjne_entries ---------------------------------------


async def test_find_blocking_no_cjne_installed(hass):
    """No cjne entries -> nothing to block."""
    entry = _our_entry(hass)
    assert find_blocking_cjne_entries(hass, entry) == []


async def test_find_blocking_orphan_cjne_does_not_block(hass):
    """A cjne config entry that is NOT loaded (orphan/failed/disabled)
    does not block our setup. The migration helper will handle its
    orphan entities cleanly."""
    entry = _our_entry(hass)
    cjne = _make_cjne_entry(hass)
    # MockConfigEntry default state is NOT_LOADED, just verify
    assert cjne.state != ConfigEntryState.LOADED

    assert find_blocking_cjne_entries(hass, entry) == []


async def test_find_blocking_active_cjne_blocks(hass):
    """A cjne config entry that IS in LOADED state for the same host
    blocks our setup."""
    entry = _our_entry(hass)
    cjne = _make_cjne_entry(hass)
    # Force the cjne entry into LOADED state, simulating cjne running.
    cjne.mock_state(hass, ConfigEntryState.LOADED)

    blocking = find_blocking_cjne_entries(hass, entry)
    assert len(blocking) == 1
    assert blocking[0] is cjne


async def test_find_blocking_loaded_but_different_host_does_not_block(hass):
    """A cjne entry can be loaded for a DIFFERENT inverter and that
    must not block our setup for our own inverter."""
    entry = _our_entry(hass)
    cjne = _make_cjne_entry(hass, host="another_host", port=502, unit_id=1)
    cjne.mock_state(hass, ConfigEntryState.LOADED)

    assert find_blocking_cjne_entries(hass, entry) == []


# ---------- orphaned control-model sensor cleanup (#17) ---------------------


def _register_ours(
    hass,
    our_entry: MockConfigEntry,
    domain: str,
    suffix: str,
) -> str:
    """Register an entity under OUR platform with a given unique_id suffix."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        domain,
        DOMAIN,
        f"{our_entry.entry_id}_{suffix}",
        suggested_object_id=suffix.lower().replace("-", "_").replace(":", "_"),
        config_entry=our_entry,
    )
    return entry.entity_id


async def test_cleanup_removes_orphaned_model_123_sensors(hass):
    """Sensors left over from a pre-v0.14.0 install of model 123 are removed."""
    entry = _our_entry(hass)
    revert = _register_ours(hass, entry, "sensor", "WMaxLimPct_RvrtTms-123-0")
    limit = _register_ours(hass, entry, "sensor", "WMaxLimPct-123-0")

    removed = cleanup_excluded_sensor_entities(hass, entry, _LOG)

    assert sorted(removed) == sorted([revert, limit])
    registry = er.async_get(hass)
    assert registry.async_get(revert) is None
    assert registry.async_get(limit) is None


async def test_cleanup_keeps_the_write_entities(hass):
    """The Number / Switch controls share model 123 and must survive.

    They use the same get_sunspec_unique_id helper, so a cleanup keyed
    on the unique_id alone would delete the controls the user operates
    on every single setup while the beta flag is on.
    """
    entry = _our_entry(hass)
    number = _register_ours(hass, entry, "number", "WMaxLimPct-123-0")
    switch = _register_ours(hass, entry, "switch", "WMaxLim_Ena-123-0")
    orphan = _register_ours(hass, entry, "sensor", "WMaxLimPct_RvrtTms-123-0")

    removed = cleanup_excluded_sensor_entities(hass, entry, _LOG)

    assert removed == [orphan]
    registry = er.async_get(hass)
    assert registry.async_get(number) is not None
    assert registry.async_get(switch) is not None


async def test_cleanup_leaves_normal_model_sensors_alone(hass):
    """Only points in SENSOR_EXCLUDED_POINTS are touched."""
    entry = _our_entry(hass)
    watts = _register_ours(hass, entry, "sensor", "W-103-0")
    mppt = _register_ours(hass, entry, "sensor", "module:0:DCW-160-0")

    removed = cleanup_excluded_sensor_entities(hass, entry, _LOG)

    assert removed == []
    registry = er.async_get(hass)
    assert registry.async_get(watts) is not None
    assert registry.async_get(mppt) is not None


async def test_cleanup_ignores_foreign_unique_id_formats(hass):
    """A unique_id we did not write is left alone rather than guessed at."""
    entry = _our_entry(hass)
    weird = _register_ours(hass, entry, "sensor", "no-model-suffix")
    trailing = _register_ours(hass, entry, "sensor", "W-123-notanindex")

    removed = cleanup_excluded_sensor_entities(hass, entry, _LOG)

    assert removed == []
    registry = er.async_get(hass)
    assert registry.async_get(weird) is not None
    assert registry.async_get(trailing) is not None


async def test_cleanup_does_not_touch_other_config_entries(hass):
    """A second inverter's orphans are that entry's business, not ours."""
    ours = _our_entry(hass)
    other = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="ours_bbb")
    other.add_to_hass(hass)
    theirs = _register_ours(hass, other, "sensor", "WMaxLimPct_RvrtTms-123-0")

    removed = cleanup_excluded_sensor_entities(hass, ours, _LOG)

    assert removed == []
    assert er.async_get(hass).async_get(theirs) is not None


async def test_cleanup_is_idempotent(hass):
    """Running twice removes nothing the second time."""
    entry = _our_entry(hass)
    _register_ours(hass, entry, "sensor", "WMaxLimPct_RvrtTms-123-0")

    assert len(cleanup_excluded_sensor_entities(hass, entry, _LOG)) == 1
    assert cleanup_excluded_sensor_entities(hass, entry, _LOG) == []


async def test_cleanup_keeps_the_model_123_timer_sensors(hass):
    """The timers came back in v0.18.0 and must survive the cleanup pass.

    @tisoft needs exactly these to see that a limit has lapsed (#17).
    A cleanup still keyed on "model 123" would delete them on every
    setup, which is why it shares the platform's predicate instead of
    keeping its own copy of the rule.
    """
    entry = _our_entry(hass)
    ramp = _register_ours(hass, entry, "sensor", "WMaxLimPct_RmpTms-123-0")
    window = _register_ours(hass, entry, "sensor", "WMaxLimPct_WinTms-123-0")
    pf_revert = _register_ours(hass, entry, "sensor", "OutPFSet_RvrtTms-123-0")

    removed = cleanup_excluded_sensor_entities(hass, entry, _LOG)

    assert removed == []
    registry = er.async_get(hass)
    for entity_id in (ramp, window, pf_revert):
        assert registry.async_get(entity_id) is not None


async def test_cleanup_removes_unmappable_unit_points(hass):
    """% VArMax and friends have no HA unit and must stay out."""
    entry = _our_entry(hass)
    var_max = _register_ours(hass, entry, "sensor", "VArMaxPct-123-0")
    var_aval = _register_ours(hass, entry, "sensor", "VArAvalPct-123-0")

    removed = cleanup_excluded_sensor_entities(hass, entry, _LOG)

    assert sorted(removed) == sorted([var_max, var_aval])


async def test_cleanup_matches_the_point_inside_a_repeating_group_key(hass):
    """Group-flattened keys arrive as group:idx:point and must still match."""
    entry = _our_entry(hass)
    grouped = _register_ours(hass, entry, "sensor", "ctl:0:WMaxLimPct-123-0")

    removed = cleanup_excluded_sensor_entities(hass, entry, _LOG)

    assert removed == [grouped]
