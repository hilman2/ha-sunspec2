"""
Custom integration to integrate SunSpec with Home Assistant.

For more details about this integration, please refer to
https://github.com/cjne/ha-sunspec
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
from .const import STRUCTURE_STORAGE_KEY
from .const import STRUCTURE_STORAGE_VERSION
from .const import TRANSPORT_RTU
from .const import TRANSPORT_TCP
from .coordinator import SunSpec2ConfigEntry
from .coordinator import SunSpecDataUpdateCoordinator
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

# This integration only supports config entries (UI setup), no YAML config.
# CONFIG_SCHEMA tells hassfest about that explicitly so it does not warn.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: Config) -> bool:
    """Set up this integration using YAML is not supported."""
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
    """Set up this integration using UI."""
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
    # flow writes the port name and baudrate into those keys as synthetic
    # coordinates so the logger prefix and the gateway lock have a stable
    # identifier. entry.data is Mapping[str, Any], so the annotations here
    # are what tells a type checker which shape actually arrives.
    host: str = entry.data[CONF_HOST]
    port: int = entry.data[CONF_PORT]
    unit_id: int = entry.data.get(CONF_UNIT_ID, 1)

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
    # #17: how long pysunspec2 pauses between models while walking the
    # SunSpec model tree. Read from options on every setup so changing
    # it in the options flow takes effect on the reload that follows.
    scan_delay = entry.options.get(CONF_SCAN_DELAY, DEFAULT_SCAN_DELAY_SECONDS)
    model_read_delay = entry.options.get(CONF_MODEL_READ_DELAY, DEFAULT_MODEL_READ_DELAY_SECONDS)

    # v0.11.0: Modbus transport selector. Default is TCP so existing
    # config entries (which never had a CONF_TRANSPORT key) keep
    # working without a migration. RTU entries pull the serial-line
    # parameters from data; the synthetic host/port pair is reused
    # so the structured logger and the diagnostics dump still have a
    # stable identifier in their `[host:port#unit_id]` prefix.
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
    # Bronze rule runtime-data: store the coordinator on the typed
    # config entry instead of in hass.data so platforms and the
    # diagnostics dump can read it without a second-level lookup.
    entry.runtime_data = coordinator

    # Hand the API client the model layout an earlier run discovered, if
    # we have one. Nothing trusts it: the next connect re-reads both ends
    # of the chain and rescans if anything moved. What it buys is the
    # first cycle, which is the one that runs inside Home Assistant's
    # setup timeout on the slowest hardware.
    await coordinator.async_load_model_structure()

    # Under the gateway lock, so on a gateway shared by several entries
    # this includes the wait for the entries ahead (#126). The debug line
    # says which part of a slow startup it is.
    started = monotonic()
    await coordinator.async_config_entry_first_refresh()
    log.debug("First refresh took %.1f s", monotonic() - started)

    coordinator.web = await _async_setup_web(hass, entry, coordinator, log)

    # Phase 5 user-value: if the user is migrating from cjne/ha-sunspec
    # and has uninstalled it (entities are orphans in the registry, no
    # live state), retarget those entities to our domain so the user
    # keeps their entity ids and Recorder history. This MUST run before
    # async_forward_entry_setups so any entity_id collisions in the
    # platform setup that follows resolve to the migrated entity.
    _maybe_migrate_from_cjne(hass, entry, log)

    # #17: drop sensor entities the sensor platform no longer builds.
    # Without this they linger in the registry as permanently
    # "Unavailable" rows. Runs
    # after the cjne migration so freshly retargeted orphans from a
    # cjne install that had model 123 ticked are cleaned up in the same
    # pass, and before the platform forward so the removals are done
    # before any entity claims an entity_id.
    cleanup_excluded_sensor_entities(hass, entry, log)
    # #17: and control entities whose model lost to a better one. A
    # device with both 123 and 704 is driven from 704 since v0.19.0, so
    # the 123 Number / Switch entities from an earlier release have
    # nothing feeding them any more.
    cleanup_superseded_control_entities(
        hass, entry, coordinator.detected_models, log, coordinator.vendor
    )

    # Every platform loads on every entry; each builds the controls
    # supported by the models and vendor profile.
    platforms_to_load = list(PLATFORMS)
    # Remember what we forwarded, and unload exactly that. The list
    # used to depend on the beta option, and async_unload_entry
    # re-deriving it from entry.options went wrong because HA writes
    # the new options BEFORE it dispatches the update listener: the
    # Number / Switch entities stayed in the state machine bound to a
    # coordinator that had been replaced. Set before the forward, not
    # after: if the forward raises partway, the recorded list still
    # describes intent.
    coordinator.forwarded_platforms = platforms_to_load
    await hass.config_entries.async_forward_entry_setups(entry, platforms_to_load)

    return True


async def _async_setup_web(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    coordinator: SunSpecDataUpdateCoordinator,
    log: logging.LoggerAdapter[logging.Logger],
) -> FroniusWebCoordinator | None:
    """The web interface coordinator, for a vendor whose web API we speak and a stored login.

    Refreshed once here rather than as a first refresh: a web
    interface that is down makes its entities unavailable and must not
    keep the Modbus entities from loading.
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
    await web.async_refresh()
    if not web.last_update_success:
        log.warning("The web interface did not answer: %s", web.last_exception)
    return web


def _maybe_migrate_from_cjne(
    hass: HomeAssistant, entry: ConfigEntry, log: SunSpecLoggerAdapter
) -> None:
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


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Gold rule stale-devices: allow the user to remove a stale device.

    A SunSpec inverter that drops one of its model blocks (e.g. an
    MPPT module that was physically removed) leaves a "ghost" device
    entry in HA's device registry pointing at a model that no longer
    appears in the scan. Returning True from this callback unlocks
    the trash-can icon next to the device on the device-info page so
    the user can clean it up.

    We never refuse the removal: a SunSpec device is just a thin
    wrapper around (entry_id, model_info_name) and trying to figure
    out programmatically whether a model is "really" gone vs.
    "temporarily missing because the inverter is asleep" is a worse
    user experience than letting the user decide.
    """
    return True


async def async_remove_entry(hass: HomeAssistant, entry: SunSpec2ConfigEntry) -> None:
    """Delete what the entry leaves behind on disk.

    Only the persisted SunSpec model layout, which is a cache keyed by
    entry id. Leaving it would be harmless (it is validated against the
    device before it is ever used) but it would also be litter that
    nothing ever collects.
    """
    store: Store[dict[str, Any]] = Store(
        hass,
        STRUCTURE_STORAGE_VERSION,
        f"{STRUCTURE_STORAGE_KEY}.{entry.entry_id}",
    )
    try:
        await store.async_remove()
    except Exception as err:  # noqa: BLE001 - best effort cleanup
        _LOGGER.debug("Could not remove the stored SunSpec layout: %s", err)


async def async_unload_entry(hass: HomeAssistant, entry: SunSpec2ConfigEntry) -> bool:
    """Handle removal of an entry."""

    _LOGGER.debug("Unload entry %s", entry.entry_id)
    # Unload exactly the platform set async_setup_entry forwarded to,
    # as recorded on the coordinator. Deriving it from entry.options
    # here is wrong - see the comment at the forward site. The fallback
    # covers a coordinator built before this attribute existed.
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
        # Drop any Repairs panel issues this coordinator may have raised.
        # Without this, removing the integration leaves ghost issues
        # in Settings -> Repairs that the user can never clear.
        coordinator._clear_repair_issues(force=True)
        # Close the Modbus connection BEFORE we drop our references. KACO
        # Powador (and likely other inverters) only allow one Modbus TCP
        # connection at a time; without an explicit close here a config
        # entry reload would race the leftover socket against the freshly
        # built one in async_setup_entry, and the new connect would time
        # out. An unload is usually followed by a setup, so the connection
        # goes for good, not just the client on it.
        await coordinator.api.async_shutdown()
        entry.update_listeners.remove(async_reload_entry)
    else:
        # Do not claim success. Returning True here (which is what this
        # did before v0.14.0) makes HA mark the entry NOT_LOADED while
        # the cleanup block above was skipped, so our Modbus socket is
        # still open and the options update listener is still
        # registered. The next setup then races a socket it does not
        # know about, on exactly the single-slot inverters where that
        # hurts most.
        _LOGGER.warning(
            "Unload of entry %s failed for at least one platform; "
            "the Modbus session and update listener are still active",
            entry.entry_id,
        )

    return unloaded


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
