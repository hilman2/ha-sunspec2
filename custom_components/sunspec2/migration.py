"""Migration helper from cjne/ha-sunspec to sunspec2.

Phase 5 user-value step. When a user installs sunspec2 on a HA instance
that already has the upstream cjne/ha-sunspec integration's entities in
the entity registry (typically: cjne is freshly uninstalled but the
config entry's entities are still in the registry as orphans), this
helper finds those orphans and retargets them to our domain so the
user keeps:

- the entity ids (e.g. ``sensor.inverter_three_phase_watts``)
- the Recorder history attached to those entity ids
- any user customisations (icon, area, name)
- references from automations, scripts, dashboards

The cjne unique_id format is verified identical to ours:
``f"{config_entry_id}_{key}-{model_id}-{model_index}"``. The only
difference is the entry_id prefix - cjne has its own config entry per
inverter, we have ours. Migration just swaps the prefix.

The actual platform/config_entry_id/unique_id rewrite goes through
``EntityRegistry.async_update_entity_platform``, which HA explicitly
documents as "should only be used when an entity needs to be migrated
between integrations." It is the only public HA API that can change
``platform`` on an existing entity without removing it - and removing
the entity would lose the Recorder history.

Constraint enforced by HA: the entity must NOT currently be loaded
(no state in ``hass.states``, or state is ``unknown``). If cjne is
still actively running, its entities are loaded and the migration
will skip them. The user has to uninstall cjne first, restart HA,
then add our integration. The blocked-migration code path produces
a persistent notification with that exact instruction.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_HOST, CONF_PORT, CONF_UNIT_ID

CJNE_DOMAIN = "sunspec"
"""The legacy domain we migrate FROM."""


def find_blocking_cjne_entries(
    hass: HomeAssistant, entry: ConfigEntry
) -> list[ConfigEntry]:
    """Return active cjne config entries that conflict with this entry.

    A cjne config entry "conflicts" if it matches our host:port:unit_id
    AND is currently in the LOADED state. While loaded, cjne holds the
    inverter's single Modbus TCP slot open (pysunspec2 never closes the
    socket - Phase 2 finding) and our coordinator would race against it.

    The user must uninstall cjne before sunspec2 can take over. Returning
    a non-empty list signals async_setup_entry to refuse setup with
    ConfigEntryNotReady so HA retries automatically once the user removes
    cjne and restarts.
    """
    host = entry.data.get(CONF_HOST)
    port = entry.data.get(CONF_PORT)
    unit_id = entry.data.get(CONF_UNIT_ID, 1)
    blocking: list[ConfigEntry] = []
    for cjne_entry in hass.config_entries.async_entries(CJNE_DOMAIN):
        ce_host = cjne_entry.data.get("host")
        ce_port = cjne_entry.data.get("port")
        ce_uid = cjne_entry.data.get("unit_id") or cjne_entry.data.get("slave_id")
        if ce_host != host or ce_port != port or ce_uid != unit_id:
            continue
        if cjne_entry.state == ConfigEntryState.LOADED:
            blocking.append(cjne_entry)
    return blocking


def migrate_from_cjne_sync(
    hass: HomeAssistant,
    entry: ConfigEntry,
    log: Any,
) -> tuple[int, list[str], list[str]]:
    """Find orphan cjne sunspec entities and retarget them to sunspec2.

    Returns a tuple of:
      - migrated_count: how many entities we successfully retargeted
      - skipped_loaded: entity_ids of cjne entities that are still
        actively loaded (cjne integration still running) and could not
        be migrated
      - errors: free-form per-entity error messages for entities that
        could not be migrated for other reasons (malformed unique_id,
        etc.)

    The function is synchronous because every helper it calls
    (``er.async_get``, ``async_entries``, ``async_update_entity_platform``)
    is sync. The async wrapper in ``__init__.py`` is what
    ``async_setup_entry`` actually awaits.
    """
    registry = er.async_get(hass)
    host = entry.data.get(CONF_HOST)
    port = entry.data.get(CONF_PORT)
    unit_id = entry.data.get(CONF_UNIT_ID, 1)

    cjne_entries = []
    for cjne_entry in hass.config_entries.async_entries(CJNE_DOMAIN):
        ce_host = cjne_entry.data.get("host")
        ce_port = cjne_entry.data.get("port")
        # cjne pre-Phase-0 used 'slave_id'; the Phase-0 v1->v2 migration
        # in our own __init__.py renamed it to 'unit_id', but cjne itself
        # never landed that rename. Match either field name.
        ce_uid = cjne_entry.data.get("unit_id") or cjne_entry.data.get("slave_id")
        if ce_host == host and ce_port == port and ce_uid == unit_id:
            cjne_entries.append(cjne_entry)

    if not cjne_entries:
        log.debug("No matching cjne sunspec config entries found")
        return (0, [], [])

    migrated = 0
    skipped_loaded: list[str] = []
    errors: list[str] = []

    for cjne_entry in cjne_entries:
        cjne_entry_id = cjne_entry.entry_id
        # Snapshot first - we mutate the registry below.
        candidates = [
            re_entry
            for re_entry in registry.entities.values()
            if re_entry.platform == CJNE_DOMAIN
            and re_entry.config_entry_id == cjne_entry_id
        ]
        for re_entry in candidates:
            old_uid = re_entry.unique_id
            expected_prefix = f"{cjne_entry_id}_"
            if not old_uid.startswith(expected_prefix):
                errors.append(
                    f"{re_entry.entity_id}: unique_id {old_uid!r} does not start "
                    f"with the cjne config entry prefix {expected_prefix!r}"
                )
                continue
            suffix = old_uid[len(expected_prefix):]
            new_uid = f"{entry.entry_id}_{suffix}"

            try:
                registry.async_update_entity_platform(
                    re_entry.entity_id,
                    new_platform="sunspec2",
                    new_config_entry_id=entry.entry_id,
                    new_unique_id=new_uid,
                )
            except ValueError as exc:
                # HA raises this with the exact message
                # "Only entities that haven't been loaded can be migrated"
                # when the entity has a live state in hass.states. That
                # is the cue for "cjne is still running, user needs to
                # uninstall it first".
                if "haven't been loaded" in str(exc):
                    skipped_loaded.append(re_entry.entity_id)
                else:
                    errors.append(f"{re_entry.entity_id}: {exc}")
                continue

            log.info(
                "Migrated %s from cjne sunspec (uid %s -> %s)",
                re_entry.entity_id,
                old_uid,
                new_uid,
            )
            migrated += 1

    return (migrated, skipped_loaded, errors)
