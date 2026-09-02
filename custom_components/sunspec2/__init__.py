"""
Custom integration to integrate SunSpec with Home Assistant.

For more details about this integration, please refer to
https://github.com/cjne/ha-sunspec
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn

from homeassistant.components.persistent_notification import (
    async_create as async_create_notification,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.core import ServiceCall
from homeassistant.core_config import Config
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SunSpecApiClient
from .const import CONF_BAUDRATE
from .const import CONF_CAPTURE_RAW
from .const import CONF_ENABLED_MODELS
from .const import CONF_HOST
from .const import CONF_PARITY
from .const import CONF_PORT
from .const import CONF_REARM_ON_CHANGE
from .const import CONF_RELEASE_SLOT
from .const import CONF_SCAN_DELAY
from .const import CONF_SCAN_INTERVAL
from .const import CONF_SERIAL_PORT
from .const import CONF_STANDBY_WHEN_IDLE
from .const import CONF_TRANSPORT
from .const import CONF_UNIT_ID
from .const import CONF_WRITE_BETA_ENABLED
from .const import DEFAULT_BAUDRATE
from .const import DEFAULT_MODELS
from .const import DEFAULT_SCAN_DELAY_SECONDS
from .const import DEVICE_IDENTITY_POINTS
from .const import DOMAIN
from .const import INTERVAL_RETRY_DELAY_SECONDS
from .const import MIN_SCAN_INTERVAL_SECONDS
from .const import NAMEPLATE_FILTER_HEADROOM
from .const import OPERATING_STATE_LABELS
from .const import OPERATING_STATE_MODEL_IDS
from .const import OPERATING_STATE_POINT
from .const import PARITY_NONE
from .const import PLATFORMS
from .const import PLATFORMS_READ_ONLY
from .const import SERVICE_SET_EXPORT_LIMIT
from .const import STALE_DATA_TOLERANCE_CYCLES
from .const import STALE_MODEL_TOLERANCE_SECONDS
from .const import STANDBY_OPERATING_STATES
from .const import STARTUP_MESSAGE
from .const import STRUCTURE_STORAGE_KEY
from .const import STRUCTURE_STORAGE_VERSION
from .const import TRANSPORT_RTU
from .const import TRANSPORT_TCP
from .const import WRITE_CAPABLE_MODEL_IDS
from .const import WRITE_LOCK_TIMEOUT_SECONDS
from .errors import CATEGORIES
from .errors import SunSpecError
from .errors import TransientError
from .errors import TransportError
from .logger import SunSpecLoggerAdapter
from .logger import get_adapter
from .migration import cleanup_excluded_sensor_entities
from .migration import cleanup_superseded_control_entities
from .migration import find_blocking_cjne_entries
from .migration import migrate_from_cjne_sync
from .models import SunSpecModelWrapper
from .vendors import VendorProfile
from .vendors import plan_write
from .vendors import profile_for

if TYPE_CHECKING:
    # Typing only: discharge_plan imports the coordinator from here.
    from .discharge_plan import DischargePlanner
from .write_controls import export_limit_points

SCAN_INTERVAL = timedelta(seconds=30)

_LOGGER: logging.Logger = logging.getLogger(__package__)

# Bronze rule runtime-data: typed config entry alias so platforms,
# diagnostics and the options flow can read the coordinator off
# ``entry.runtime_data`` with a real type instead of fishing it out
# of ``hass.data[DOMAIN][entry.entry_id]``.
type SunSpec2ConfigEntry = ConfigEntry["SunSpecDataUpdateCoordinator"]

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


async def async_setup_entry(hass: HomeAssistant, entry: SunSpec2ConfigEntry) -> bool:
    """Set up this integration using UI."""
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
        )
        log = get_adapter(serial_port or "rtu", baudrate, unit_id)
    else:
        client = SunSpecApiClient(
            host, port, unit_id, hass, capture_enabled=capture_enabled, scan_delay=scan_delay
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

    await coordinator.async_config_entry_first_refresh()

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
    if entry.options.get(CONF_WRITE_BETA_ENABLED, False):
        cleanup_superseded_control_entities(hass, entry, coordinator.detected_models, log)

    # v0.12.0: forward to the write platforms (number, switch) only
    # when the user has opted in via CONF_WRITE_BETA_ENABLED. The
    # individual number / switch async_setup_entry hooks each do
    # their own check, so this is mostly a "don't even try"
    # optimisation - the platform-level guards are the source of
    # truth.
    write_beta_enabled = entry.options.get(CONF_WRITE_BETA_ENABLED, False)
    platforms_to_load = list(PLATFORMS) if write_beta_enabled else list(PLATFORMS_READ_ONLY)
    # Remember what we forwarded. async_unload_entry must NOT re-derive
    # this from entry.options: HA writes the new options BEFORE it
    # dispatches the update listener, so unticking the beta flag makes
    # the unload read False and unload only the sensor platform. The
    # Number / Switch entities then stay in the state machine bound to
    # a coordinator that has already been replaced, report available
    # forever off its frozen data, and write through its closed api on
    # the next press. Set before the forward, not after: if the forward
    # raises partway, the recorded list still describes intent.
    coordinator.forwarded_platforms = platforms_to_load
    await hass.config_entries.async_forward_entry_setups(entry, platforms_to_load)

    # Register the experimental write service action once per HA
    # process (not per config entry). The handler routes by
    # config_entry_id passed in the call data.
    _async_register_services(hass)

    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the v0.12.0 write service actions, idempotently.

    Service actions are HA-process-global, not per-config-entry, so
    we only have to do this on the first config entry that loads.
    The handlers route to the right entry by reading the
    ``config_entry_id`` field from the service call.
    """
    if hass.services.has_service(DOMAIN, SERVICE_SET_EXPORT_LIMIT):
        return

    async def _async_set_export_limit(call: ServiceCall) -> None:
        entry_id = call.data["config_entry_id"]
        percent = call.data["percent"]
        enable = call.data.get("enable", True)
        target_entry = hass.config_entries.async_get_entry(entry_id)
        if target_entry is None or target_entry.runtime_data is None:
            raise HomeAssistantError(f"SunSpec config entry {entry_id} is not loaded")
        if not target_entry.options.get(CONF_WRITE_BETA_ENABLED, False):
            raise HomeAssistantError(
                "Experimental write controls are not enabled for this entry. "
                "Open the integration options and tick "
                "'Enable experimental write controls (BETA)' first."
            )
        coordinator = target_entry.runtime_data
        # Resolve the model the same way the entities do, so the service
        # and the UI cannot end up driving different registers on a
        # device that exposes both 123 and 704.
        target = export_limit_points(coordinator.detected_models)
        if target is None:
            raise HomeAssistantError(
                "Inverter exposes neither SunSpec model 123 (immediate controls) "
                "nor 704 (DER AC controls), cannot set export limit."
            )
        model_id, percent_point, enable_point = target
        points: list[tuple[str, object]] = [(percent_point, percent)]
        if enable:
            # Same lock hold as the percentage, not a second one. "Set
            # the limit and turn it on" is one logical operation: with
            # two acquisitions a poll cycle or a competing service call
            # can slip in between and leave the inverter running with
            # the new percentage and the old enable state.
            points.append((enable_point, 1))
        try:
            await coordinator.async_write_points_locked(model_id, points)
        except SunSpecError as exc:
            raise HomeAssistantError(f"Failed to set export limit: {exc}") from exc
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_EXPORT_LIMIT,
        _async_set_export_limit,
    )


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
    platforms_to_unload = getattr(coordinator, "forwarded_platforms", None) or list(
        PLATFORMS_READ_ONLY
    )
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in platforms_to_unload
            ]
        )
    )
    if unloaded:
        # Drop any Repairs panel issues this coordinator may have raised.
        # Without this, removing the integration leaves ghost issues
        # in Settings -> Repairs that the user can never clear.
        coordinator._clear_repair_issues(force=True)
        # Close the TCP socket BEFORE we drop our references. KACO Powador
        # (and likely other inverters) only allow one Modbus TCP connection
        # at a time; without an explicit disconnect here a config entry
        # reload would race the leftover socket against the freshly built
        # one in async_setup_entry, and the new connect would time out.
        # force=True: an unload is usually followed by a setup, and the
        # new coordinator must not race a socket the old one left behind.
        # This is one of the few places an RST is the right answer.
        coordinator.api.close(force=True)
        coordinator.unsub()
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


def get_sunspec_unique_id(config_entry_id: str, key: str, model_id: int, model_index: int) -> str:
    """Create a uniqe id for a SunSpec entity"""
    return f"{config_entry_id}_{key}-{model_id}-{model_index}"


class SunSpecDataUpdateCoordinator(DataUpdateCoordinator[dict[int, SunSpecModelWrapper]]):
    """Class to manage fetching data from the API."""

    # Per-gateway asyncio lock used to serialise update cycles from multiple
    # config entries that share the same TCP endpoint (host, port). Several
    # inverters and Modbus TCP gateways - notably SolarEdge - only accept a
    # single TCP connection at a time. Without this lock two coordinators
    # polling different unit IDs behind the same gateway would race each
    # other and produce "connection reset by peer" errors. The lock is
    # held for the entire update cycle, and a shared endpoint is exactly
    # the case that still hands the slot back at the end of every cycle
    # (see ``release_slot_between_polls``), so only one of them holds a
    # TCP session at a time. Single-gateway users see no behavioural
    # change because the lock is always free, and their one session
    # stays open between cycles.
    _GATEWAY_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}

    @classmethod
    def _get_gateway_lock(cls, host: str, port: int) -> asyncio.Lock:
        """Return (and lazily create) the asyncio lock for a (host, port)."""
        key = (host, port)
        lock = cls._GATEWAY_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            cls._GATEWAY_LOCKS[key] = lock
        return lock

    def __init__(
        self, hass: HomeAssistant, client: SunSpecApiClient, entry: SunSpec2ConfigEntry
    ) -> None:
        """Initialize."""
        self.api = client
        self.hass = hass
        self.entry = entry
        # device_info (SunSpec common model 1) is fetched once inside the
        # gateway-locked update cycle and cached here so
        # sensor.async_setup_entry can read it without opening a second
        # Modbus-TCP connection. Opening a second socket outside the lock
        # deadlocks single-slot inverters like KACO Powador - the first
        # connect grabs the slot, the second hits the 60s Home Assistant
        # setup timeout instead of returning.
        self.device_info: SunSpecModelWrapper | None = None
        # What the manufacturer string in model 1 says about the device,
        # beyond SunSpec: None until model 1 has been read, and None for
        # a manufacturer without a profile. See vendors/.
        self.vendor: VendorProfile | None = None
        # The watt values the vendor's battery mode recipes fill in,
        # keyed by vendors.profile.Rate. Kept here rather than on the
        # Number entities because the Select reads all four at once and
        # the registers can hold only what the current mode uses.
        self.storage_setpoints: dict[str, float] = {}
        # The scheduled discharge, built by the first of its entities
        # and shared by the rest. See discharge_plan.py.
        self.discharge_plan: DischargePlanner | None = None
        # Which platforms async_setup_entry actually forwarded. Recorded
        # there and read back by async_unload_entry, because the set
        # depends on the write-beta option and the option may have been
        # unticked in between. See the note at the assignment.
        self.forwarded_platforms: list[str] = []
        # Persisted SunSpec model layout. The store is per config entry so
        # removing the entry can drop the file, and so two inverters never
        # share a cache key.
        self._structure_store: Store[dict[str, Any]] = Store(
            hass,
            STRUCTURE_STORAGE_VERSION,
            f"{STRUCTURE_STORAGE_KEY}.{entry.entry_id}",
        )
        # Revision of the layout currently on disk. Compared against
        # ``api.structure_revision`` after every successful cycle, which
        # is how a rescan that found something new gets written out
        # without diffing the lists.
        self._persisted_structure_revision: int | None = None
        # Device identity (model 1) recorded alongside the persisted
        # layout. Checked once per run against the live device, so a
        # different inverter answering on a recycled IP cannot be read at
        # the previous one's offsets.
        self._persisted_identity: dict[str, Any] | None = None
        self._identity_checked = False
        # Whether the nameplate auto-detection has run. It is a
        # convenience read, so it gets exactly one attempt per run rather
        # than being retried on every cycle for a device that simply does
        # not expose model 120 or 121.
        self._nameplate_probed = False
        self._gateway_lock = self._get_gateway_lock(entry.data[CONF_HOST], entry.data[CONF_PORT])
        self._log = get_adapter(
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_UNIT_ID],
        )
        # Phase-3 per-category buffers. The dict shape ({category: deque})
        # is the contract that diagnostics.py reads. Categories come from
        # errors.CATEGORIES so adding a new category there auto-creates a
        # buffer here. Each deque keeps at most 20 entries (FIFO drop on
        # overflow). Phase 4 may persist these across HA restarts.
        self._recent_errors: dict[str, deque[dict[str, Any]]] = {
            cat: deque(maxlen=20) for cat in CATEGORIES
        }
        # Counts how many consecutive failures we have observed in each
        # category since the last successful update. Drives the Repairs
        # panel threshold (Phase 3 commit 4): protocol fires at 1, the
        # others at 3. Resets to 0 across the board on the next success.
        self._consecutive_failures: dict[str, int] = {cat: 0 for cat in CATEGORIES}
        # Counts how many consecutive scheduled update cycles have failed
        # (after the in-cycle retry was already exhausted). Drives the
        # entity-side stale-data tolerance: as long as this stays at or
        # below STALE_DATA_TOLERANCE_CYCLES, sensors keep serving the
        # last successfully read value via SunSpecEntity.available
        # instead of flipping to "unavailable" on every transient blip.
        self.consecutive_failed_cycles: int = 0
        # Set of model IDs the inverter actually exposes, populated by
        # the first successful update cycle. The options-flow form reads
        # this to render its model multi-select - it must NOT call
        # ``api.known_models()`` directly because that returns ``[]``
        # whenever ``api._client`` is ``None``. That used to be the
        # steady state between cycles; since v0.22.0 the session is held
        # open, so ``None`` is only left before the first cycle, after a
        # failed one, and between cycles on an entry that hands the slot
        # back. A v0.7.3 -> v0.7.5
        # regression where the form rendered an empty multi-select and
        # silently saved ``models_enabled: []`` (killing every sensor)
        # was the motivating bug.
        self.detected_models: set[int] = set()
        # Auto-detected nameplate AC power, in kW. Populated on the first
        # successful update cycle by reading SunSpec model 120 ("WRtg" -
        # continuous AC power output capability) with model 121 ("WMax",
        # the configured max output power) as a fallback. The options
        # flow uses this as a suggested_value for CONF_MAX_AC_POWER_KW
        # so users do not have to type their inverter's nameplate by
        # hand. ``None`` means the inverter does not expose either
        # model and we have nothing to suggest.
        self.detected_max_ac_power_kw: float | None = None
        # Which register the nameplate above came from, e.g. "model 120
        # WRtg". Reported in diagnostics and named in the rejection log
        # line, because "configured peak" on a value nobody configured
        # sent the #45 reporter looking in the wrong place.
        self.detected_max_ac_power_source: str | None = None
        # Per-model "first cycle that stopped seeing it" timestamp (cjne
        # issue #202). Once a model has been seen, the first cycle that
        # does NOT see it records the time; a successful re-detection
        # clears the entry. When a model has been gone for
        # ``STALE_MODEL_TOLERANCE_SECONDS`` we raise a Repairs issue
        # suggesting the user remove the related device entry, because
        # the inverter has stopped exposing that model entirely (typical
        # for SMA Tripower X12 with DER 714 after a firmware update).
        # The decision to actually remove the device stays with the user.
        #
        # Wall-clock rather than a cycle counter: the model tree is only
        # rescanned when the cached layout stops validating, so counting
        # cycles would mean counting events that mostly cannot observe
        # the thing being counted.
        self._model_missing_since: dict[int, datetime] = {}
        # Per-cycle scratch sets populated by _run_one_update_cycle
        # and consumed by _after_successful_cycle. They live as
        # instance state instead of being passed through the call
        # chain because that chain crosses the in-cycle retry, which
        # would have to thread the diff between two attempts.
        self._missing_this_cycle: set[int] = set()
        self._new_this_cycle: set[int] = set()

        # Issue #52: the inverter operating state (SunSpec "St") read by
        # the last successful cycle, or None if the device does not
        # expose a model that carries it. Recorded on the way out of a
        # good cycle precisely because it has to outlive the connection:
        # once the inverter powers its comms board down, the last thing
        # it said before going quiet is the only evidence we have that
        # the silence was its own decision.
        self._last_operating_state: int | None = None
        # Opt-out for devices whose shutdown this cannot observe. See
        # CONF_STANDBY_WHEN_IDLE. Read once here rather than per cycle
        # because changing an option reloads the entry anyway.
        self.standby_when_idle: bool = bool(entry.options.get(CONF_STANDBY_WHEN_IDLE, False))

        self._log.debug("Data: %s", entry.data)
        self._log.debug("Options: %s", entry.options)
        models = entry.options.get(
            CONF_ENABLED_MODELS, entry.data.get(CONF_ENABLED_MODELS, DEFAULT_MODELS)
        )
        # Defense in depth: a previously corrupted options save (see the
        # detected_models comment above) could persist ``models_enabled: []``
        # to disk. Without this fallback the coordinator would happily
        # poll zero models on every cycle and the user would see all
        # sensors disappear. Fall back to DEFAULT_MODELS so the user gets
        # *something* to look at while they re-open the options form.
        if not models:
            self._log.warning(
                "Configured models filter is empty, falling back to defaults. "
                "Re-open the options form and pick the models you want."
            )
            models = DEFAULT_MODELS
        # Second line of defence behind the config flow's range check.
        # An entry saved before that check existed, or hand-edited in
        # core.config_entries, can still carry a 0 or a negative value,
        # and neither survives contact with HA's coordinator: 0 is falsy
        # so update_interval becomes None and polling stops silently and
        # forever, while a negative value puts the next refresh in the
        # past and hot-loops it. See MIN_SCAN_INTERVAL_SECONDS. Clamping
        # here means an affected install repairs itself on the next
        # reload instead of needing the user to notice and re-save.
        configured_interval = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, SCAN_INTERVAL.total_seconds()),
        )
        try:
            interval_seconds = float(configured_interval)
        except (TypeError, ValueError):
            interval_seconds = SCAN_INTERVAL.total_seconds()
        if interval_seconds < MIN_SCAN_INTERVAL_SECONDS:
            self._log.warning(
                "Configured scan interval %s is below the %s second minimum and would "
                "stop polling instead of speeding it up. Using %s seconds; set a valid "
                "interval in the integration options.",
                configured_interval,
                MIN_SCAN_INTERVAL_SECONDS,
                MIN_SCAN_INTERVAL_SECONDS,
            )
            interval_seconds = MIN_SCAN_INTERVAL_SECONDS
        scan_interval = timedelta(seconds=interval_seconds)
        self.option_model_filter = set(map(lambda m: int(m), models))
        # #17: model 123 has to be polled whenever the experimental
        # write controls are on, even if the user never ticked 123 in
        # the model multi-select. number.py and switch.py read the
        # current setpoint from ``coordinator.data[123]``; without the
        # model in the polled set they bail out at
        # ``coordinator.data.get(...) is None`` and no write entity ever
        # registers, so ticking the beta flag looks like it did nothing.
        #
        # Deliberately a SEPARATE set from option_model_filter.
        # option_model_filter mirrors exactly what the user picked and
        # is what the options form round-trips, so folding 123 into it
        # would render a tick the user never made and then persist it
        # on their next save.
        self.write_model_filter: set[int] = (
            set(WRITE_CAPABLE_MODEL_IDS)
            if entry.options.get(CONF_WRITE_BETA_ENABLED, False)
            else set()
        )
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

    @property
    def release_slot_between_polls(self) -> bool:
        """Whether to hand the inverter's Modbus session back after each poll.

        False is the normal case, and it is a change of position. The
        integration used to disconnect after every cycle so a
        single-slot inverter would be free for other readers between
        polls. Measured against the hardware that motivated that design,
        a KACO Powador 7.8 TL3 at a 30 s interval, it is what breaks it:
        reconnecting per poll failed 5 of 6 cycles, holding one session
        served 20 of 20 at a steady 1.6 s. Modbus TCP is built around a
        session that stays up, and an embedded stack rebuilding one
        every 30 seconds is the thing it handles worst.

        It stays True for the two cases where somebody else genuinely
        needs the slot:

        * more than one config entry behind the same endpoint, which is
          the SolarEdge-style gateway the per-gateway lock already
          serialises. Detected here rather than configured, because the
          user has no reason to know it matters.
        * the CONF_RELEASE_SLOT option, for a reader outside Home
          Assistant that cannot be put behind a Modbus proxy.
        """
        if self.entry.options.get(CONF_RELEASE_SLOT, False):
            return True
        return self._gateway_is_shared()

    def _gateway_is_shared(self) -> bool:
        """True if another config entry talks to the same endpoint."""
        host = self.entry.data.get(CONF_HOST)
        port = self.entry.data.get(CONF_PORT)
        seen = 0
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_HOST) == host and entry.data.get(CONF_PORT) == port:
                seen += 1
                if seen > 1:
                    return True
        return False

    async def async_load_model_structure(self) -> None:
        """Hand the API client the layout an earlier run discovered.

        Called once, before the first refresh. A missing or unreadable
        store is not an error: it just means the next connect scans, so
        every failure here is swallowed at debug level.
        """
        try:
            payload = await self._structure_store.async_load()
        except Exception as err:  # noqa: BLE001 - a cache read must never break setup
            self._log.debug("Could not read the stored SunSpec layout: %s", err)
            return
        if not isinstance(payload, dict):
            return
        structure = payload.get("structure")
        if not self.api.import_model_structure(structure):
            return
        self._persisted_structure_revision = self.api.structure_revision
        identity = payload.get("identity")
        self._persisted_identity = identity if isinstance(identity, dict) else None
        self._log.debug(
            "Loaded a stored SunSpec layout with %d model(s)",
            len(structure.get("models", [])) if isinstance(structure, dict) else 0,
        )

    async def _async_save_model_structure(self) -> None:
        """Write the layout out when a scan produced something new.

        Runs after a successful cycle, so what gets stored is a layout
        that has just been read from end to end without an error. Cheap
        to call on every cycle: it compares one integer and one small
        dict and returns. The identity is part of the comparison so a
        firmware update whose rescan found the same layout still gets
        its new version string written out, instead of being re-reported
        on every restart.
        """
        revision = self.api.structure_revision
        identity = self._device_identity()
        if revision == self._persisted_structure_revision and identity == self._persisted_identity:
            return
        payload = self.api.export_model_structure()
        if payload is None:
            return
        payload = {"structure": payload, "identity": identity}
        try:
            await self._structure_store.async_save(payload)
        except Exception as err:  # noqa: BLE001 - a cache write must never break a poll
            self._log.debug("Could not store the SunSpec layout: %s", err)
            return
        self._persisted_structure_revision = revision
        self._persisted_identity = identity
        self._log.debug("Stored the SunSpec layout at revision %d", revision)

    async def async_remove_stored_model_structure(self) -> None:
        """Drop the store file. Called when the config entry is removed."""
        try:
            await self._structure_store.async_remove()
        except Exception as err:  # noqa: BLE001 - best effort cleanup
            self._log.debug("Could not remove the stored SunSpec layout: %s", err)

    def _device_identity(self) -> dict[str, Any] | None:
        """Manufacturer / model / version / serial, out of SunSpec model 1.

        Stored next to the layout so a different device answering on a
        recycled IP is caught even in the case the address checks cannot
        see: another inverter of a different make whose model tree
        happens to start the same way.
        """
        if self.device_info is None:
            return None
        identity = {}
        for point in ("Mn", "Md", "Vr", "SN"):
            try:
                value = self.device_info.getValue(point)
            except Exception:  # noqa: BLE001 - identity is a nice-to-have
                continue
            if value is not None:
                identity[point] = str(value)
        return identity or None

    async def _check_device_identity(self) -> bool:
        """Compare the live device against the identity we stored.

        Returns True when the model layout should be rescanned after
        this cycle, which is the firmware-update case: same
        manufacturer, model and serial, different version string. A
        firmware update is the one event that can legitimately change a
        SunSpec model tree, so the cached layout is rescanned once, but
        the device is still the device and the cycle runs through. Until
        v0.26.0 the version was part of the identity, so a routine
        firmware update was treated as a device swap (#49).

        Raises :class:`TransientError` when manufacturer, model or
        serial differ, which fails the cycle and forces a fresh scan on
        the next one. Before raising it deletes the store file. That is
        not optional: this check runs on the first cycle of a run, and
        the first cycle is the one ``async_config_entry_first_refresh``
        drives, which has no in-cycle retry. Its failure is
        ``ConfigEntryNotReady``, and Home Assistant retries that by
        building a NEW coordinator, which loads the store again and hits
        the same mismatch. Clearing ``_persisted_identity`` on this
        object did nothing for that next coordinator, so the integration
        looped on "belongs to a different device, rescanning" forever
        (#49), and the same loop would have blocked a genuinely replaced
        inverter for good.

        Runs once per run: a device does not change identity underneath
        a live connection, and re-reading model 1 every cycle to prove it
        would cost a Modbus round trip for nothing.
        """
        if self._identity_checked:
            return False
        self._identity_checked = True
        stored = self._persisted_identity
        if not stored:
            return False
        current = self._device_identity()
        if not current or current == stored:
            return False
        stored_device = {k: v for k, v in stored.items() if k in DEVICE_IDENTITY_POINTS}
        current_device = {k: v for k, v in current.items() if k in DEVICE_IDENTITY_POINTS}
        if stored_device == current_device:
            self._log.info(
                "Firmware version changed (%s -> %s) since the SunSpec layout was stored. "
                "Rescanning the model tree once, in case the update changed it.",
                stored.get("Vr"),
                current.get("Vr"),
            )
            self._persisted_identity = None
            return True
        self._log.warning(
            "The device at this address is not the one the stored SunSpec layout came from "
            "(%s vs %s). Discarding the layout and rescanning.",
            stored,
            current,
        )
        self._persisted_identity = None
        self._persisted_structure_revision = None
        await self.async_remove_stored_model_structure()
        self.api.reconnect_next()
        raise TransientError("Stored SunSpec layout belongs to a different device, rescanning")

    async def _async_update_data(self) -> dict[int, SunSpecModelWrapper]:
        """Update data via library, with one in-cycle retry on failure.

        Inverters and Modbus TCP gateways have famously flaky network
        connectivity. A single fast retry catches most one-shot blips
        before HA marks the coordinator as failed and the entity flips
        to "unavailable". The retry only kicks in once at least one
        cycle has succeeded - first-refresh failures fall straight
        through to ConfigEntryNotReady so HA's standard exponential
        backoff can take over instead of having every setup attempt
        block for an extra ``INTERVAL_RETRY_DELAY_SECONDS``.

        The whole update cycle is held under the per-gateway lock (see
        ``_GATEWAY_LOCKS``), including the connect and the close on the
        cycles that still do one. The lock is released across the retry
        sleep so other coordinators sharing the same TCP endpoint can
        poll in the meantime.
        """
        self._log.debug("Update data coordinator update")
        first_err: BaseException | None = None
        try:
            async with self._gateway_lock:
                data = await self._run_one_update_cycle()
            result = self._after_successful_cycle(data)
            await self._async_save_model_structure()
            return result
        except Exception as exc:  # noqa: BLE001 - dispatched below
            first_err = exc

        # First refresh: no prior data exists, no point sleeping for
        # an in-cycle retry. Fail fast and let HA handle the retry via
        # ConfigEntryNotReady's exponential backoff.
        if self.data is None:
            return self._after_failed_cycle(first_err)

        # Issue #52: this line fires once per poll, so on an inverter
        # that sleeps through the night it is the single loudest thing
        # in the log. Keep it at warning while the silence is
        # unexplained, drop it to debug once the device has told us it
        # was shutting down.
        retry_log = self._log.debug if self._downtime_is_expected() else self._log.warning
        retry_log(
            "Update cycle failed (%s: %s); retrying in %ds",
            first_err.__class__.__name__,
            first_err,
            INTERVAL_RETRY_DELAY_SECONDS,
        )
        # Force a fresh client on the next attempt; sleep WITHOUT the
        # gateway lock so other coordinators on the same gateway can
        # use the slot during the wait.
        self.api.reconnect_next()
        await asyncio.sleep(INTERVAL_RETRY_DELAY_SECONDS)
        try:
            async with self._gateway_lock:
                data = await self._run_one_update_cycle()
        except Exception as second_err:  # noqa: BLE001 - dispatched below
            return self._after_failed_cycle(second_err)
        result = self._after_successful_cycle(data)
        await self._async_save_model_structure()
        return result

    def _point_reads_on(self, model_id: int, point: str) -> bool:
        """Whether the last poll reported an enable point as on."""
        if self.data is None:
            return False
        wrapper = self.data.get(model_id)
        if wrapper is None:
            return False
        try:
            return bool(wrapper.getValue(point) == 1)
        except KeyError:
            return False

    async def async_write_points_locked(
        self, model_id: int, points: list[tuple[str, object]]
    ) -> None:
        """Write one or more points while holding the per-gateway lock.

        Every write must go through here. Before v0.14.0 the Number and
        Switch entities and the ``set_export_limit`` service called
        ``coordinator.api.async_write_point`` directly, and a write
        never took the gateway lock at all. Two silent failure modes:

        * **Mid-cycle**: the write got the coordinator's *live* client
          back from ``api.get_client()`` and drove ``point.write()`` on
          it from a second executor thread while ``read_model`` was
          mid-``recv`` on the same socket. The Modbus client takes no
          lock of its own, so both threads pull frames off the same
          socket. (Until v0.31.0 every frame carried transaction id 0
          and each side silently took the other's answer; the embedded
          transport now drops a frame with the wrong id, so both reads
          end in a timeout instead. Either way the lock is required.)
          This is not a single-slot-inverter problem; frames can
          interleave on any gateway.
        * **Between cycles**: ``api._client`` was None, because back
          then ``_run_one_update_cycle`` closed at the end of every
          cycle, so the write opened a *second* TCP session (connect
          plus a full scan) that nothing ever closed, holding a
          single-slot inverter's only Modbus slot until the next cycle.
          v0.13.4's ``model.read()`` widened that window by one more
          block read. Since v0.22.0 the session is normally held open
          and a write finds the live client, but an entry that hands
          the slot back still opens one of its own here, which is why
          this method closes it under the lock (see the ``finally``
          below).

        The method is named ``_locked`` rather than mirroring
        ``SunSpecApiClient.async_write_point`` on purpose. An identical
        name one layer up would make the call-site diff a bare deletion
        of ``.api``, which is the least reviewable possible shape for a
        race-condition guard and the easiest to regress by accident.

        The timeout wraps only the acquire. Cancelling a write already
        in flight would hand control back to the caller while the
        executor thread is still writing to the socket - executor jobs
        are not cancellable - which is the exact race we are closing.

        ``async_request_refresh()`` deliberately stays at the call
        sites, OUTSIDE this lock. ``asyncio.Lock`` is not reentrant and
        ``REQUEST_REFRESH_DEFAULT_IMMEDIATE`` is True, so the debouncer
        runs ``_async_refresh`` inline and it would deadlock on the
        lock we are still holding.
        """
        try:
            async with asyncio.timeout(WRITE_LOCK_TIMEOUT_SECONDS):
                await self._gateway_lock.acquire()
        except TimeoutError as exc:
            raise TransientError(
                f"Timed out after {WRITE_LOCK_TIMEOUT_SECONDS}s waiting for the Modbus "
                f"gateway to become free; the inverter is busy, try again"
            ) from exc
        try:
            # One step for everyone but a vendor that takes a new value
            # only on the rising edge of its enable register; then three
            # (see plan_write). Each step is one batch, not one call per
            # point: the API layer flushes a batch together so adjacent
            # registers go out in a single Modbus frame and the inverter
            # cannot act on half of them. The pause between the steps
            # stays under the lock, so no poll or other write lands
            # inside the sequence.
            steps = plan_write(
                self.vendor,
                model_id,
                points,
                self._point_reads_on,
                self.entry.options.get(CONF_REARM_ON_CHANGE, False),
            )
            for step in steps:
                await self.api.async_write_points(model_id, step.points)
                if step.settle_seconds > 0:
                    await asyncio.sleep(step.settle_seconds)
        finally:
            # Whoever opens a session under the lock closes it under the
            # lock. Leaving the socket open past the release would let
            # the next coordinator on this gateway win the lock
            # (asyncio.Lock is FIFO, so a queued waiter is handed the
            # lock on release) and then fail to connect, which surfaces
            # as a bogus TransportError in an unrelated config entry -
            # the hardest possible shape to diagnose.
            if self.release_slot_between_polls:
                self.api.close()
            self._gateway_lock.release()

    async def _run_one_update_cycle(self) -> dict[int, SunSpecModelWrapper]:
        """Single read attempt over the live session. Caller holds the gateway lock.

        Connects only when there is no live session: the first cycle,
        the cycle after a failure, and every cycle on an entry that
        hands the slot back (see ``release_slot_between_polls``), which
        is also the only case that closes at the end.

        Returns the freshly-read data dict on success and re-raises any
        exception untouched on failure - bookkeeping (error categorisation,
        Repairs issues, failure counters) is the caller's job so the
        in-cycle retry can swallow a transient first failure without
        inflating the per-category thresholds.
        """
        data = {}
        all_models = set(await self.api.async_get_models())
        # Track which previously-known models have just gone missing
        # (cjne issue #202). The repair-issue check happens in
        # _after_successful_cycle once the rest of the cycle is
        # confirmed good, so we don't escalate on a partial scan.
        previously_known = self.detected_models
        missing = previously_known - all_models
        if missing and self.api.last_scan_was_partial:
            # The scan stopped early and the API layer had no usable
            # cached layout to fall back on, so this list is short
            # through no fault of the device. Serving it would drop the
            # affected entities to "unknown" on a cycle that counts as a
            # success, which means the stale-data tolerance never gets
            # to hold the last good value and the user sees a gap that
            # looks exactly like a connection drop (#42). Failing the
            # cycle keeps the last good values on screen and gets a
            # fresh scan on the retry.
            raise TransientError(
                f"SunSpec scan was incomplete: model(s) {sorted(missing)} were not read this cycle"
            )
        self._missing_this_cycle = missing
        self._new_this_cycle = all_models - previously_known
        # Cache the full set of models the inverter exposes so the
        # options-flow form can render its multi-select even when
        # ``api._client`` is gone and ``api.known_models()`` would
        # return an empty list: before the first cycle, after a failed
        # one, or between cycles on an entry that hands the slot back.
        self.detected_models = all_models
        # Union first, intersect second: a model the device does not
        # expose is still never read, but the write-beta model gets in
        # without polluting the user's own selection.
        model_ids = (self.option_model_filter | self.write_model_filter) & all_models
        self._log.debug("Update data got models %s", model_ids)

        # Fetch common model 1 once per process under the lock so
        # the sensor platform setup can read device metadata
        # without opening a second TCP slot. Re-reading it on
        # every cycle would be wasteful - the device info never
        # changes for a given physical inverter.
        if self.device_info is None:
            self.device_info = await self.api.async_get_data(1)
            try:
                manufacturer = self.device_info.getValue("Mn")
            except (KeyError, AttributeError):
                manufacturer = None
            self.vendor = profile_for(manufacturer if isinstance(manufacturer, str) else None)
            if self.vendor is not None:
                self._log.info("Vendor profile %s applies to this device", self.vendor.slug)
        # Only meaningful once model 1 has been read, and it is read on
        # the first cycle, so this lands exactly where it can act.
        rescan_after_cycle = await self._check_device_identity()

        # Auto-detect the inverter's nameplate AC power once, on the
        # first cycle that reaches this point. Prefer model 120
        # (Inverter Nameplate, "WRtg" = continuous output capability)
        # which is the canonical SunSpec field for this. Fall back to
        # model 121 (Inverter Settings, "WMax" = currently configured
        # max output) only if 120 is missing - 121 reflects whatever
        # the installer set, which is usually but not always the
        # nameplate. Both reads are guarded by individual try/excepts
        # so a flaky model read never breaks the whole update cycle.
        if self.detected_max_ac_power_kw is None and not self._nameplate_probed:
            self._nameplate_probed = True
            self.detected_max_ac_power_kw = await self._read_nameplate(all_models)

        for model_id in model_ids:
            data[model_id] = await self.api.async_get_data(model_id)
        if rescan_after_cycle:
            # Firmware changed: hand this cycle's data back as read, and
            # let the NEXT connect walk the model tree again. Flagging it
            # here, after the reads, rather than inside the identity
            # check keeps the rebuild out of the middle of a cycle whose
            # model list was computed against the old client.
            self.api.reconnect_next()
        if self.release_slot_between_polls:
            self.api.close()
        return data

    async def _read_nameplate(self, all_models: set[int]) -> float | None:
        """Read continuous AC power capability from model 120 / 121.

        Returns the nameplate in kW, or ``None`` if neither model is
        present or both reads failed. Errors are swallowed (logged at
        debug level only) so a missing-model condition never escalates
        to an UpdateFailed - the auto-detection is a convenience, not
        a hard requirement.
        """
        for model_id, point_name, label in (
            (120, "WRtg", "model 120 WRtg"),
            (121, "WMax", "model 121 WMax"),
        ):
            if model_id not in all_models:
                continue
            try:
                wrapper = await self.api.async_get_data(model_id)
                value = wrapper.getValue(point_name)
            except TransientError:
                # A read timeout is the one failure this must not
                # swallow. A device that did not answer this read will
                # not answer the model reads that follow either, and each
                # of those waits the full socket timeout. Failing here
                # saves the cycle those waits and starts the reconnect
                # at once.
                raise
            except Exception as exc:  # noqa: BLE001 - convenience read, never escalate
                self._log.debug(
                    "Auto-detect nameplate from %s failed (%s), trying next",
                    label,
                    exc,
                )
                continue
            if isinstance(value, (int, float)) and value > 0:
                kw = float(value) / 1000.0
                self.detected_max_ac_power_source = label
                self._log.info(
                    "Auto-detected inverter nameplate AC power: %.2f kW (from %s). "
                    "Plausibility ceiling for active power: %.2f kW (x%s headroom)",
                    kw,
                    label,
                    kw * NAMEPLATE_FILTER_HEADROOM,
                    NAMEPLATE_FILTER_HEADROOM,
                )
                return kw
        return None

    def _after_successful_cycle(
        self, data: dict[int, SunSpecModelWrapper]
    ) -> dict[int, SunSpecModelWrapper]:
        """Reset failure bookkeeping after a successful read."""
        # Silver rule log-when-unavailable: emit a single recovery
        # log line when we come back from an unavailable run, so
        # the user can correlate "the sensor recovered" with a
        # specific moment in their HA log without having to grep
        # through every successful debug line.
        if self.consecutive_failed_cycles > 0:
            # Issue #52: an inverter that announced its own shutdown and
            # then answered again has not recovered from anything, it
            # woke up. Same line either way, but not at a level that
            # reads like an incident in the log. Evaluated before
            # _last_operating_state is refreshed below, because the
            # evidence is the state from *before* the silence.
            recovery_log = self._log.info if self._downtime_is_expected() else self._log.warning
            recovery_log(
                "Inverter recovered after %d failed update cycle(s)",
                self.consecutive_failed_cycles,
            )
        self.consecutive_failed_cycles = 0
        for cat in self._consecutive_failures:
            self._consecutive_failures[cat] = 0
        self._last_operating_state = self._read_operating_state(data)
        self._clear_repair_issues()
        # cjne issue #200: log new models that just appeared so the
        # user can correlate "wait, my inverter has new sensors now"
        # with a specific moment.
        if self._new_this_cycle:
            self._log.info(
                "Detected %d new SunSpec model(s) on this cycle: %s",
                len(self._new_this_cycle),
                sorted(self._new_this_cycle),
            )
            # Clear the missing-since stamp for re-appearing models so
            # a flapping device doesn't accumulate.
            for mid in self._new_this_cycle:
                self._model_missing_since.pop(mid, None)
        # cjne issue #202: track how long each model has been missing
        # and raise a Repairs issue once a previously-known model has
        # been gone for STALE_MODEL_TOLERANCE_SECONDS. Models that
        # re-appear get their stamp cleared.
        self._update_stale_model_tracking()
        return data

    def _read_operating_state(self, data: dict[int, SunSpecModelWrapper]) -> int | None:
        """Return the inverter operating state from a fresh data dict.

        Issue #52. ``data`` is {model_id: SunSpecModelWrapper}, and only
        the models in OPERATING_STATE_MODEL_IDS are consulted - see the
        note there about model 701, whose identically-named point means
        something else entirely.

        Returns None when the device exposes no such model, when the
        user filtered it out of the polled set, or when the point is
        there but unimplemented (``cvalue`` is None). None simply means
        "no evidence", and the caller treats it as "not standby".
        """
        for model_id in OPERATING_STATE_MODEL_IDS:
            wrapper = data.get(model_id)
            if wrapper is None:
                continue
            try:
                value = wrapper.getValue(OPERATING_STATE_POINT)
            except (KeyError, IndexError, TypeError, AttributeError):
                # A model instance without the point, or a wrapper
                # shape we did not expect. Not worth failing a cycle
                # over a convenience read.
                continue
            if isinstance(value, int):
                return value
        return None

    def _downtime_is_expected(self) -> bool:
        """Whether the inverter being unreachable right now is normal.

        Issue #52. True when the device told us it was on its way down
        (last successful read reported OFF / SLEEPING / SHUTTING_DOWN /
        STANDBY), or when the user ticked CONF_STANDBY_WHEN_IDLE for a
        device whose shutdown we cannot observe.

        Only the transport category consults this. Anything that got an
        answer out of the inverter proves it is awake, and an awake
        inverter misbehaving is still worth a repair issue.
        """
        if self.standby_when_idle:
            return True
        return self._last_operating_state in STANDBY_OPERATING_STATES

    def _update_stale_model_tracking(self) -> None:
        """Stamp newly-missing models and surface long-gone ones."""
        now = dt_util.utcnow()
        # Stamp models that should still be there but weren't seen this
        # cycle. setdefault, not assignment: the stamp records when the
        # model went missing, so re-stamping it on every later cycle
        # would keep resetting the clock and the threshold would never
        # be reached.
        for mid in self._missing_this_cycle:
            self._model_missing_since.setdefault(mid, now)
        # Clear stamps for models that ARE present this cycle.
        for mid in list(self._model_missing_since):
            if mid in self.detected_models:
                self._model_missing_since.pop(mid)
                # Also clear any open Repairs issue for this model.
                ir.async_delete_issue(self.hass, DOMAIN, f"{self.entry.entry_id}_stale_model_{mid}")
        # Raise / refresh Repairs issues for models past the threshold.
        # Idempotent: HA's issue registry treats a second create_issue
        # call with the same id as an update.
        for mid, since in self._model_missing_since.items():
            missing_for = (now - since).total_seconds()
            if missing_for >= STALE_MODEL_TOLERANCE_SECONDS:
                self._raise_stale_model_issue(mid, missing_for)
        # Per-cycle scratch is consumed - reset for the next cycle so
        # an unrelated coordinator refresh path that doesn't run
        # _run_one_update_cycle can't accidentally use stale data.
        self._missing_this_cycle = set()
        self._new_this_cycle = set()

    def _raise_stale_model_issue(self, model_id: int, missing_seconds: float) -> None:
        """Raise a Repairs issue suggesting the user remove a stale device."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{self.entry.entry_id}_stale_model_{model_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="stale_model",
            translation_placeholders={
                "host": str(self.entry.data.get(CONF_HOST, "?")),
                "port": str(self.entry.data.get(CONF_PORT, "?")),
                "unit_id": str(self.entry.data.get(CONF_UNIT_ID, "?")),
                "model_id": str(model_id),
                "missing_minutes": str(int(missing_seconds // 60)),
            },
        )

    def _after_failed_cycle(self, exc: BaseException) -> NoReturn:
        """Record a failed cycle and raise UpdateFailed.

        Wraps unclassified exceptions as TransportError before recording
        them so the diagnostics dump always sees a categorised entry.
        Pass the exception explicitly because this helper may be called
        from outside the original ``except`` block (after the in-cycle
        retry path), where ``sys.exc_info()`` is no longer set.
        """
        if isinstance(exc, SunSpecError):
            wrapped = exc
        else:
            self._log.error(
                "Unclassified exception in update loop: %s",
                exc,
                exc_info=exc,
            )
            wrapped = TransportError(f"Unclassified: {exc.__class__.__name__}: {exc}")
            wrapped.__cause__ = exc
        self._record_error(wrapped)
        # Drop the session now rather than flagging it for the next
        # get_client(). Whatever went wrong, this socket is a suspect: a
        # half-open TCP session to an inverter that rebooted looks
        # exactly like a slow one until the next timeout. force=True,
        # because a session that already misbehaved has not earned a
        # polite goodbye, and the inverter should get its slot back at
        # once. (Until v0.31.0 there was a second reason: a late answer
        # on this socket would have been read as the answer to the next
        # request. The embedded transport checks the transaction id now.)
        self.api.close(force=True)
        self.api.reconnect_next()
        self.consecutive_failed_cycles += 1
        # HA's DataUpdateCoordinator._async_refresh stops dispatching
        # listeners on consecutive failures (it early-returns when both
        # the previous and the current refresh failed). That means the
        # entity state would never get a chance to flip from "stale
        # value" to "unavailable" once we exhaust the tolerance window
        # - it would just freeze on the last good value forever. Drive
        # the transition ourselves so the user actually sees the sensor
        # go unavailable when the inverter has been gone too long.
        if self.consecutive_failed_cycles == STALE_DATA_TOLERANCE_CYCLES + 1:
            self.async_update_listeners()
        raise UpdateFailed(str(wrapped)) from exc

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
        # Issue #52: a PV inverter that powers its comms board down at
        # dusk is unreachable for ten hours by design. Escalating that
        # to a red repair every night, and writing two warnings per
        # poll into the log while doing it, told the user nothing they
        # could act on.
        expected = cat == "transport" and self._downtime_is_expected()
        if expected:
            state = (
                OPERATING_STATE_LABELS.get(self._last_operating_state)
                if self._last_operating_state is not None
                else None
            )
            reason = (
                f"it last reported {state}"
                if state
                else "this entry is configured for an inverter that powers down when idle"
            )
            # One line at the top of the outage so the log still says
            # when the inverter went away, then quiet. The alternative,
            # staying at warning, is roughly 2400 lines a night at the
            # default poll interval.
            quiet_log = self._log.info if self._consecutive_failures[cat] == 1 else self._log.debug
            quiet_log(
                "Inverter is not answering and %s, so this is an expected standby "
                "rather than a fault (#%d in a row): %s",
                reason,
                self._consecutive_failures[cat],
                exc,
            )
        else:
            self._log.warning(
                "%s (#%d in a row): %s",
                exc.__class__.__name__,
                self._consecutive_failures[cat],
                exc,
            )
        if cat == "transient":
            return
        if expected:
            # The failure still goes in the ring buffer above, so a
            # diagnostics dump taken at 3am shows exactly what happened.
            # It just does not page the user about it.
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

    def _clear_repair_issues(self, *, force: bool = False) -> None:
        """Delete every Repairs issue this coordinator may have raised.

        Called on every successful update cycle (so a recovered inverter
        clears the panel automatically) and on async_unload_entry (so
        removing the integration does not leave ghost issues behind).
        ``transient`` is excluded - it never raises issues to begin with.
        Also clears any per-model stale issues from cjne #202 tracking.

        ``force`` deletes even an issue the user has ignored, and is for
        teardown only: an entry that is being unloaded or removed should
        not leave anything at all behind.
        """
        for category in CATEGORIES:
            if category == "transient":
                continue
            self._delete_issue(f"{self.entry.entry_id}_{category}", force=force)
        # cjne #202: clear any open stale-model issues so the Repairs
        # panel doesn't carry ghosts after the model came back or
        # the integration was removed.
        for mid in list(self._model_missing_since):
            self._delete_issue(f"{self.entry.entry_id}_stale_model_{mid}", force=force)

    def _delete_issue(self, issue_id: str, *, force: bool = False) -> None:
        """Delete one Repairs issue, unless the user has ignored it.

        Issue #52. HA's ``IssueRegistry.async_delete`` pops the whole
        entry, ``dismissed_version`` included, so deleting an ignored
        issue silently revokes the ignore. For a device that goes
        unreachable every night that made the button useless: the issue
        was cleared each morning and re-created unignored each evening,
        so the user could press "Ignore" every day and never be rid of
        it.

        Leaving an ignored issue in the registry costs nothing visible.
        The Repairs panel hides ignored issues, ``async_get_or_create``
        keeps ``dismissed_version`` when the same issue is raised again,
        and Home Assistant re-surfaces ignored issues after a core
        version bump. That is exactly the contract the user accepted
        when they pressed the button.
        """
        if not force:
            issue = ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id)
            if issue is not None and issue.dismissed_version is not None:
                return
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)
