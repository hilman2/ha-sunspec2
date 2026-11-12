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
        now: The moment the request went unanswered, timezone-aware.
        round_seconds: The period of the gateway's collection rounds,
            which start on the clock, counted from the Unix epoch.

    Returns:
        The end of the rest, in UTC.
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
    """Polls the SunSpec models of one config entry and carries out its writes."""

    # One asyncio lock per (host, port), shared by the config entries
    # behind that endpoint. Their shared Modbus connection serialises
    # single requests. The gateway lock keeps a whole read cycle and a
    # whole write sequence in one piece, so a poll of one unit cannot
    # land between the steps of another unit's write plan.
    _GATEWAY_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}
    # Per gateway, the end of its rest after a request it left unanswered
    # (see GATEWAY_REST_MIN_SECONDS). Class-level like the locks: the
    # gateway is busy for every entry behind it, not only for the entry
    # whose request it did not answer.
    _GATEWAY_RESTS: dict[tuple[str, int], datetime] = {}

    @classmethod
    def _get_gateway_lock(cls, host: str, port: int) -> asyncio.Lock:
        """The lock of the gateway at (host, port), created on first use."""
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
        # Common model 1, read once by the first update cycle under the
        # gateway lock. The platforms take the device metadata from here
        # instead of sending a request of their own outside the lock.
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
        # The platforms async_setup_entry forwarded. async_unload_entry
        # unloads exactly these.
        self.forwarded_platforms: list[str] = []
        self._structure_store = structure_store(hass, entry.entry_id)
        # Revision of the layout on disk. A successful cycle compares it
        # with ``api.structure_revision``, so a rescan that found
        # something new is written out without diffing the layouts.
        self._persisted_structure_revision: int | None = None
        # Device identity (model 1) stored with the layout and checked
        # once per run against the live device. The check catches what
        # the layout validation cannot: a different inverter on a
        # recycled IP whose model tree happens to start the same way.
        self._persisted_identity: dict[str, Any] | None = None
        self._identity_checked = False
        # Whether the nameplate auto-detection has run. It gets one
        # attempt per run: a device without model 120 or 121 would
        # otherwise be asked on every cycle.
        self._nameplate_probed = False
        self._gateway_key: tuple[str, int] = (entry.data[CONF_HOST], entry.data[CONF_PORT])
        self._gateway_lock = self._get_gateway_lock(*self._gateway_key)
        self._log = get_adapter(
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_UNIT_ID],
        )
        self._raw_reader = RawBlockReader(client, self._log)
        # The last 20 errors per category. diagnostics.py reads this
        # {category: deque} shape. The categories come from
        # errors.CATEGORIES, so a new category gets its buffer here.
        self._recent_errors: dict[str, deque[dict[str, Any]]] = {
            cat: deque(maxlen=20) for cat in CATEGORIES
        }
        # Consecutive failures per category since the last successful
        # update. _record_error raises the Repairs issues from them.
        self._consecutive_failures: dict[str, int] = {cat: 0 for cat in CATEGORIES}
        # Consecutive failed update cycles, each counted after its
        # in-cycle retry. SunSpecEntity.available reads the count for
        # the stale-data tolerance.
        self.consecutive_failed_cycles: int = 0
        # The model ids the device exposes, set by every successful
        # cycle. The options flow builds its model multi-select from
        # this set, because ``api.known_models()`` returns ``[]`` while
        # there is no client: before the first cycle and after a failed
        # one. A form rendered from an empty list has no model to tick,
        # and the flow refuses to save an empty selection.
        self.detected_models: set[int] = set()
        # Nameplate AC power in kW, detected on the first successful
        # cycle from model 120 "WRtg" or else model 121 "WMax" (see
        # _read_nameplate). The options flow suggests it for
        # CONF_MAX_AC_POWER_KW. None when the device exposes neither.
        self.detected_max_ac_power_kw: float | None = None
        # The register the nameplate came from, e.g. "model 120 WRtg".
        # Diagnostics and the rejection log line name it, so nobody
        # looks for a configured peak that was never configured (#45).
        self.detected_max_ac_power_source: str | None = None
        # Per model, the time of the first cycle that stopped seeing it
        # (cjne issue #202). Detecting the model again clears the entry.
        # After STALE_MODEL_TOLERANCE_SECONDS a Repairs issue suggests
        # removing the model's device, because the inverter has stopped
        # exposing that model (typical for SMA Tripower X12 with DER 714
        # after a firmware update). Wall-clock time rather than a cycle
        # counter: the model tree is rescanned only when the cached
        # layout stops validating, so most cycles cannot observe a
        # missing model.
        self._model_missing_since: dict[int, datetime] = {}
        # Scratch sets of one cycle, filled by _run_one_update_cycle and
        # consumed by _after_successful_cycle. They are instance state
        # because the in-cycle retry sits between those two methods.
        self._missing_this_cycle: set[int] = set()
        self._new_this_cycle: set[int] = set()

        # The operating state (SunSpec "St") read by the last successful
        # cycle, or None if no polled model carries it. Once an inverter
        # powers its comms board down, the last state it reported is the
        # only evidence that the silence is its own decision (#52).
        self._last_operating_state: int | None = None
        # Opt-out for devices whose shutdown cannot be observed, see
        # CONF_STANDBY_WHEN_IDLE. A changed option reloads the entry.
        self.standby_when_idle: bool = bool(entry.options.get(CONF_STANDBY_WHEN_IDLE, False))

        self._log.debug("Data: %s", entry.data)
        self._log.debug("Options: %s", redact_options(entry.options))
        models = entry.options.get(
            CONF_ENABLED_MODELS, entry.data.get(CONF_ENABLED_MODELS, DEFAULT_MODELS)
        )
        # A corrupted options save can have stored ``models_enabled: []``
        # (see detected_models above). Polling no model would remove
        # every sensor, so DEFAULT_MODELS apply until the user picks
        # models again.
        if not models:
            self._log.warning(
                "Configured models filter is empty, falling back to defaults. "
                "Re-open the options form and pick the models you want."
            )
            models = DEFAULT_MODELS
        # An entry saved before the config flow checked the range, or
        # edited by hand in core.config_entries, can carry 0 or a
        # negative interval. Home Assistant's coordinator stops polling
        # on the one and hot-loops on the other (see
        # MIN_SCAN_INTERVAL_SECONDS). The clamp repairs such an entry on
        # its next reload.
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
        # them: the write entities read their state from
        # ``coordinator.data[model]``, so a model that is not polled
        # registers no write entity (#17). Polling writes no register.
        # The set stays separate from option_model_filter, which the
        # options form round-trips: a model folded into it would show a
        # tick the user never made and be saved with their next change.
        self.write_model_filter: set[int] = set(WRITE_CAPABLE_MODELS)
        # Control models the device refuses to read while they are
        # polled only for their controls (see _read_control_model). An
        # Enphase IQ Gateway lists model 704 in its scan and answers
        # every read of it with exception 6, "busy", even after the
        # transport has retried for a minute (#172). A refused model
        # leaves the polled set until the entry reloads, so it cannot
        # keep the models the user did select from loading.
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
        """Hand the API client the layout an earlier run stored.

        Called once, before the first refresh. A missing or unreadable
        store is not an error: the next connect scans instead.
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
        """Store the layout when its revision or the device identity changed.

        Runs after every successful cycle, so the stored layout has just
        been read from end to end without an error.
        """
        revision = self.api.structure_revision
        identity = self._device_identity()
        # The identity is part of the comparison: a firmware update whose
        # rescan found the same layout still gets its new version string
        # stored, instead of being reported again on every restart.
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
        """Manufacturer, model, version and serial out of SunSpec model 1.

        None before model 1 has been read, or when it holds none of them.
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
        """Compare the live device with the identity stored with the layout.

        The comparison is made once per run. Every later call returns False.

        Returns:
            True when only the version string differs, which is a firmware
            update: the caller finishes the cycle and then has the model
            layout rescanned once.

        Raises:
            TransientError: Manufacturer, model or serial differ. The
                stored layout is deleted and the next connect rescans.
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
        # Same manufacturer, model and serial, another version: a
        # firmware update, the one event that can legitimately change a
        # SunSpec model tree. The device is still the same device, so
        # the cycle runs through and the layout is rescanned once (#49).
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
        # The store file has to go, not only _persisted_identity. This
        # check runs in the first cycle of a run, which has no in-cycle
        # retry and fails as ConfigEntryNotReady. Home Assistant retries
        # that with a new coordinator, which would load the same store
        # and fail on the same mismatch forever (#49).
        await self.async_remove_stored_model_structure()
        self.api.reconnect_next()
        raise TransientError("Stored SunSpec layout belongs to a different device, rescanning")

    async def _async_update_data(self) -> dict[int, SunSpecModelWrapper]:
        """Run one update cycle, with one in-cycle retry after a failure."""
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

        # No in-cycle retry on the first refresh: its failure becomes
        # ConfigEntryNotReady, and Home Assistant retries the setup with
        # its own backoff. And none after a request the gateway left
        # unanswered, where the retry would queue behind that request
        # (see GATEWAY_REST_MIN_SECONDS).
        if self.data is None or self._rests_after(first_err):
            return await self._after_failed_cycle(first_err)

        # A warning for the first failure of an outage, unless the
        # inverter is expected to be away (#52). The cycles of the same
        # outage that follow log at debug: one line when a device goes
        # away and one when it is back is what Home Assistant asks for.
        first_of_outage = self.consecutive_failed_cycles == 0
        retry_log = (
            self._log.warning
            if first_of_outage and not self._downtime_is_expected()
            else self._log.debug
        )
        retry_log(
            "Update cycle failed (%s: %s); retrying in %ds",
            first_err.__class__.__name__,
            first_err,
            INTERVAL_RETRY_DELAY_SECONDS,
        )
        # The retry runs on a fresh client. The sleep is taken without
        # the gateway lock, so other coordinators on the same gateway
        # can poll during the wait.
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

        Call ``async_request_refresh()`` after this method has returned,
        never while the lock is held. ``asyncio.Lock`` is not reentrant
        and ``REQUEST_REFRESH_DEFAULT_IMMEDIATE`` is True, so the
        debouncer runs ``_async_refresh`` inline and the refresh would
        wait forever for the lock the write still holds.

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
            # plan_write returns one step, or three for a vendor that
            # takes a new value only on the rising edge of its enable
            # register. Each step is one batch, not one call per point:
            # the API layer sends adjacent registers in a single Modbus
            # frame, so the inverter cannot act on half of them. The
            # pause between the steps stays under the lock.
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
            # Once per run, at the first persistent write. The count
            # sensor shows a user what their automation does.
            self._log.warning(
                "Writing the inverter's persistent registers. The manufacturer warns that "
                "periodic changes wear its flash memory; the 'Register writes' sensor counts them"
            )
            self._flash_warned = True
        for listener in list(self._raw_write_listeners):
            listener()

    async def _run_one_update_cycle(self) -> dict[int, SunSpecModelWrapper]:
        """Read every polled model once. Caller holds the gateway lock.

        Returns the freshly read data. An exception passes through
        untouched: the caller does the error bookkeeping, so a failure
        that the in-cycle retry recovers counts towards no threshold.
        """
        data = {}
        await self._async_apply_link_profile()
        all_models = set(await self.api.async_get_models())
        # Models that were known and are missing now (cjne issue #202).
        # _after_successful_cycle acts on them, once the rest of the
        # cycle is known to be good.
        previously_known = self.detected_models
        missing = previously_known - all_models
        if missing and self.api.last_scan_was_partial:
            # The scan stopped early and the API layer had no usable
            # cached layout to fall back on, so the list is short
            # through no fault of the device. Served as a success, it
            # would turn the affected entities "unknown" past the
            # stale-data tolerance: a gap that looks exactly like a
            # connection drop (#42). A failed cycle keeps the last good
            # values and gets a fresh scan on the retry.
            raise TransientError(
                f"SunSpec scan was incomplete: model(s) {sorted(missing)} were not read this cycle"
            )
        self._missing_this_cycle = missing
        self._new_this_cycle = all_models - previously_known
        self.detected_models = all_models
        # Union first, intersection second: a model the device does not
        # expose is never read, and the control models get in without
        # touching the user's own selection.
        model_ids = (self.option_model_filter | self.write_model_filter) & all_models
        model_ids -= self.refused_control_models

        # Common model 1 is read once per run: the device metadata does
        # not change for a given inverter.
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
        # The identity check needs model 1, which the first cycle has
        # just read.
        rescan_after_cycle = await self._check_device_identity()
        if self.vendor is not None:
            model_ids |= self.vendor.required_models & all_models
        self._log.debug("Update data got models %s", model_ids)

        # The nameplate is detected once, on the first cycle that gets
        # here. _read_nameplate guards each read, so only a read timeout
        # fails the cycle.
        if self.detected_max_ac_power_kw is None and not self._nameplate_probed:
            self._nameplate_probed = True
            self.detected_max_ac_power_kw = await self._read_nameplate(all_models)

        for model_id in model_ids:
            # Model 1 was already read for the identity on this cycle and
            # is reused for its sensors without another request and
            # pacing pause. Later cycles read it when the user selected it.
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
            # Firmware changed: this cycle's data is returned as read and
            # the next connect walks the model tree again. The flag is
            # set here, after the reads, because the model list of this
            # cycle was computed against the current client.
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
        """Read the continuous AC power capability from model 120 or 121.

        Returns the nameplate in kW, or None if neither model is present
        or neither read gave a value. Only a read timeout is raised, as
        TransientError. Any other failure is logged at debug level.
        """
        # Model 120 "WRtg" is the continuous output capability. Model 121
        # "WMax" is the maximum the installer configured, usually but not
        # always the nameplate, so it is only the fallback.
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
                # A device that did not answer this read will not answer
                # the model reads that follow either, and each of those
                # waits the full request timeout. Failing here saves the
                # cycle those waits and starts the reconnect at once.
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
        """Reset the failure bookkeeping after a successful cycle and return ``data``."""
        if self.consecutive_failed_cycles > 0:
            # One recovery line, so the log says when the sensors came
            # back. An inverter that announced its own shutdown and then
            # answers again has woken up rather than recovered, so that
            # case logs at info (#52). _downtime_is_expected is called
            # before _last_operating_state is refreshed below, because
            # the evidence is the state from before the silence.
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
        if self._new_this_cycle:
            self._log.info(
                "Detected %d new SunSpec model(s) on this cycle: %s",
                len(self._new_this_cycle),
                sorted(self._new_this_cycle),
            )
            for mid in self._new_this_cycle:
                self._model_missing_since.pop(mid, None)
        self._update_stale_model_tracking()
        return data

    def _read_operating_state(self, data: dict[int, SunSpecModelWrapper]) -> int | None:
        """The inverter operating state in a fresh data dict, or None.

        Only the models in OPERATING_STATE_MODEL_IDS are consulted; the
        note there says why model 701 is not one of them. None means the
        device exposes no such model, the model is not polled, or the
        point is unimplemented. The caller treats None as "not standby".
        """
        for model_id in OPERATING_STATE_MODEL_IDS:
            wrapper = data.get(model_id)
            if wrapper is None:
                continue
            try:
                value = wrapper.getValue(OPERATING_STATE_POINT)
            except (KeyError, IndexError, TypeError, AttributeError):
                # The model instance lacks the point or the wrapper has an
                # unexpected shape. A convenience read does not fail a cycle.
                continue
            if isinstance(value, int):
                return value
        return None

    def _downtime_is_expected(self) -> bool:
        """Whether the inverter being unreachable right now is normal (#52).

        True when the last successful read reported a state in
        STANDBY_OPERATING_STATES, or when the user set
        CONF_STANDBY_WHEN_IDLE.
        """
        if self.standby_when_idle:
            return True
        return self._last_operating_state in STANDBY_OPERATING_STATES

    def _update_stale_model_tracking(self) -> None:
        """Stamp newly missing models and raise a Repairs issue for long-gone ones."""
        now = dt_util.utcnow()
        # setdefault, not assignment: the stamp records when the model
        # went missing. Stamping it again on every later cycle would
        # reset the clock, and the threshold would never be reached.
        for mid in self._missing_this_cycle:
            self._model_missing_since.setdefault(mid, now)
        for mid in list(self._model_missing_since):
            if mid in self.detected_models:
                self._model_missing_since.pop(mid)
                ir.async_delete_issue(self.hass, DOMAIN, f"{self.entry.entry_id}_stale_model_{mid}")
        # async_create_issue with an id that exists updates the issue,
        # so raising it on every cycle past the threshold is safe.
        for mid, since in self._model_missing_since.items():
            missing_for = (now - since).total_seconds()
            if missing_for >= STALE_MODEL_TOLERANCE_SECONDS:
                self._raise_stale_model_issue(mid, missing_for)
        self._missing_this_cycle = set()
        self._new_this_cycle = set()

    def _raise_stale_model_issue(self, model_id: int, missing_seconds: float) -> None:
        """Raise the Repairs issue that suggests removing the device of a missing model."""
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

        An exception that is not a SunSpecError is recorded as a
        TransportError, so the diagnostics dump always sees a
        categorised entry.
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
        # Drop the session now rather than leaving it flagged for the
        # next get_client(): a half-open TCP session to an inverter that
        # rebooted looks exactly like a slow one until the next timeout.
        await self.api.async_close(force=True)
        self.api.reconnect_next(rescan=not isinstance(wrapped, (TransportError, TransientError)))
        self._count_failed_cycle()
        raise UpdateFailed(str(wrapped)) from exc

    def _count_failed_cycle(self) -> None:
        """Count one failed cycle towards the stale-data tolerance."""
        self.consecutive_failed_cycles += 1
        # Home Assistant's DataUpdateCoordinator._async_refresh returns
        # early, without notifying listeners, when the previous and the
        # current refresh both failed. The entities would stay on their
        # last good value forever, so the step to "unavailable" is
        # driven here, on the cycle at which SunSpecEntity.available
        # starts to return False.
        if self.consecutive_failed_cycles == STALE_DATA_TOLERANCE_CYCLES + 1:
            self.async_update_listeners()

    def _link_profile(self) -> VendorProfile | None:
        """The vendor profile that decides how the link is used.

        The profile read from the device this run, or before that the
        profile of the identity stored with the layout. A restart
        therefore asks the link for the profile's timeout before the
        first request, not after the first cycle.
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
        """Record a categorised error and raise its Repairs issue when due."""
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
        # A PV inverter that powers its comms board down at dusk is
        # unreachable all night by design (#52). That is no reason for a
        # Repairs issue or for warnings in the log. Only a transport
        # error qualifies: any answer proves the inverter is awake, and
        # an awake inverter that misbehaves is still worth an issue.
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
            # One info line when the outage starts, so the log says when
            # the inverter went away, then debug. At warning level this
            # would be roughly 2400 lines a night at the default poll
            # interval.
            quiet_log = self._log.info if self._consecutive_failures[cat] == 1 else self._log.debug
            quiet_log(
                "Inverter is not answering and %s, so this is an expected standby "
                "rather than a fault (#%d in a row): %s",
                reason,
                self._consecutive_failures[cat],
                exc,
            )
        else:
            # A warning for the first error of its category in a row,
            # debug for the ones that follow. The count stays in the
            # line, and the Repairs issue below reports a fault that
            # lasts.
            # Nor during setup: every retry builds a new coordinator,
            # so each one would warn afresh, and Home Assistant logs the
            # failed setup itself.
            first_in_a_row = self._consecutive_failures[cat] == 1 and self.data is not None
            loud_log = self._log.warning if first_in_a_row else self._log.debug
            loud_log(
                "%s (#%d in a row): %s",
                exc.__class__.__name__,
                self._consecutive_failures[cat],
                exc,
            )
        if cat == "transient":
            return
        if expected:
            # The failure is in the ring buffer above, so a diagnostics
            # dump taken during the night still shows it.
            return
        # A protocol error is a configuration or compatibility problem,
        # never a transient state, so the first one is raised. A
        # transport or device error can be a glitch or a fault during a
        # state transition and has to repeat first.
        threshold = 1 if cat == "protocol" else 3
        if self._consecutive_failures[cat] >= threshold:
            self._raise_repair_issue(cat, exc)

    def _raise_repair_issue(self, category: str, exc: SunSpecError) -> None:
        """Create or update this entry's Repairs issue for one error category."""
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
        """Delete the category and stale-model Repairs issues of this coordinator.

        Args:
            force: Delete an issue the user has ignored as well. For
                teardown only, where the entry must leave nothing behind.
        """
        for category in CATEGORIES:
            # A transient error never raises an issue.
            if category == "transient":
                continue
            self._delete_issue(f"{self.entry.entry_id}_{category}", force=force)
        for mid in list(self._model_missing_since):
            self._delete_issue(f"{self.entry.entry_id}_stale_model_{mid}", force=force)

    def _delete_issue(self, issue_id: str, *, force: bool = False) -> None:
        """Delete one Repairs issue, unless the user has ignored it.

        Args:
            force: Delete an ignored issue as well, which loses the ignore.
        """
        # Home Assistant's ``IssueRegistry.async_delete`` pops the whole
        # entry, ``dismissed_version`` included, so deleting an ignored
        # issue revokes the ignore. On a device that is unreachable
        # every night the issue would come back unignored every evening
        # (#52). An ignored issue left in the registry is hidden by the
        # Repairs panel, and ``async_get_or_create`` keeps
        # ``dismissed_version`` when the same issue is raised again. A
        # core update does not reset the ignore (#160). A reload or
        # removal of the entry does, because its unload deletes with
        # ``force``.
        #
        # "Show ignored repairs" lists only active issues, and the
        # registry loads every non-persistent issue as inactive after a
        # restart. The user can then lift the ignore only once this
        # integration raises the issue again.
        if not force:
            issue = ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id)
            if issue is not None and issue.dismissed_version is not None:
                return
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)
