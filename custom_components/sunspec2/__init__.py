"""Home Assistant integration for SunSpec Modbus devices: setup and unload of a config entry.

Documentation: https://github.com/hilman2/ha-sunspec2
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Any

from homeassistant.components.persistent_notification import (
    async_create as async_create_notification,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.core_config import Config
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .api import SunSpecApiClient
from .const import CONF_BAUDRATE
from .const import CONF_CAPTURE_RAW
from .const import CONF_FRONIUS_WEB_TOKEN
from .const import CONF_HOST
from .const import CONF_MODEL_READ_DELAY
from .const import CONF_PARITY
from .const import CONF_PORT
from .const import CONF_SCAN_DELAY
from .const import CONF_SERIAL_PORT
from .const import CONF_TRANSPORT
from .const import CONF_UNIT_ID
from .const import DEFAULT_BAUDRATE
from .const import DEFAULT_MODEL_READ_DELAY_SECONDS
from .const import DEFAULT_SCAN_DELAY_SECONDS
from .const import DOMAIN
from .const import PARITY_NONE
from .const import PLATFORMS
from .const import STARTUP_MESSAGE
from .const import TRANSPORT_RTU
from .const import TRANSPORT_TCP
from .coordinator import SunSpec2ConfigEntry
from .coordinator import SunSpecDataUpdateCoordinator
from .coordinator import structure_store
from .fronius_web import FroniusWebCoordinator
from .fronius_web import WebToken
from .logger import SunSpecLoggerAdapter
from .logger import get_adapter
from .migration import cleanup_excluded_sensor_entities
from .migration import cleanup_superseded_control_entities
from .migration import find_blocking_cjne_entries
from .migration import migrate_from_cjne_sync
from .services import async_setup_services

_LOGGER: logging.Logger = logging.getLogger(__package__)

# The store of the former usage ping, see async_setup.
_USAGE_STATS_STORAGE_KEY = f"{DOMAIN}.usage_stats"

# The integration is set up from config entries only, without YAML.
# CONFIG_SCHEMA says so to hassfest, which warns otherwise.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: Config) -> bool:
    """Register the actions. There is no YAML configuration to read."""
    # 2026.10.8 to 2026.10.17 sent a daily usage ping and kept a random
    # install id for it in this store. The ping is gone, so the id goes
    # with it. Removing a store that does not exist is a no-op, and a
    # file that cannot be removed must not keep the integration from
    # loading.
    try:
        await Store[Any](hass, 1, _USAGE_STATS_STORAGE_KEY).async_remove()
    except OSError as err:
        _LOGGER.debug("Could not remove the former usage ping store: %s", err)

    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SunSpec2ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    # The flows change data and options and leave the reload to this
    # listener, also while the entry waits to retry a failed setup: a new
    # address entered for an unreachable device must be tried at once.
    # entry.async_on_unload would remove the listener with every failed
    # attempt, so it is added once and removed in async_unload_entry.
    if async_reload_entry not in entry.update_listeners:
        entry.add_update_listener(async_reload_entry)
    if not hass.data.get(f"{DOMAIN}_started"):
        hass.data[f"{DOMAIN}_started"] = True
        _LOGGER.info(STARTUP_MESSAGE)

    # Every entry carries host and port, RTU ones included: the serial
    # flow stores the port name and the baudrate under those keys, so
    # the logger prefix and the gateway lock have a stable identifier.
    # entry.data is Mapping[str, Any], so the annotations here tell a
    # type checker which shape arrives.
    host: str = entry.data[CONF_HOST]
    port: int = entry.data[CONF_PORT]
    unit_id: int = entry.data.get(CONF_UNIT_ID, 1)

    # Refuse to poll while cjne/ha-sunspec is still loaded for the same
    # host, port and unit id. A KACO Powador, like most SunSpec
    # inverters, allows only one Modbus TCP session at a time, and two
    # integrations racing for it produce flapping sensors.
    # ConfigEntryNotReady makes Home Assistant retry until the user has
    # uninstalled cjne.
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

    # No conflict: clear the Repairs issue an earlier setup attempt may have raised.
    ir.async_delete_issue(hass, DOMAIN, f"{entry.entry_id}_cjne_conflict")

    capture_enabled = entry.options.get(CONF_CAPTURE_RAW, False)
    # scan_delay is the pause between two models while the SunSpec model
    # tree is walked (#17).
    scan_delay = entry.options.get(CONF_SCAN_DELAY, DEFAULT_SCAN_DELAY_SECONDS)
    model_read_delay = entry.options.get(CONF_MODEL_READ_DELAY, DEFAULT_MODEL_READ_DELAY_SECONDS)

    # An entry without CONF_TRANSPORT is a TCP entry: entries created
    # before the serial transport existed have no such key.
    transport = entry.data.get(CONF_TRANSPORT, TRANSPORT_TCP)
    if transport == TRANSPORT_RTU:
        serial_port = entry.data.get(CONF_SERIAL_PORT)
        baudrate = entry.data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE)
        parity = entry.data.get(CONF_PARITY, PARITY_NONE)
        client = SunSpecApiClient(
            host=serial_port or "rtu",
            port=baudrate,
            unit_id=unit_id,
            hass=hass,
            capture_enabled=capture_enabled,
            transport=TRANSPORT_RTU,
            serial_port=serial_port,
            baudrate=baudrate,
            parity=parity,
            scan_delay=scan_delay,
            model_read_delay=model_read_delay,
            entry=entry,
        )
        log = get_adapter(serial_port or "rtu", baudrate, unit_id)
    else:
        client = SunSpecApiClient(
            host,
            port,
            unit_id,
            hass,
            capture_enabled=capture_enabled,
            scan_delay=scan_delay,
            model_read_delay=model_read_delay,
            entry=entry,
        )
        log = get_adapter(host, port, unit_id)
    log.debug("Setup config entry for SunSpec")
    coordinator = SunSpecDataUpdateCoordinator(hass, client=client, entry=entry)
    entry.runtime_data = coordinator

    # The stored layout is not trusted: the next connect re-reads both
    # ends of the model chain and rescans if anything moved. The layout
    # saves the scan on the first cycle, the one that runs inside Home
    # Assistant's setup timeout.
    await coordinator.async_load_model_structure()

    # Under the gateway lock, so on a gateway shared by several entries
    # this includes the wait for the entries ahead (#126). The debug line
    # says which part of a slow startup it is.
    started = monotonic()
    await coordinator.async_config_entry_first_refresh()
    log.debug("First refresh took %.1f s", monotonic() - started)

    coordinator.web = await _async_setup_web(hass, entry, coordinator, log)

    # Retarget the orphaned entities of an uninstalled cjne/ha-sunspec
    # to this domain, so the user keeps entity ids and Recorder history.
    # The migration must run before async_forward_entry_setups, so that
    # an entity_id collision in the platform setup resolves to the
    # migrated entity.
    _maybe_migrate_from_cjne(hass, entry, log)

    # Remove sensor entities the sensor platform no longer builds, which
    # would otherwise stay in the registry as "Unavailable" rows (#17).
    # After the cjne migration, so that retargeted orphans of a cjne
    # install with model 123 ticked go in the same pass, and before the
    # platform forward, so that no entity has claimed an entity_id yet.
    cleanup_excluded_sensor_entities(hass, entry, log)
    # Remove the control entities of a model that lost to a better one
    # (#17). A device with both 123 and 704 is driven from 704, so the
    # 123 Number and Switch entities of an earlier release have nothing
    # feeding them.
    cleanup_superseded_control_entities(
        hass, entry, coordinator.detected_models, log, coordinator.vendor
    )

    # Every platform loads on every entry; each builds the controls
    # supported by the models and vendor profile.
    platforms_to_load = list(PLATFORMS)
    # async_unload_entry unloads exactly the recorded list and must not
    # derive it from entry.options: Home Assistant writes new options
    # before it dispatches the update listener, so a list that depends
    # on an option would name the wrong platforms at unload. The list
    # is recorded before the forward, so that it is there even if the
    # forward raises partway.
    coordinator.forwarded_platforms = platforms_to_load
    await hass.config_entries.async_forward_entry_setups(entry, platforms_to_load)

    return True


async def _async_setup_web(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    coordinator: SunSpecDataUpdateCoordinator,
    log: logging.LoggerAdapter[logging.Logger],
) -> FroniusWebCoordinator | None:
    """The web interface coordinator, or None.

    None unless the vendor has a web API this integration speaks, a
    login is stored and the entry is a TCP entry.
    """
    vendor = coordinator.vendor
    token = WebToken.from_dict(entry.options.get(CONF_FRONIUS_WEB_TOKEN))
    if (
        vendor is None
        or vendor.web_user is None
        or token is None
        or entry.data.get(CONF_TRANSPORT, TRANSPORT_TCP) != TRANSPORT_TCP
    ):
        return None
    web = FroniusWebCoordinator(hass, entry, entry.data[CONF_HOST], token)
    # async_refresh rather than a first refresh: a web interface that is
    # down makes its own entities unavailable and must not keep the
    # Modbus entities from loading.
    await web.async_refresh()
    if not web.last_update_success:
        log.warning("The web interface did not answer: %s", web.last_exception)
    return web


def _maybe_migrate_from_cjne(
    hass: HomeAssistant, entry: ConfigEntry, log: SunSpecLoggerAdapter
) -> None:
    """Run the cjne/ha-sunspec entity migration and report its result.

    The result goes to the log and into persistent notifications.
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


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow the user to remove any device of the entry.

    A device stands for one SunSpec model of the entry. A model that
    left the scan, such as a removed MPPT module, leaves its device
    behind. Whether a model is gone for good or only missing while the
    inverter sleeps cannot be told from here, so the removal is never
    refused and the decision stays with the user.
    """
    return True


async def async_remove_entry(hass: HomeAssistant, entry: SunSpec2ConfigEntry) -> None:
    """Delete what the entry leaves behind on disk: the stored SunSpec model layout."""
    try:
        await structure_store(hass, entry.entry_id).async_remove()
    except Exception as err:  # noqa: BLE001 - best effort cleanup
        _LOGGER.debug("Could not remove the stored SunSpec layout: %s", err)


async def async_unload_entry(hass: HomeAssistant, entry: SunSpec2ConfigEntry) -> bool:
    """Unload a config entry. Returns False when a platform did not unload."""

    _LOGGER.debug("Unload entry %s", entry.entry_id)
    # Unload the platforms async_setup_entry recorded on the
    # coordinator; the comment at the forward says why. An empty record
    # falls back to every platform.
    coordinator = entry.runtime_data
    platforms_to_unload = getattr(coordinator, "forwarded_platforms", None) or list(PLATFORMS)
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in platforms_to_unload
            ]
        )
    )
    if unloaded:
        # Stop retries before clearing Repairs or awaiting transport cleanup.
        await coordinator.async_shutdown()
        # Without this, an unloaded or removed entry leaves issues in
        # Settings -> Repairs that the user can never clear.
        coordinator._clear_repair_issues(force=True)
        # Shut the API client down: it drops its Modbus session and
        # withdraws its timeout from the shared connection, and a retry
        # or scan that resumes after the unload can no longer reconnect.
        await coordinator.api.async_shutdown()
        entry.update_listeners.remove(async_reload_entry)
    else:
        # Do not claim success. Returning True makes Home Assistant mark
        # the entry NOT_LOADED although the cleanup above was skipped:
        # the coordinator is not shut down, the API client still holds
        # its Modbus session and the update listener is still
        # registered.
        _LOGGER.warning(
            "Unload of entry %s failed for at least one platform; "
            "the Modbus session and update listener are still active",
            entry.entry_id,
        )

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry after its data or options changed."""
    # The reload goes through Home Assistant's state machine, never
    # through async_unload_entry and async_setup_entry called from here.
    # async_config_entry_first_refresh requires the entry state
    # SETUP_IN_PROGRESS, which hass.config_entries.async_reload sets.
    # Called outside that state it raises ConfigEntryError, and every
    # sensor stays unavailable until Home Assistant restarts.
    await hass.config_entries.async_reload(entry.entry_id)
