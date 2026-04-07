"""Tests for the cjne→sunspec2 migration helper (Phase 5)."""

from __future__ import annotations

import logging

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.migration import (
    CJNE_DOMAIN,
    migrate_from_cjne_sync,
)

from .const import MOCK_CONFIG

_LOG = logging.getLogger(__name__)


# ---------- helpers ---------------------------------------------------------


def _our_entry(hass) -> MockConfigEntry:
    """A live sunspec2 config entry for the standard MOCK_CONFIG host."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, entry_id="ours_aaa"
    )
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
    matching_cjne = _make_cjne_entry(hass, entry_id="cjne_match", host="test_host", port=123, unit_id=1)
    other_cjne = _make_cjne_entry(hass, entry_id="cjne_other", host="another_host", port=502, unit_id=1)
    matching_eid = _register_cjne_entity(hass, matching_cjne, "W-103-0")
    other_eid = _register_cjne_entity(hass, other_cjne, "W-103-0", object_id="another_inverter_watts")

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
