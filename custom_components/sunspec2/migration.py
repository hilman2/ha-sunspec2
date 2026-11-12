"""Entity migration from cjne/ha-sunspec, and removal of entities no platform builds.

When the entity registry still holds the entities of a cjne/ha-sunspec
config entry for the same device, ``migrate_from_cjne_sync`` retargets
them to this integration. The user keeps the entity ids (e.g.
``sensor.inverter_three_phase_watts``), the Recorder history attached to
them, the customisations (icon, area, name) and every reference from
automations, scripts and dashboards.

Both integrations build the unique id as
``{config_entry_id}_{key}-{model_id}-{model_index}``, so the migration
only swaps the entry id in front. The migration breaks if
``get_sunspec_unique_id`` ever builds the id differently.

The rewrite goes through ``EntityRegistry.async_update_entity_platform``,
the only public Home Assistant API that changes ``platform`` on an
existing entity without removing it. Removing the entity would lose the
Recorder history. Home Assistant refuses the call for an entity that a
running platform serves, which it reads from ``entity_sources``. The
entities of a cjne integration that is still loaded are therefore
skipped, and the user has to remove cjne and restart Home Assistant
first. A state left behind by an integration that is gone does not block
the migration.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_HOST
from .const import CONF_PORT
from .const import CONF_UNIT_ID
from .const import is_excluded_sensor_point
from .logger import LoggerLike
from .vendors.profile import VendorProfile

CJNE_DOMAIN = "sunspec"
"""The legacy domain we migrate FROM."""


def _matching_cjne_entries(hass: HomeAssistant, entry: ConfigEntry) -> list[ConfigEntry]:
    """The cjne config entries for the device ``entry`` is set up for.

    A cjne entry is for the same device when its host, port and unit id
    equal those of ``entry``. The host is compared as text, so a cjne
    entry set up with a host name does not match an entry set up with
    the IP address.
    """
    host = entry.data.get(CONF_HOST)
    port = entry.data.get(CONF_PORT)
    unit_id = entry.data.get(CONF_UNIT_ID, 1)
    matching: list[ConfigEntry] = []
    for cjne_entry in hass.config_entries.async_entries(CJNE_DOMAIN):
        # cjne/ha-sunspec renamed the key ``slave_id`` to ``unit_id`` in
        # 2025. An entry that an older cjne release loaded last still
        # carries the old key, so both keys are read.
        cjne_unit_id = cjne_entry.data.get("unit_id") or cjne_entry.data.get("slave_id")
        if (
            cjne_entry.data.get("host") == host
            and cjne_entry.data.get("port") == port
            and cjne_unit_id == unit_id
        ):
            matching.append(cjne_entry)
    return matching


def find_blocking_cjne_entries(hass: HomeAssistant, entry: ConfigEntry) -> list[ConfigEntry]:
    """Return the loaded cjne config entries for the device ``entry`` is set up for.

    A loaded cjne entry keeps its own Modbus TCP session to the
    inverter open, and an inverter that grants one session cannot serve
    this integration next to it. ``async_setup_entry`` refuses the setup
    with ``ConfigEntryNotReady`` while the list is not empty, so Home
    Assistant retries until the user has removed cjne.
    """
    return [
        cjne_entry
        for cjne_entry in _matching_cjne_entries(hass, entry)
        if cjne_entry.state == ConfigEntryState.LOADED
    ]


def migrate_from_cjne_sync(
    hass: HomeAssistant,
    entry: ConfigEntry,
    log: LoggerLike,
) -> tuple[int, list[str], list[str]]:
    """Retarget the orphaned cjne entities of the device of ``entry`` to sunspec2.

    The function is synchronous because every registry helper it calls
    is.

    Returns:
        A three-way tuple (migrated, skipped_loaded, errors): the number
        of entities retargeted, the entity ids a running cjne platform
        still serves, and one message for every entity that could not be
        migrated for another reason, such as a malformed unique id.
    """
    registry = er.async_get(hass)
    cjne_entries = _matching_cjne_entries(hass, entry)
    if not cjne_entries:
        log.debug("No matching cjne sunspec config entries found")
        return (0, [], [])

    migrated = 0
    skipped_loaded: list[str] = []
    errors: list[str] = []

    for cjne_entry in cjne_entries:
        cjne_entry_id = cjne_entry.entry_id
        # A snapshot, because the loop below changes the registry.
        candidates = [
            re_entry
            for re_entry in registry.entities.values()
            if re_entry.platform == CJNE_DOMAIN and re_entry.config_entry_id == cjne_entry_id
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
            suffix = old_uid[len(expected_prefix) :]
            new_uid = f"{entry.entry_id}_{suffix}"

            try:
                registry.async_update_entity_platform(
                    re_entry.entity_id,
                    new_platform="sunspec2",
                    new_config_entry_id=entry.entry_id,
                    new_unique_id=new_uid,
                )
            except ValueError as exc:
                # Home Assistant raises ValueError with the message
                # "Only entities that haven't been loaded can be migrated"
                # for an entity a running platform serves: cjne is still
                # loaded and the user has to remove it first. The message
                # text is the only way to tell that case apart.
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


def cleanup_excluded_sensor_entities(
    hass: HomeAssistant, entry: ConfigEntry, log: LoggerLike
) -> list[str]:
    """Remove the sensor entities of points the sensor platform does not build.

    Home Assistant keeps a registry entry that no platform claims and
    shows it as unavailable for good (#17), so the sensors of excluded
    points have to be removed here.

    Returns the removed entity ids.
    """
    registry = er.async_get(hass)
    removed: list[str] = []

    # A snapshot, because async_remove changes the registry.
    for re_entry in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        # Only sensors. The Number and Switch entities of the same points
        # carry the same model id and key in their unique id
        # (``get_sunspec_unique_id``), so a match on the unique id alone
        # would delete the controls on every setup.
        if re_entry.domain != "sensor":
            continue
        parsed = _parse_sunspec_unique_id(re_entry.unique_id, entry.entry_id)
        if parsed is None:
            continue
        model_id, key = parsed
        # The predicate is the one the sensor platform builds from. A
        # copy of the rule here would delete sensors the platform has
        # started to build again.
        if not is_excluded_sensor_point(model_id, key):
            continue
        registry.async_remove(re_entry.entity_id)
        removed.append(re_entry.entity_id)

    if removed:
        log.info(
            "Removed %d orphaned sensor entities for control points: %s",
            len(removed),
            ", ".join(removed),
        )
    return removed


def _parse_sunspec_unique_id(unique_id: str, entry_id: str) -> tuple[int, str] | None:
    """Split ``{entry_id}_{key}-{model_id}-{model_index}`` into (model_id, key).

    Returns None for a unique id that ``get_sunspec_unique_id`` did not
    write, so the caller leaves that entity alone.
    """
    prefix = f"{entry_id}_"
    if not unique_id.startswith(prefix):
        return None
    rest = unique_id[len(prefix) :]
    # The id is split from the right: the key is the only free-form part
    # (models.py flattens a repeating-group point to ``group:idx:point``),
    # and the two trailing fields are always integers.
    parts = rest.rsplit("-", 2)
    if len(parts) != 3:
        return None
    key, model_id, model_index = parts
    if not key:
        return None
    try:
        int(model_index)
        return int(model_id), key
    except ValueError:
        return None


def cleanup_superseded_control_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    detected_models: set[int],
    log: LoggerLike,
    vendor: VendorProfile | None = None,
) -> list[str]:
    """Remove the SunSpec control entities that no active control spec backs.

    A device with model 123 and model 704 gets its controls from 704
    (``active_specs``), so the model 123 Number and Switch entities that
    an earlier setup registered have nothing feeding them, and Home
    Assistant would show them as unavailable for good. A control the
    vendor profile lists as unsupported goes the same way: the APsystems
    ECU-R has no revert time and no power factor, and entities for them
    only ever read "unknown".

    Nothing is removed while no spec is active, so a failed discovery
    cannot take the entity ids of the controls away.

    Returns the removed entity ids.
    """
    from .write_controls import active_specs

    unsupported = vendor.unsupported_controls if vendor else frozenset()
    live = {(spec.model_id, spec.point_name) for spec in active_specs(detected_models, unsupported)}
    if not live:
        return []

    registry = er.async_get(hass)
    removed: list[str] = []
    for re_entry in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        if re_entry.domain not in ("number", "switch", "select"):
            continue
        parsed = _parse_sunspec_unique_id(re_entry.unique_id, entry.entry_id)
        if parsed is None:
            continue
        model_id, key = parsed
        # Vendor raw controls are independent of the SunSpec control specs.
        if key.startswith("raw:"):
            continue
        if (model_id, key.rsplit(":", 1)[-1]) in live:
            continue
        registry.async_remove(re_entry.entity_id)
        removed.append(re_entry.entity_id)

    if removed:
        log.info(
            "Removed %d control entities superseded by another model: %s",
            len(removed),
            ", ".join(removed),
        )
    return removed
