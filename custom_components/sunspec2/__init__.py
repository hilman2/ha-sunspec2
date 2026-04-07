"""
Custom integration to integrate SunSpec with Home Assistant.

For more details about this integration, please refer to
https://github.com/cjne/ha-sunspec
"""

import asyncio
import logging
from collections import deque
from datetime import timedelta

from homeassistant.components.persistent_notification import (
    async_create as async_create_notification,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.core_config import Config
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SunSpecApiClient
from .const import CONF_CAPTURE_RAW
from .const import CONF_ENABLED_MODELS
from .const import CONF_HOST
from .const import CONF_PORT
from .const import CONF_SCAN_INTERVAL
from .const import CONF_UNIT_ID
from .const import DEFAULT_MODELS
from .const import DOMAIN
from .const import PLATFORMS
from .const import STARTUP_MESSAGE
from .errors import CATEGORIES
from .errors import SunSpecError
from .errors import TransportError
from .logger import get_adapter
from .migration import find_blocking_cjne_entries
from .migration import migrate_from_cjne_sync

SCAN_INTERVAL = timedelta(seconds=30)

_LOGGER: logging.Logger = logging.getLogger(__package__)

# This integration only supports config entries (UI setup), no YAML config.
# CONFIG_SCHEMA tells hassfest about that explicitly so it does not warn.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: Config) -> bool:
    """Set up this integration using YAML is not supported."""
    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating configuration from version %s", config_entry.version)

    if config_entry.version == 1:
        # Migrate from version 1 to version 2
        # Version 1 used 'slave_id', version 2 uses 'unit_id'
        new_data = {**config_entry.data}

        # Migrate slave_id to unit_id if needed
        if "slave_id" in new_data:
            if "unit_id" not in new_data:
                # No unit_id exists, migrate slave_id to unit_id
                new_data["unit_id"] = new_data.pop("slave_id")
                _LOGGER.info("Migrated 'slave_id' to 'unit_id': %s", new_data["unit_id"])
            else:
                # Both exist, remove slave_id and keep unit_id
                new_data.pop("slave_id")
                _LOGGER.info(
                    "Removed 'slave_id', keeping existing 'unit_id': %s",
                    new_data["unit_id"],
                )

        # Update the config entry with new version and data
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=2)
        _LOGGER.info("Migration to version %s successful", config_entry.version)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(STARTUP_MESSAGE)

    host = entry.data.get(CONF_HOST)
    port = entry.data.get(CONF_PORT)
    unit_id = entry.data.get(CONF_UNIT_ID, 1)

    # Phase 5 conflict guard: refuse to start polling while cjne/ha-sunspec
    # is still actively running for the same host/port/unit_id. KACO Powador
    # (and most SunSpec inverters) only allow ONE Modbus TCP slot at a time.
    # Trying to share it would race against cjne and produce flapping
    # sensors. Raising ConfigEntryNotReady makes HA retry automatically
    # once the user uninstalls cjne and restarts.
    blocking_cjne = find_blocking_cjne_entries(hass, entry)
    if blocking_cjne:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{entry.entry_id}_cjne_conflict",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="cjne_conflict",
            translation_placeholders={
                "host": str(host),
                "port": str(port),
                "unit_id": str(unit_id),
            },
        )
        raise ConfigEntryNotReady(
            f"cjne/ha-sunspec is still loaded for {host}:{port} unit {unit_id}; "
            "uninstall it via HACS and restart Home Assistant"
        )

    # No conflict - clear any leftover Repairs issue from a previous setup
    # attempt that did fail this guard.
    ir.async_delete_issue(hass, DOMAIN, f"{entry.entry_id}_cjne_conflict")

    capture_enabled = entry.options.get(CONF_CAPTURE_RAW, False)

    client = SunSpecApiClient(host, port, unit_id, hass, capture_enabled=capture_enabled)

    log = get_adapter(host, port, unit_id)
    log.debug("Setup config entry for SunSpec")
    coordinator = SunSpecDataUpdateCoordinator(hass, client=client, entry=entry)
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await coordinator.async_config_entry_first_refresh()

    # Phase 5 user-value: if the user is migrating from cjne/ha-sunspec
    # and has uninstalled it (entities are orphans in the registry, no
    # live state), retarget those entities to our domain so the user
    # keeps their entity ids and Recorder history. This MUST run before
    # async_forward_entry_setups so any entity_id collisions in the
    # platform setup that follows resolve to the migrated entity.
    _maybe_migrate_from_cjne(hass, entry, log)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _maybe_migrate_from_cjne(hass: HomeAssistant, entry: ConfigEntry, log) -> None:
    """Run the cjne→sunspec2 entity migration and emit notifications.

    A thin wrapper around migration.migrate_from_cjne_sync that translates
    its return tuple into log lines and persistent notifications. The
    function is intentionally synchronous; every helper it calls is sync
    and HA's persistent_notification.async_create is also a sync callback
    despite the name.
    """
    migrated, skipped, errors = migrate_from_cjne_sync(hass, entry, log)

    if migrated == 0 and not skipped and not errors:
        return  # Quietly: nothing to do

    if migrated > 0:
        log.info("Migrated %d entities from cjne/ha-sunspec", migrated)
        async_create_notification(
            hass,
            (
                f"{migrated} sensor(s) were migrated from the cjne/ha-sunspec "
                "integration to sunspec2. Their entity IDs and Recorder history "
                "have been preserved.\n\n"
                "If you have not done so already, you can now safely uninstall "
                "the cjne/ha-sunspec integration via HACS."
            ),
            title="SunSpec migration complete",
            notification_id=f"sunspec2_migration_{entry.entry_id}",
        )

    if skipped:
        log.warning(
            "%d cjne entities are still loaded (cjne integration is "
            "running) and could not be migrated. Uninstall cjne/ha-sunspec "
            "first.",
            len(skipped),
        )
        affected_list = "\n".join(f"  - {e}" for e in skipped[:10])
        if len(skipped) > 10:
            affected_list += "\n  ..."
        async_create_notification(
            hass,
            (
                f"{len(skipped)} sensor(s) from cjne/ha-sunspec are still "
                "active and could not be migrated to sunspec2.\n\n"
                "To complete the migration:\n"
                "1. Uninstall the cjne/ha-sunspec integration via HACS\n"
                "2. Restart Home Assistant\n"
                "3. Reload the SunSpec 2 integration\n\n"
                f"Affected entities:\n{affected_list}"
            ),
            title="SunSpec migration blocked",
            notification_id=f"sunspec2_migration_blocked_{entry.entry_id}",
        )

    if errors:
        log.error("cjne migration produced errors: %s", errors)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""

    _LOGGER.debug("Unload entry %s", entry.entry_id)
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
            ]
        )
    )
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        # Drop any Repairs panel issues this coordinator may have raised.
        # Without this, removing the integration leaves ghost issues
        # in Settings -> Repairs that the user can never clear.
        coordinator._clear_repair_issues()
        # Close the TCP socket BEFORE we drop our references. KACO Powador
        # (and likely other inverters) only allow one Modbus TCP connection
        # at a time; without an explicit disconnect here a config entry
        # reload would race the leftover socket against the freshly built
        # one in async_setup_entry, and the new connect would time out.
        coordinator.api.close()
        coordinator.unsub()

    return True  # unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry via HA's proper state machine.

    Phase 4 hot-reload bug root cause: the hand-rolled
    ``await async_unload_entry(...); await async_setup_entry(...)`` pattern
    inherited from cjne stopped working in HA 2026.x because
    ``coordinator.async_config_entry_first_refresh()`` (called from
    ``async_setup_entry``) now strictly requires the entry state to be
    ``SETUP_IN_PROGRESS``. Calling ``async_setup_entry`` directly from this
    update listener leaves the entry in ``LOADED`` state, and the
    first-refresh raises ``ConfigEntryError`` and the new coordinator never
    finishes setup - so all sensors stay unavailable until the user
    restarts HA entirely.

    The CLIENT_CACHE refactor in commit ``e508460`` addressed a real
    architectural problem (cross-instance shared state, orphan TCP
    sockets) but it was not the cause of the user-visible "sensors die
    after toggle" symptom. THIS is. The canonical HA pattern is to let
    ``hass.config_entries.async_reload`` drive the state machine instead
    of doing it by hand.
    """
    await hass.config_entries.async_reload(entry.entry_id)


def get_sunspec_unique_id(config_entry_id: str, key: str, model_id: int, model_index: int) -> str:
    """Create a uniqe id for a SunSpec entity"""
    return f"{config_entry_id}_{key}-{model_id}-{model_index}"


class SunSpecDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, client: SunSpecApiClient, entry) -> None:
        """Initialize."""
        self.api = client
        self.hass = hass
        self.entry = entry
        self._log = get_adapter(
            entry.data.get(CONF_HOST),
            entry.data.get(CONF_PORT),
            entry.data.get(CONF_UNIT_ID),
        )
        # Phase-3 per-category buffers. The dict shape ({category: deque})
        # is the contract that diagnostics.py reads. Categories come from
        # errors.CATEGORIES so adding a new category there auto-creates a
        # buffer here. Each deque keeps at most 20 entries (FIFO drop on
        # overflow). Phase 4 may persist these across HA restarts.
        self._recent_errors: dict[str, deque] = {cat: deque(maxlen=20) for cat in CATEGORIES}
        # Counts how many consecutive failures we have observed in each
        # category since the last successful update. Drives the Repairs
        # panel threshold (Phase 3 commit 4): protocol fires at 1, the
        # others at 3. Resets to 0 across the board on the next success.
        self._consecutive_failures: dict[str, int] = {cat: 0 for cat in CATEGORIES}

        self._log.debug("Data: %s", entry.data)
        self._log.debug("Options: %s", entry.options)
        models = entry.options.get(
            CONF_ENABLED_MODELS, entry.data.get(CONF_ENABLED_MODELS, DEFAULT_MODELS)
        )
        scan_interval = timedelta(
            seconds=entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, SCAN_INTERVAL.total_seconds()),
            )
        )
        self.option_model_filter = set(map(lambda m: int(m), models))
        self.unsub = entry.add_update_listener(async_reload_entry)
        self._log.debug(
            "Setup entry with models %s, scan interval %s",
            self.option_model_filter,
            scan_interval,
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=scan_interval,
            config_entry=entry,
        )

    async def _async_update_data(self):
        """Update data via library."""
        self._log.debug("Update data coordinator update")
        data = {}
        try:
            model_ids = self.option_model_filter & set(await self.api.async_get_models())
            self._log.debug("Update data got models %s", model_ids)

            for model_id in model_ids:
                data[model_id] = await self.api.async_get_data(model_id)
            self.api.close()
            # Successful cycle: reset every consecutive-failure counter
            # and clear any active Repairs issues so a recovered inverter
            # disappears from the panel automatically.
            for cat in self._consecutive_failures:
                self._consecutive_failures[cat] = 0
            self._clear_repair_issues()
            return data
        except SunSpecError as exc:
            self._record_error(exc)
            self.api.reconnect_next()
            raise UpdateFailed(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - last-resort safety net
            # Unclassified exception: record as transport (most likely
            # cause for an unexpected failure in the modbus path) and log
            # the full traceback so we know to add an explicit category
            # if this happens repeatedly.
            self._log.exception("Unclassified exception in update loop")
            wrapped = TransportError(f"Unclassified: {exc.__class__.__name__}: {exc}")
            wrapped.__cause__ = exc
            self._record_error(wrapped)
            self.api.reconnect_next()
            raise UpdateFailed(str(exc)) from exc

    def _record_error(self, exc: SunSpecError) -> None:
        """Append a categorised error to the matching ring buffer.

        Bumps the per-category consecutive_failures counter and, if the
        threshold for the category is crossed, raises a Repairs panel
        issue. Thresholds:

          - protocol: 1 (configuration / hardware compatibility problem,
            never a transient state, surface immediately)
          - transport: 3 (transient blips like a brief power glitch
            should not page the user)
          - device:    3 (same reasoning - the inverter may briefly
            return a fault during a state transition)
          - transient: never escalates
        """
        cat = exc.category
        self._recent_errors[cat].append(
            {
                "ts": dt_util.utcnow().isoformat(),
                "type": exc.__class__.__name__,
                "msg": str(exc),
                "cause": str(exc.__cause__) if exc.__cause__ else None,
            }
        )
        self._consecutive_failures[cat] += 1
        self._log.warning(
            "%s (#%d in a row): %s",
            exc.__class__.__name__,
            self._consecutive_failures[cat],
            exc,
        )
        if cat == "transient":
            return
        threshold = 1 if cat == "protocol" else 3
        if self._consecutive_failures[cat] >= threshold:
            self._raise_repair_issue(cat, exc)

    def _raise_repair_issue(self, category: str, exc: SunSpecError) -> None:
        """Create or update the Repairs panel issue for this category.

        Issue id is namespaced per config entry so multi-inverter installs
        do not collapse into a single global issue. Translation key matches
        ``<category>_error`` in translations/<lang>.json (commit 4).
        """
        issue_id = f"{self.entry.entry_id}_{category}"
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=f"{category}_error",
            translation_placeholders={
                "host": str(self.entry.data.get(CONF_HOST, "?")),
                "port": str(self.entry.data.get(CONF_PORT, "?")),
                "unit_id": str(self.entry.data.get(CONF_UNIT_ID, "?")),
                "error": str(exc),
            },
        )

    def _clear_repair_issues(self) -> None:
        """Delete every Repairs issue this coordinator may have raised.

        Called on every successful update cycle (so a recovered inverter
        clears the panel automatically) and on async_unload_entry (so
        removing the integration does not leave ghost issues behind).
        ``transient`` is excluded - it never raises issues to begin with.
        """
        for category in CATEGORIES:
            if category == "transient":
                continue
            ir.async_delete_issue(self.hass, DOMAIN, f"{self.entry.entry_id}_{category}")
