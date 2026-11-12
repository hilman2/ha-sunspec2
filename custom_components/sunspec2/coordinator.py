"""The coordinator: one polling loop and one write path per config entry."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SunSpecApiClient
from .api import is_unanswered
from .const import CONF_ENABLED_MODELS
from .const import CONF_HOST
from .const import CONF_PORT
from .const import CONF_REARM_ON_CHANGE
from .const import CONF_SCAN_INTERVAL
from .const import CONF_STANDBY_WHEN_IDLE
from .const import CONF_UNIT_ID
from .const import CONTROL_REFUSALS_BEFORE_SKIP
from .const import DEFAULT_MODELS
from .const import DEVICE_IDENTITY_POINTS
from .const import DOMAIN
from .const import GATEWAY_REST_MIN_SECONDS
from .const import GATEWAY_REST_SETTLE_SECONDS
from .const import INTERVAL_RETRY_DELAY_SECONDS
from .const import MIN_SCAN_INTERVAL_SECONDS
from .const import NAMEPLATE_FILTER_HEADROOM
from .const import OPERATING_STATE_LABELS
from .const import OPERATING_STATE_MODEL_IDS
from .const import OPERATING_STATE_POINT
from .const import STALE_DATA_TOLERANCE_CYCLES
from .const import STALE_MODEL_TOLERANCE_SECONDS
from .const import STANDBY_OPERATING_STATES
from .const import STRUCTURE_STORAGE_KEY
from .const import STRUCTURE_STORAGE_VERSION
from .const import WRITE_LOCK_TIMEOUT_SECONDS
from .errors import CATEGORIES
from .errors import DeviceError
from .errors import SunSpecError
from .errors import TransientError
from .errors import TransportError
from .fronius_web import FroniusWebCoordinator
from .logger import get_adapter
from .logger import redact_options
from .models import SunSpecModelWrapper
from .models import point_text
from .raw_blocks import RawBlockReader
from .raw_blocks import encode_value
from .vendors import VendorProfile
from .vendors import plan_write
from .vendors import profile_for
from .vendors.profile import RawBlock
from .vendors.profile import RawField
from .write_controls import WRITE_CAPABLE_MODELS

if TYPE_CHECKING:
    # Typing only: battery_plan imports the coordinator from here.
    from .battery_plan import BatteryPlanner

SCAN_INTERVAL = timedelta(seconds=30)

_LOGGER: logging.Logger = logging.getLogger(__package__)

# Modbus exceptions in a row per (entry id, model id), for the control
# models that are polled without being selected. Read and reset by
# SunSpecDataUpdateCoordinator._read_control_model.
_CONTROL_REFUSALS_KEY = f"{DOMAIN}_control_refusals"

# Bronze rule runtime-data: typed config entry alias so platforms,
# diagnostics and the options flow can read the coordinator off
# ``entry.runtime_data`` with a real type instead of fishing it out
# of ``hass.data[DOMAIN][entry.entry_id]``.
type SunSpec2ConfigEntry = ConfigEntry["SunSpecDataUpdateCoordinator"]


def structure_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The file the SunSpec model layout of an entry is kept in.

    One file per config entry, so that two inverters never share a
    layout and removing an entry can remove its file.
    """
    return Store(hass, STRUCTURE_STORAGE_VERSION, f"{STRUCTURE_STORAGE_KEY}.{entry_id}")


def gateway_rest_until(now: datetime, round_seconds: int) -> datetime:
    """When a gateway that left a request unanswered may be asked again.

    See GATEWAY_REST_MIN_SECONDS for the reasoning.

    Args:
        now (datetime): The moment the request went unanswered, timezone-aware.
        round_seconds (int): The period of the gateway's collection rounds,
            which start on the clock, counted from the Unix epoch.

    Returns:
        datetime: The end of the rest, in UTC.
    """
    earliest = now.timestamp() + GATEWAY_REST_MIN_SECONDS
    next_round = (earliest // round_seconds + 1) * round_seconds
    return datetime.fromtimestamp(next_round + GATEWAY_REST_SETTLE_SECONDS, tz=UTC)


class _GatewayResting(Exception):
    """A poll fell into the gateway's rest. Never leaves the coordinator."""

    def __init__(self, until: datetime) -> None:
        super().__init__(until)
        self.until = until


class SunSpecDataUpdateCoordinator(DataUpdateCoordinator[dict[int, SunSpecModelWrapper]]):
    """Class to manage fetching data from the API."""

    # Per-gateway asyncio lock used to serialise update cycles and write
    # sequences from multiple config entries that share the same TCP
    # endpoint (host, port). Several inverters and Modbus TCP gateways -
    # notably SolarEdge - only accept a single TCP connection at a time,
    # and Home Assistant's modbus integration gives every entry behind
    # one endpoint the same connection, so the slot is not what is
    # contended any more. What the lock keeps is the whole cycle and the
    # whole write sequence in one piece: the connection's own lock
    # serialises single requests, so without this a poll of one unit
    # could land between the steps of another unit's write plan.
    # Single-gateway users see no behavioural change because the lock is
    # always free.
    _GATEWAY_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}
    # Per gateway, the end of its rest after a request it left unanswered
    # (see GATEWAY_REST_MIN_SECONDS). Class-level like the locks, because
    # the gateway is busy for every entry behind it, not only for the one
    # whose request it did not answer.
    _GATEWAY_RESTS: dict[tuple[str, int], datetime] = {}

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
        # The battery plan, built by the first of its entities
        # and shared by the rest. See battery_plan.py.
        self.battery_plan: BatteryPlanner | None = None
        # The vendor's web interface, when the profile has one and the
        # user entered its password. See fronius_web.py.
        self.web: FroniusWebCoordinator | None = None
        # The registers a vendor keeps outside the SunSpec models,
        # decoded, keyed by the profile's block key. See raw_blocks.py.
        self.raw_blocks: dict[str, dict[str, Any]] = {}
        # What Home Assistant last wrote to a raw field, keyed
        # (block, field), for the timed rewrites in vendor_blocks.py.
        self.raw_setpoints: dict[tuple[str, str], Any] = {}
        # Writes to the vendor's persistent registers, for the flash
        # wear counter. The sensor restores it across restarts.
        self.raw_write_count = 0
        self._raw_write_listeners: list[Callable[[], None]] = []
        self._flash_warned = False
        # Which platforms async_setup_entry actually forwarded. Recorded
        # there and read back by async_unload_entry, including when
        # setup only forwarded part of the list before failing.
        self.forwarded_platforms: list[str] = []
        self._structure_store = structure_store(hass, entry.entry_id)
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
        self._gateway_key: tuple[str, int] = (entry.data[CONF_HOST], entry.data[CONF_PORT])
        self._gateway_lock = self._get_gateway_lock(*self._gateway_key)
        self._log = get_adapter(
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_UNIT_ID],
        )
        self._raw_reader = RawBlockReader(client, self._log)
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
        # failed one. A v0.7.3 -> v0.7.5
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
        self._log.debug("Options: %s", redact_options(entry.options))
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
        # The control models are polled whether or not the user ticked
        # them in the model multi-select: the write entities read their
        # state from ``coordinator.data[model]``, and without the model
        # in the polled set no write entity ever registers (#17). The
        # battery, export and grid blocks are read on every device
        # that has them. Polling does not write any register.
        #
        # Deliberately a SEPARATE set from option_model_filter.
        # option_model_filter mirrors exactly what the user picked and
        # is what the options form round-trips, so folding a model into
        # it would render a tick the user never made and then persist
        # it on their next save.
        self.write_model_filter: set[int] = set(WRITE_CAPABLE_MODELS)
        # Control models the device refuses to read while they are
        # polled only for their controls. An Enphase IQ Gateway lists
        # model 704 in its scan and answers every read of it with
        # exception 6, "busy", even after the transport has retried for
        # a minute (#172). Such a model leaves the polled set until the
        # entry reloads, so it cannot keep the models the user did
        # select from loading. _read_control_model decides when a
        # Modbus exception counts as a refusal.
        self.refused_control_models: set[int] = set()
        # Control models this coordinator has read at least once.
        self._control_models_read: set[int] = set()
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
                self._raise_if_gateway_rests()
                try:
                    data = await self._run_one_update_cycle()
                except BaseException:
                    # Release the failed session before another unit takes
                    # the lock, including during the retry sleep.
                    await self.api.async_close(force=True)
                    raise
            result = self._after_successful_cycle(data)
            await self._async_save_model_structure()
            return result
        except _GatewayResting as rest:
            self._skip_resting_cycle(rest.until)
        except Exception as exc:  # noqa: BLE001 - dispatched below
            first_err = exc

        # First refresh: no prior data exists, no point sleeping for
        # an in-cycle retry. Fail fast and let HA handle the retry via
        # ConfigEntryNotReady's exponential backoff. Nor after a request
        # the gateway left unanswered, where the retry would queue
        # behind it (see GATEWAY_REST_MIN_SECONDS).
        if self.data is None or self._rests_after(first_err):
            return await self._after_failed_cycle(first_err)

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
        self.api.reconnect_next(rescan=not isinstance(first_err, (TransportError, TransientError)))
        await asyncio.sleep(INTERVAL_RETRY_DELAY_SECONDS)
        try:
            async with self._gateway_lock:
                # Another entry's request may have gone unanswered meanwhile.
                self._raise_if_gateway_rests()
                try:
                    data = await self._run_one_update_cycle()
                except BaseException:
                    await self.api.async_close(force=True)
                    raise
        except _GatewayResting:
            return await self._after_failed_cycle(first_err)
        except Exception as second_err:  # noqa: BLE001 - dispatched below
            return await self._after_failed_cycle(second_err)
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
        """Write points of one model while holding the per-gateway lock.

        A write goes through here or through ``async_write_transaction``,
        never straight through ``coordinator.api``. The lock is held for
        the whole sequence that ``plan_write`` makes of ``points``, so no
        poll and no other write runs between its steps.

        ``async_request_refresh()`` stays at the call sites, outside the
        lock. ``asyncio.Lock`` is not reentrant and
        ``REQUEST_REFRESH_DEFAULT_IMMEDIATE`` is True, so the debouncer
        runs ``_async_refresh`` inline and the refresh would wait forever
        for the lock this method still holds.

        Args:
            model_id: The SunSpec model the points belong to.
            points: Point name and value. The order of the list does not
                decide the order on the wire: the points of one step go
                out in the order of their registers.

        Raises:
            TransientError: The lock did not come free within
                ``WRITE_LOCK_TIMEOUT_SECONDS``, or the gateway is resting
                after a request it left unanswered.
            SunSpecError: The write itself failed, see
                ``SunSpecApiClient.async_write_points``.
        """
        async with self.async_write_transaction():
            # One step for everyone but a vendor that takes a new value
            # only on the rising edge of its enable register; then three
            # (see plan_write). Each step is one batch, not one call per
            # point: the API layer flushes a batch together so adjacent
            # registers go out in a single Modbus frame and the inverter
            # cannot act on half of them. The pause between the steps
            # stays under the lock, so no poll or other write lands
            # inside the sequence.
            rearm_default = self.vendor.rearm_by_default if self.vendor is not None else False
            steps = plan_write(
                self.vendor,
                model_id,
                points,
                self._point_reads_on,
                self.entry.options.get(CONF_REARM_ON_CHANGE, rearm_default),
            )
            for step in steps:
                await self.api.async_write_points(model_id, step.points)
                if step.settle_seconds > 0:
                    await asyncio.sleep(step.settle_seconds)

    @asynccontextmanager
    async def async_write_transaction(self) -> AsyncIterator[None]:
        """Hold the gateway across reads, writes and verification of one control action."""
        try:
            async with asyncio.timeout(WRITE_LOCK_TIMEOUT_SECONDS):
                await self._gateway_lock.acquire()
        except TimeoutError as exc:
            raise TransientError(
                f"Timed out after {WRITE_LOCK_TIMEOUT_SECONDS}s waiting for the Modbus "
                f"gateway to become free; the inverter is busy, try again"
            ) from exc
        if (until := self.gateway_rest_end()) is not None:
            self._gateway_lock.release()
            raise TransientError(
                "The device is still busy with a request it left unanswered; try again after "
                f"{dt_util.as_local(until).strftime('%H:%M:%S')}"
            )
        successful = False
        try:
            yield
            successful = True
        except BaseException as exc:
            # A write the gateway did not answer keeps it busy like a read.
            if self._rests_after(exc):
                self._start_gateway_rest()
            raise
        finally:
            try:
                # A failed transaction can leave a late response on the
                # session. Do not let the next request consume it.
                if not successful:
                    await self.api.async_close(force=True)
            finally:
                self._gateway_lock.release()

    @callback
    def async_add_raw_write_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` after every counted raw write; returns the remover."""
        self._raw_write_listeners.append(listener)

        def _remove() -> None:
            self._raw_write_listeners.remove(listener)

        return _remove

    async def async_write_raw_locked(
        self, block: RawBlock, raw_field: RawField, value: float | int
    ) -> None:
        """Write one field of a vendor's raw block while holding the gateway lock.

        The raw counterpart of ``async_write_points_locked``: same lock,
        same session rules, one register run per call. The write is
        remembered in ``raw_setpoints`` for the timed rewrites and
        counted unless the profile calls the register volatile.

        Raises:
            SunSpecError: The write failed; see ``api.async_write_block``.
        """
        data = encode_value(raw_field.kind, value, block.word_order)
        address = block.address + raw_field.offset
        async with self.async_write_transaction():
            await self.api.async_write_block(address, data, unit_id_offset=block.unit_id_offset)
        self.raw_setpoints[(block.key, raw_field.name)] = value
        vendor = self.vendor
        if vendor is not None and address in vendor.volatile_registers:
            return
        self.raw_write_count += 1
        if not self._flash_warned:
            # Once per run, at the first persistent write. The vendor
            # says periodic changes wear the flash; the count sensor is
            # how a user sees what their automation does.
            self._log.warning(
                "Writing the inverter's persistent registers. The manufacturer warns that "
                "periodic changes wear its flash memory; the 'Register writes' sensor counts them"
            )
            self._flash_warned = True
        for listener in list(self._raw_write_listeners):
            listener()

    async def _run_one_update_cycle(self) -> dict[int, SunSpecModelWrapper]:
        """Single read attempt over the live session. Caller holds the gateway lock.

        Connects only when there is no live session: the first cycle and
        the cycle after a failure. The session stays up between cycles.

        Returns the freshly-read data dict on success and re-raises any
        exception untouched on failure - bookkeeping (error categorisation,
        Repairs issues, failure counters) is the caller's job so the
        in-cycle retry can swallow a transient first failure without
        inflating the per-category thresholds.
        """
        data = {}
        await self._async_apply_link_profile()
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
        # one.
        self.detected_models = all_models
        # Union first, intersect second: a model the device does not
        # expose is still never read, but the control models get in
        # without polluting the user's own selection.
        model_ids = (self.option_model_filter | self.write_model_filter) & all_models
        model_ids -= self.refused_control_models

        # Fetch common model 1 once per process under the lock so
        # the sensor platform setup can read device metadata
        # without opening a second TCP slot. Re-reading it on
        # every cycle would be wasteful - the device info never
        # changes for a given physical inverter.
        fresh_common: SunSpecModelWrapper | None = None
        if self.device_info is None:
            info = await self.api.async_get_data(1)
            self.device_info = info
            fresh_common = info

            # The manufacturer picks the vendor; model, option and
            # version tell a vendor's generations apart.
            self.vendor = profile_for(
                point_text(info, "Mn") or None,
                point_text(info, "Md"),
                point_text(info, "Opt"),
                point_text(info, "Vr"),
            )
            if self.vendor is not None:
                self._log.info("Vendor profile %s applies to this device", self.vendor.slug)
                # A first run without a stored identity learns the vendor
                # only now, so the rest of this cycle already gets its timeout.
                await self._async_apply_link_profile()
        # Only meaningful once model 1 has been read, and it is read on
        # the first cycle, so this lands exactly where it can act.
        rescan_after_cycle = await self._check_device_identity()
        if self.vendor is not None:
            model_ids |= self.vendor.required_models & all_models
        self._log.debug("Update data got models %s", model_ids)

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
            # Model 1 was already read for identity on this cycle. Reuse it
            # for its sensors without another request and pacing pause;
            # later cycles still read it when the user selected it.
            if model_id == 1 and fresh_common is not None:
                data[model_id] = fresh_common
            elif self._polled_only_for_controls(model_id):
                wrapper = await self._read_control_model(model_id)
                if wrapper is not None:
                    data[model_id] = wrapper
            else:
                data[model_id] = await self.api.async_get_data(model_id)
        # After the models, on the same session: what the vendor keeps
        # outside them. A block that fails never fails the cycle.
        if self.vendor is not None and self.vendor.raw_blocks:
            self.raw_blocks = await self._raw_reader.async_read(self.vendor.raw_blocks)
        if rescan_after_cycle:
            # Firmware changed: hand this cycle's data back as read, and
            # let the NEXT connect walk the model tree again. Flagging it
            # here, after the reads, rather than inside the identity
            # check keeps the rebuild out of the middle of a cycle whose
            # model list was computed against the old client.
            self.api.reconnect_next()
        return data

    def _polled_only_for_controls(self, model_id: int) -> bool:
        """Whether the model is read only because its controls might be built.

        True for a write-capable model the user did not select and the
        vendor profile does not require. Such a model may be refused
        without failing the cycle.
        """
        if model_id not in self.write_model_filter or model_id in self.option_model_filter:
            return False
        return self.vendor is None or model_id not in self.vendor.required_models

    async def _read_control_model(self, model_id: int) -> SunSpecModelWrapper | None:
        """Read a model that is polled only for its controls.

        Returns None when the device refuses the model. The model is
        then in ``refused_control_models`` and is not read again.

        Raises:
            SunSpecError: The read failed, and the cycle fails with it.
        """
        refusals: dict[tuple[str, int], int] = self.hass.data.setdefault(_CONTROL_REFUSALS_KEY, {})
        key = (self.entry.entry_id, model_id)
        try:
            wrapper = await self.api.async_get_data(model_id)
        except DeviceError as exc:
            # Only a Modbus exception gets here. A timeout or a dropped
            # link says the device is not answering at all and fails the
            # cycle like any other read.
            #
            # A model this coordinator has read before had a hiccup: the
            # cycle fails, and the retry and the stale-data tolerance
            # keep its values. A model never read counts as refused only
            # at the second exception in a row, so one bad answer during
            # setup ends in a setup retry, not in an entry without that
            # model's controls. The count lives in hass.data because
            # every setup retry and every reload builds a new
            # coordinator, and a success in between resets it.
            if model_id in self._control_models_read:
                raise
            refusals[key] = refusals.get(key, 0) + 1
            if refusals[key] < CONTROL_REFUSALS_BEFORE_SKIP:
                raise
            self.refused_control_models.add(model_id)
            self._log.warning(
                "%s. Its controls stay unavailable until the integration reloads", exc
            )
            return None
        refusals.pop(key, None)
        self._control_models_read.add(model_id)
        return wrapper

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

    async def _after_failed_cycle(self, exc: BaseException) -> NoReturn:
        """Record a failed cycle and raise UpdateFailed.

        Wraps unclassified exceptions as TransportError before recording
        them so the diagnostics dump always sees a categorised entry.
        Pass the exception explicitly because this helper may be called
        from outside the original ``except`` block (after the in-cycle
        retry path), where ``sys.exc_info()`` is no longer set.
        """
        if self._shutdown_requested:
            raise asyncio.CancelledError("SunSpec entry was unloaded")
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
        if self._rests_after(exc):
            self._start_gateway_rest()
        # Drop the session now rather than flagging it for the next
        # get_client(). Whatever went wrong, this socket is a suspect: a
        # half-open TCP session to an inverter that rebooted looks
        # exactly like a slow one until the next timeout. force=True,
        # because a session that already misbehaved has not earned a
        # polite goodbye, and the inverter should get its slot back at
        # once. (Until v0.31.0 there was a second reason: a late answer
        # on this socket would have been read as the answer to the next
        # request. The embedded transport checks the transaction id now.)
        await self.api.async_close(force=True)
        self.api.reconnect_next(rescan=not isinstance(wrapped, (TransportError, TransientError)))
        self._count_failed_cycle()
        raise UpdateFailed(str(wrapped)) from exc

    def _count_failed_cycle(self) -> None:
        """Count one failed cycle towards the stale-data tolerance."""
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

    def _link_profile(self) -> VendorProfile | None:
        """The vendor profile that decides how the link is used.

        The one read from the device this run, or before that the one of
        the identity stored with the layout. That way a restart asks the
        link for the profile's timeout before the first request, instead
        of after the first cycle has run on the default.
        """
        if self.vendor is not None or self._persisted_identity is None:
            return self.vendor
        identity = self._persisted_identity
        return profile_for(identity.get("Mn"), identity.get("Md", ""), "", identity.get("Vr", ""))

    async def _async_apply_link_profile(self) -> None:
        """Ask the link for the timeout the vendor profile names. Caller holds the gateway lock."""
        profile = self._link_profile()
        if profile is not None and profile.request_timeout is not None:
            await self.api.async_require_timeout(profile.request_timeout)

    def _rests_after(self, exc: BaseException | None) -> bool:
        """Whether ``exc`` sends the gateway into a rest (see GATEWAY_REST_MIN_SECONDS)."""
        profile = self._link_profile()
        return (
            exc is not None
            and profile is not None
            and profile.collection_round_seconds is not None
            and is_unanswered(exc)
        )

    def _start_gateway_rest(self) -> None:
        """Leave the gateway alone, for every entry behind it, until its next round."""
        profile = self._link_profile()
        if profile is None or profile.collection_round_seconds is None:
            return
        until = gateway_rest_until(dt_util.utcnow(), profile.collection_round_seconds)
        self._GATEWAY_RESTS[self._gateway_key] = until
        self._log.info(
            "The device left a request unanswered. Nothing more is sent to it until %s, "
            "so it can finish the request first",
            dt_util.as_local(until).strftime("%H:%M:%S"),
        )

    def gateway_rest_end(self) -> datetime | None:
        """The end of the gateway's current rest, or None when it is not resting."""
        until = self._GATEWAY_RESTS.get(self._gateway_key)
        if until is None:
            return None
        if dt_util.utcnow() >= until:
            del self._GATEWAY_RESTS[self._gateway_key]
            return None
        return until

    def _raise_if_gateway_rests(self) -> None:
        """Raise ``_GatewayResting`` while the gateway rests. Caller holds the gateway lock."""
        if (until := self.gateway_rest_end()) is not None:
            raise _GatewayResting(until)

    def _skip_resting_cycle(self, until: datetime) -> NoReturn:
        """Fail a poll that fell into the gateway's rest, without sending anything.

        It counts as a failed cycle, so the sensors go unavailable after as
        many polls as they would if the poll had gone out and failed. It is
        not recorded as an error, because nothing new was learned.
        """
        if self._shutdown_requested:
            raise asyncio.CancelledError("SunSpec entry was unloaded")
        self._count_failed_cycle()
        until_text = dt_util.as_local(until).strftime("%H:%M:%S")
        self._log.debug("Gateway rests until %s, poll skipped", until_text)
        raise UpdateFailed(
            f"The device is still busy with a request it left unanswered; "
            f"the next poll goes out after {until_text}"
        )

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
        The Repairs panel hides ignored issues, and ``async_get_or_create``
        keeps ``dismissed_version`` when the same issue is raised again.
        A core update does not reset the ignore (#160). A reload or removal
        of the entry does, because its unload deletes with ``force``.
        "Show ignored repairs" lists only active issues, and the registry
        loads every non-persistent issue as inactive after a restart, so
        after a restart the user can lift the ignore only once this
        integration raises the issue again. That is exactly the contract
        the user accepted when they pressed the button.
        """
        if not force:
            issue = ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id)
            if issue is not None and issue.dismissed_version is not None:
                return
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)
