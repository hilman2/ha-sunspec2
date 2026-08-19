"""
Custom integration to integrate SunSpec with Home Assistant.

For more details about this integration, please refer to
https://github.com/cjne/ha-sunspec
"""

import asyncio
import logging
from collections import deque
from datetime import datetime
from datetime import timedelta

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
from homeassistant.helpers import issue_registry as ir
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
from .const import CONF_SCAN_DELAY
from .const import CONF_SCAN_INTERVAL
from .const import CONF_SERIAL_PORT
from .const import CONF_TRANSPORT
from .const import CONF_UNIT_ID
from .const import CONF_WRITE_BETA_ENABLED
from .const import DEFAULT_BAUDRATE
from .const import DEFAULT_MODELS
from .const import DEFAULT_SCAN_DELAY_SECONDS
from .const import DOMAIN
from .const import INTERVAL_RETRY_DELAY_SECONDS
from .const import PARITY_NONE
from .const import PLATFORMS
from .const import PLATFORMS_READ_ONLY
from .const import SERVICE_SET_EXPORT_LIMIT
from .const import STALE_DATA_TOLERANCE_CYCLES
from .const import STALE_MODEL_TOLERANCE_SECONDS
from .const import STARTUP_MESSAGE
from .const import TRANSPORT_RTU
from .const import TRANSPORT_TCP
from .const import WRITE_CONTROLS_MODEL_ID
from .const import WRITE_LOCK_TIMEOUT_SECONDS
from .errors import CATEGORIES
from .errors import SunSpecError
from .errors import TransientError
from .errors import TransportError
from .logger import get_adapter
from .migration import cleanup_excluded_model_sensors
from .migration import find_blocking_cjne_entries
from .migration import migrate_from_cjne_sync

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

    await coordinator.async_config_entry_first_refresh()

    # Phase 5 user-value: if the user is migrating from cjne/ha-sunspec
    # and has uninstalled it (entities are orphans in the registry, no
    # live state), retarget those entities to our domain so the user
    # keeps their entity ids and Recorder history. This MUST run before
    # async_forward_entry_setups so any entity_id collisions in the
    # platform setup that follows resolve to the migrated entity.
    _maybe_migrate_from_cjne(hass, entry, log)

    # #17: drop sensor entities the sensor platform stopped building
    # when v0.14.0 introduced SENSOR_EXCLUDED_MODELS. Without this they
    # linger in the registry as permanently "Unavailable" rows. Runs
    # after the cjne migration so freshly retargeted orphans from a
    # cjne install that had model 123 ticked are cleaned up in the same
    # pass, and before the platform forward so the removals are done
    # before any entity claims an entity_id.
    cleanup_excluded_model_sensors(hass, entry, log)

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
        if WRITE_CONTROLS_MODEL_ID not in coordinator.detected_models:
            raise HomeAssistantError(
                f"Inverter does not expose SunSpec model {WRITE_CONTROLS_MODEL_ID} "
                "(immediate controls), cannot set export limit."
            )
        points: list[tuple[str, object]] = [("WMaxLimPct", percent)]
        if enable:
            # Same lock hold as the percentage, not a second one. "Set
            # the limit and turn it on" is one logical operation: with
            # two acquisitions a poll cycle or a competing service call
            # can slip in between and leave the inverter running with
            # the new percentage and the old enable state.
            points.append(("WMaxLim_Ena", 1))
        try:
            await coordinator.async_write_points_locked(WRITE_CONTROLS_MODEL_ID, points)
        except SunSpecError as exc:
            raise HomeAssistantError(f"Failed to set export limit: {exc}") from exc
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_EXPORT_LIMIT,
        _async_set_export_limit,
    )


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


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    device_entry,
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
        coordinator._clear_repair_issues()
        # Close the TCP socket BEFORE we drop our references. KACO Powador
        # (and likely other inverters) only allow one Modbus TCP connection
        # at a time; without an explicit disconnect here a config entry
        # reload would race the leftover socket against the freshly built
        # one in async_setup_entry, and the new connect would time out.
        coordinator.api.close()
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


class SunSpecDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    # Per-gateway asyncio lock used to serialise update cycles from multiple
    # config entries that share the same TCP endpoint (host, port). Several
    # inverters and Modbus TCP gateways - notably SolarEdge - only accept a
    # single TCP connection at a time. Without this lock two coordinators
    # polling different unit IDs behind the same gateway would race each
    # other and produce "connection reset by peer" errors. The lock is
    # held for the entire connect/read/close cycle so exactly one TCP
    # session is open per (host, port) at any moment. Single-gateway
    # users see no behavioural change because the lock is always free.
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

    def __init__(self, hass: HomeAssistant, client: SunSpecApiClient, entry) -> None:
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
        self.device_info = None
        self._gateway_lock = self._get_gateway_lock(
            entry.data.get(CONF_HOST), entry.data.get(CONF_PORT)
        )
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
        # whenever ``api._client`` is ``None``, which is the steady
        # state between cycles after ``api.close()``. A v0.7.3 -> v0.7.5
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
        # rescanned every MODEL_STRUCTURE_TTL_SECONDS now, so counting
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
        scan_interval = timedelta(
            seconds=entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, SCAN_INTERVAL.total_seconds()),
            )
        )
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
            {WRITE_CONTROLS_MODEL_ID}
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

    async def _async_update_data(self):
        """Update data via library, with one in-cycle retry on failure.

        Inverters and Modbus TCP gateways have famously flaky network
        connectivity. A single fast retry catches most one-shot blips
        before HA marks the coordinator as failed and the entity flips
        to "unavailable". The retry only kicks in once at least one
        cycle has succeeded - first-refresh failures fall straight
        through to ConfigEntryNotReady so HA's standard exponential
        backoff can take over instead of having every setup attempt
        block for an extra ``INTERVAL_RETRY_DELAY_SECONDS``.

        The connect/read/close cycle is held under the per-gateway lock
        (see ``_GATEWAY_LOCKS``). The lock is released across the
        retry sleep so other coordinators sharing the same TCP endpoint
        can poll in the meantime.
        """
        self._log.debug("Update data coordinator update")
        first_err: BaseException | None = None
        try:
            async with self._gateway_lock:
                data = await self._run_one_update_cycle()
            return self._after_successful_cycle(data)
        except Exception as exc:  # noqa: BLE001 - dispatched below
            first_err = exc

        # First refresh: no prior data exists, no point sleeping for
        # an in-cycle retry. Fail fast and let HA handle the retry via
        # ConfigEntryNotReady's exponential backoff.
        if self.data is None:
            return self._after_failed_cycle(first_err)

        self._log.warning(
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
        return self._after_successful_cycle(data)

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
          mid-``recv`` on the same socket. pysunspec2 packs a leading
          ``0, 0`` MBAP header - the transaction id is hardcoded 0 -
          and takes no lock of its own, so neither side can tell whose
          response it just pulled off the socket. This is not a
          single-slot-inverter problem; frames can interleave on any
          gateway.
        * **Between cycles**: ``api._client`` is None because
          ``_run_one_update_cycle`` closes at the end, so the write
          opened a *second* TCP session (connect plus a full scan) that
          nothing ever closed, holding a single-slot inverter's only
          Modbus slot until the next cycle. v0.13.4's ``model.read()``
          widened that window by one more block read.

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
            for point_name, value in points:
                await self.api.async_write_point(model_id, point_name, value)
        finally:
            # Whoever opens a session under the lock closes it under the
            # lock. Leaving the socket open past the release would let
            # the next coordinator on this gateway win the lock
            # (asyncio.Lock is FIFO, so a queued waiter is handed the
            # lock on release) and then fail to connect, which surfaces
            # as a bogus TransportError in an unrelated config entry -
            # the hardest possible shape to diagnose.
            self.api.close()
            self._gateway_lock.release()

    async def _run_one_update_cycle(self):
        """Single connect/read/close attempt. Caller holds the gateway lock.

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
        self._missing_this_cycle = previously_known - all_models
        self._new_this_cycle = all_models - previously_known
        # Cache the full set of models the inverter exposes so the
        # options-flow form can render its multi-select even between
        # cycles, when ``api._client`` has already been closed and
        # ``api.known_models()`` would return an empty list.
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

        # Auto-detect the inverter's nameplate AC power once, on the
        # first cycle that reaches this point. Prefer model 120
        # (Inverter Nameplate, "WRtg" = continuous output capability)
        # which is the canonical SunSpec field for this. Fall back to
        # model 121 (Inverter Settings, "WMax" = currently configured
        # max output) only if 120 is missing - 121 reflects whatever
        # the installer set, which is usually but not always the
        # nameplate. Both reads are guarded by individual try/excepts
        # so a flaky model read never breaks the whole update cycle.
        if self.detected_max_ac_power_kw is None:
            self.detected_max_ac_power_kw = await self._read_nameplate(all_models)

        for model_id in model_ids:
            data[model_id] = await self.api.async_get_data(model_id)
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
            except Exception as exc:  # noqa: BLE001 - convenience read, never escalate
                self._log.debug(
                    "Auto-detect nameplate from %s failed (%s), trying next",
                    label,
                    exc,
                )
                continue
            if isinstance(value, (int, float)) and value > 0:
                kw = float(value) / 1000.0
                self._log.info(
                    "Auto-detected inverter nameplate AC power: %.2f kW (from %s)",
                    kw,
                    label,
                )
                return kw
        return None

    def _after_successful_cycle(self, data):
        """Reset failure bookkeeping after a successful read."""
        # Silver rule log-when-unavailable: emit a single recovery
        # log line when we come back from an unavailable run, so
        # the user can correlate "the sensor recovered" with a
        # specific moment in their HA log without having to grep
        # through every successful debug line.
        if self.consecutive_failed_cycles > 0:
            self._log.warning(
                "Inverter recovered after %d failed update cycle(s)",
                self.consecutive_failed_cycles,
            )
        self.consecutive_failed_cycles = 0
        for cat in self._consecutive_failures:
            self._consecutive_failures[cat] = 0
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

    def _after_failed_cycle(self, exc):
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
        Also clears any per-model stale issues from cjne #202 tracking.
        """
        for category in CATEGORIES:
            if category == "transient":
                continue
            ir.async_delete_issue(self.hass, DOMAIN, f"{self.entry.entry_id}_{category}")
        # cjne #202: clear any open stale-model issues so the Repairs
        # panel doesn't carry ghosts after the model came back or
        # the integration was removed.
        for mid in list(self._model_missing_since):
            ir.async_delete_issue(self.hass, DOMAIN, f"{self.entry.entry_id}_stale_model_{mid}")
