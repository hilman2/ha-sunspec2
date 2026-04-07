"""
Custom integration to integrate SunSpec with Home Assistant.

For more details about this integration, please refer to
https://github.com/cjne/ha-sunspec
"""

import asyncio
from collections import deque
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.core_config import Config
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

SCAN_INTERVAL = timedelta(seconds=30)

_LOGGER: logging.Logger = logging.getLogger(__package__)


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
                _LOGGER.info(
                    "Migrated 'slave_id' to 'unit_id': %s", new_data["unit_id"]
                )
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
    capture_enabled = entry.options.get(CONF_CAPTURE_RAW, False)

    client = SunSpecApiClient(
        host, port, unit_id, hass, capture_enabled=capture_enabled
    )

    log = get_adapter(host, port, unit_id)
    log.debug("Setup config entry for SunSpec")
    coordinator = SunSpecDataUpdateCoordinator(hass, client=client, entry=entry)
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


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
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


def get_sunspec_unique_id(
    config_entry_id: str, key: str, model_id: int, model_index: int
) -> str:
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
        self._recent_errors: dict[str, deque] = {
            cat: deque(maxlen=20) for cat in CATEGORIES
        }
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
            model_ids = self.option_model_filter & set(
                await self.api.async_get_models()
            )
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
            wrapped = TransportError(
                f"Unclassified: {exc.__class__.__name__}: {exc}"
            )
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
            ir.async_delete_issue(
                self.hass, DOMAIN, f"{self.entry.entry_id}_{category}"
            )
