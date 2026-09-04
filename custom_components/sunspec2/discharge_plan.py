"""A scheduled discharge: the battery gives what it holds above a reserve back over a window.

#57: a GEN24 owner in an energy cooperative wants the battery to
deliver its charge above a reserve to the grid overnight, at a steady
power, without writing the automation. This module is that automation,
as entities on the battery device: a switch for the plan, the start
and end of the window, the state of charge to keep, and the battery's
capacity for the arithmetic.

At the start of the window the planner reads the state of charge from
model 124, takes the energy above the reserve, spreads it over the
window, and puts the battery into the vendor's "discharge to grid"
mode with that power. At the end of the window it sets the mode back
to automatic, and before that if a poll shows the battery already down
at the reserve. Switched on inside the window, or started inside it
after a restart, it plans for what is left of it.

Built where the battery modes are: a vendor with a storage profile and
a WChaMax above zero.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from datetime import time
from datetime import timedelta
from typing import Any

from homeassistant.components.number import NumberMode
from homeassistant.components.number import RestoreNumber
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.const import EntityCategory
from homeassistant.const import UnitOfEnergy
from homeassistant.core import CALLBACK_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import SunSpecDataUpdateCoordinator
from . import get_sunspec_unique_id
from .entity import SunSpecEntity
from .errors import SunSpecError
from .models import SunSpecModelWrapper
from .storage_modes import async_apply_storage_mode
from .storage_modes import storage_profile_ready
from .storage_modes import wchamax_of
from .vendors.profile import Rate
from .vendors.profile import StorageMode
from .write_controls import STORAGE_CONTROL_MODEL

_LOGGER: logging.Logger = logging.getLogger(__package__)

NAMEPLATE_MODEL = 120

#: How long after the switch restores its state the planner waits
#: before it looks at the window. The times and numbers restore in
#: other platforms, and this is long enough for all of them.
STARTUP_GRACE_SECONDS = 10.0

#: Fraction of the device's own revert timeout after which the plan
#: writes its mode again. The timer itself is set to the length of the
#: window, so this is the net under a device that clamped it to
#: something shorter, and the margin only has to cover a busy event
#: loop and a poll in flight: 30 s of the 300 a GEN24 came with. Not
#: half of it. A missed write costs one interval of discharge, and the
#: tick after it plans again.
REFRESH_FRACTION = 0.9

#: Floor under that interval, whatever timeout the device reports.
MIN_REFRESH_SECONDS = 30.0

#: Largest value ``InOutWRte_RvrtTms`` holds: it is a uint16 of seconds,
#: 18 h 12 min. Longer windows keep the device's timer at the ceiling
#: and rely on the periodic re-write for the rest.
MAX_REVERT_SECONDS = 65535


def window_hours(start: time, end: time) -> float:
    """The length of the window, wrapping past midnight; a full day when both are equal."""
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    if minutes <= 0:
        minutes += 24 * 60
    return minutes / 60.0


def hours_until(now: datetime, end: time) -> float:
    """Hours from ``now`` to the next ``end``, today or tomorrow."""
    end_at = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if end_at <= now:
        end_at += timedelta(days=1)
    return (end_at - now).total_seconds() / 3600.0


def in_window(at: time, start: time, end: time) -> bool:
    """Whether the time of day ``at`` lies in the window, wrapping past midnight."""
    if start <= end:
        return start <= at < end
    return at >= start or at < end


def planned_power_w(
    soc_pct: float,
    reserve_pct: float,
    capacity_kwh: float,
    hours: float,
    wchamax_w: float,
    step_w: float,
) -> float:
    """The steady power that empties the battery down to the reserve over ``hours``.

    Capped at the battery's WChaMax and rounded down to the vendor's
    step, so the battery never gives more than planned.
    """
    if hours <= 0 or capacity_kwh <= 0:
        return 0.0
    energy_kwh = max(0.0, (soc_pct - reserve_pct) / 100.0 * capacity_kwh)
    power = min(energy_kwh * 1000.0 / hours, wchamax_w)
    if step_w > 0:
        power = math.floor(power / step_w) * step_w
    return float(power)


def state_of_charge_pct(wrapper: SunSpecModelWrapper) -> float | None:
    """``ChaState`` of model 124 in percent, or None when the device does not report it."""
    try:
        value = wrapper.getValue("ChaState")
    except (KeyError, AttributeError):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def revert_timeout_seconds(wrapper: SunSpecModelWrapper) -> float | None:
    """``InOutWRte_RvrtTms`` of model 124, or None where the device does not report it.

    The point is the dead-man switch of the battery rates: the inverter
    honours them for that many seconds and then goes back to what it did
    before. Zero means it holds them until told otherwise, and is a
    value like any other here; None is a device that does not implement
    the point at all.
    """
    try:
        value = wrapper.getValue("InOutWRte_RvrtTms")
    except (KeyError, AttributeError):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def nameplate_capacity_kwh(coordinator: SunSpecDataUpdateCoordinator) -> float | None:
    """The storage energy rating from model 120, in kWh, or None when there is none."""
    wrapper = (coordinator.data or {}).get(NAMEPLATE_MODEL)
    if wrapper is None:
        return None
    try:
        value = wrapper.getValue("WHRtg")
    except (KeyError, AttributeError):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value) / 1000.0
    return None


@dataclass
class DischargePlanSettings:
    """What the entities set, restored by each of them across restarts."""

    enabled: bool = False
    start: time = time(20, 0)
    end: time = time(6, 0)
    reserve_pct: float = 10.0
    capacity_kwh: float | None = None


class DischargePlanner:
    """Runs the plan: the window's triggers and the writes they cause.

    One per config entry, held on the coordinator, shared by the five
    entities. ``async_stop`` goes on the entry's unload.
    """

    def __init__(self, hass: HomeAssistant, coordinator: SunSpecDataUpdateCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.settings = DischargePlanSettings()
        #: The power the last plan asked for, None before the first plan.
        self.planned_power_w: float | None = None
        self._unsub_window: list[CALLBACK_TYPE] = []
        self._unsub_startup: CALLBACK_TYPE | None = None
        self._unsub_refresh: CALLBACK_TYPE | None = None
        self._unsub_poll: CALLBACK_TYPE | None = None
        #: What ``InOutWRte_RvrtTms`` held before the plan took it over,
        #: put back at the end of the window.
        self._prior_revert_seconds: float | None = None
        self._revert_owned = False
        self._listeners: list[Callable[[], None]] = []
        self._capacity_warned = False
        self._track_window()

    # ----- lifecycle -------------------------------------------------------

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsub_window:
            unsub()
        self._unsub_window = []
        if self._unsub_startup is not None:
            self._unsub_startup()
            self._unsub_startup = None
        self._stop_watching()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call ``listener`` whenever the plan changes something an entity shows."""
        self._listeners.append(listener)

        def _remove() -> None:
            self._listeners.remove(listener)

        return _remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def _track_window(self) -> None:
        for unsub in self._unsub_window:
            unsub()
        start, end = self.settings.start, self.settings.end
        self._unsub_window = [
            async_track_time_change(
                self.hass, self._on_start, hour=start.hour, minute=start.minute, second=0
            ),
            async_track_time_change(
                self.hass, self._on_end, hour=end.hour, minute=end.minute, second=0
            ),
        ]

    async def _on_start(self, now: datetime) -> None:
        await self.async_at_start()

    async def _on_end(self, now: datetime) -> None:
        await self.async_at_end()

    # ----- what the entities call ----------------------------------------

    @callback
    def async_set_window(self, start: time | None = None, end: time | None = None) -> None:
        if start is not None:
            self.settings.start = start
        if end is not None:
            self.settings.end = end
        self._track_window()

    async def async_enable(self) -> None:
        """Switch the plan on; inside the window, plan for what is left of it."""
        self.settings.enabled = True
        await self.async_resume()

    async def async_disable(self) -> None:
        """Switch the plan off; inside the window, hand the battery back."""
        self.settings.enabled = False
        self._stop_watching()
        if self._inside_window_now():
            await self._set_automatic()

    @callback
    def async_check_after_restore(self) -> None:
        """Look at the window once every entity has had time to restore its state."""
        if self._unsub_startup is not None:
            return

        async def _resume(_now: datetime) -> None:
            self._unsub_startup = None
            await self.async_resume()

        self._unsub_startup = async_call_later(self.hass, STARTUP_GRACE_SECONDS, _resume)

    async def async_resume(self) -> None:
        """Plan for the rest of the window, if the plan is on and the window is open."""
        if not self.settings.enabled or not self._inside_window_now():
            return
        await self._apply(hours_until(dt_util.now(), self.settings.end))

    async def async_at_start(self) -> None:
        """The start of the window: plan the whole of it."""
        if not self.settings.enabled:
            return
        await self._apply(window_hours(self.settings.start, self.settings.end))

    async def async_at_end(self) -> None:
        """The end of the window: the battery goes back to automatic.

        Whether or not this run planned anything, so a restart inside
        the window, which forgets what was planned, does not leave the
        battery discharging past the end.
        """
        if not self.settings.enabled:
            return
        await self._set_automatic()

    # ----- the plan itself -------------------------------------------------

    def _inside_window_now(self) -> bool:
        return in_window(dt_util.now().time(), self.settings.start, self.settings.end)

    async def _apply(self, hours: float) -> None:
        ready = storage_profile_ready(self.coordinator)
        if ready is None:
            _LOGGER.warning("Scheduled discharge: the device reports no battery, nothing planned")
            return
        profile, wrapper = ready
        soc = state_of_charge_pct(wrapper)
        capacity = self.settings.capacity_kwh
        wchamax = wchamax_of(wrapper)
        if capacity is None or capacity <= 0:
            if not self._capacity_warned:
                _LOGGER.warning(
                    "Scheduled discharge: set the battery capacity entity first, "
                    "the plan cannot turn a state of charge into watts without it"
                )
                self._capacity_warned = True
            return
        if soc is None or wchamax is None:
            _LOGGER.warning(
                "Scheduled discharge: no state of charge from the device, nothing planned"
            )
            return
        power = planned_power_w(
            soc, self.settings.reserve_pct, capacity, hours, wchamax, profile.grid_power_step_w
        )
        was_discharging = bool(self.planned_power_w)
        self.planned_power_w = power
        self._notify()
        if power < profile.grid_power_step_w:
            _LOGGER.info(
                "Scheduled discharge: %.0f %% charge is not above the %.0f %% reserve, "
                "nothing planned",
                soc,
                self.settings.reserve_pct,
            )
            if was_discharging:
                # The battery reached the reserve before the window was
                # over. Hand it back rather than leave it in a forced
                # mode that nothing writes again.
                await self._set_automatic()
            return
        self.coordinator.storage_setpoints[Rate.GRID_DISCHARGE.value] = power
        await self._arm_revert_timer(wrapper, hours)
        try:
            await async_apply_storage_mode(self.coordinator, profile, StorageMode.DISCHARGE_TO_GRID)
        except (SunSpecError, HomeAssistantError) as exc:
            _LOGGER.error("Scheduled discharge: could not start discharging: %s", exc)
        else:
            _LOGGER.info(
                "Scheduled discharge: %.0f W for %.1f h, from %.0f %% down to the %.0f %% reserve",
                power,
                hours,
                soc,
                self.settings.reserve_pct,
            )
        # Armed whether or not that write landed: the reserve has to be
        # watched either way, and a link that was down for this write
        # may be back for the next.
        self._watch_discharge()

    async def _arm_revert_timer(self, wrapper: SunSpecModelWrapper, hours: float) -> None:
        """Point the device's own revert timer at the end of the window.

        The battery rates are a dead-man switch: the inverter drops them
        after ``InOutWRte_RvrtTms`` seconds. A GEN24 ships with 300 of
        them, which is what ended #57's discharge after five minutes.
        Set to the length of the window, the switch does what the plan
        wants of it: with Home Assistant gone, the battery stops at the
        end of the window rather than five minutes in or never.

        Best effort, and not the only line of defence. A device that
        refuses the write or clamps it back to a shorter timeout keeps
        what it has, and the periodic re-write, paced off the value the
        device reports back, carries the discharge either way.
        """
        if not self._revert_owned:
            self._prior_revert_seconds = revert_timeout_seconds(wrapper)
            self._revert_owned = True
        seconds = max(1, min(MAX_REVERT_SECONDS, int(hours * 3600)))
        try:
            await self.coordinator.async_write_points_locked(
                STORAGE_CONTROL_MODEL, [("InOutWRte_RvrtTms", seconds)]
            )
        except (SunSpecError, HomeAssistantError) as exc:
            _LOGGER.warning(
                "Scheduled discharge: could not set the battery rate revert time to %d s, "
                "the discharge falls back on being written again periodically: %s",
                seconds,
                exc,
            )

    async def _release_revert_timer(self) -> None:
        """Put ``InOutWRte_RvrtTms`` back to what it held before the plan ran."""
        if not self._revert_owned:
            return
        prior = self._prior_revert_seconds
        self._revert_owned = False
        self._prior_revert_seconds = None
        if prior is None:
            return
        try:
            await self.coordinator.async_write_points_locked(
                STORAGE_CONTROL_MODEL, [("InOutWRte_RvrtTms", int(prior))]
            )
        except (SunSpecError, HomeAssistantError) as exc:
            _LOGGER.warning(
                "Scheduled discharge: could not restore the battery rate revert time to %d s: %s",
                int(prior),
                exc,
            )

    def _watch_discharge(self) -> None:
        """Watch the running discharge: every poll, and the device's own timer.

        The polls carry the state of charge already, so they are what
        the plan watches the reserve with. They cost nothing and they
        are the only thing here quick enough: the power is an estimate
        built on a capacity a user typed, over a battery the house
        draws on as well, and being wrong about it by an hour at four
        kilowatts is most of a battery.

        The timer is the other half, and only where the device has one:
        an inverter that drops a battery rate after so many seconds is
        written again before it does. Paced off what the device reports
        now, not off what the plan asked for, so one that clamped the
        timer to its own maximum is still covered.
        """
        self._stop_watching()
        self._unsub_poll = self.coordinator.async_add_listener(self._on_poll)
        wrapper = (self.coordinator.data or {}).get(STORAGE_CONTROL_MODEL)
        timeout = revert_timeout_seconds(wrapper) if wrapper is not None else None
        if not timeout:
            return
        interval = max(MIN_REFRESH_SECONDS, timeout * REFRESH_FRACTION)

        async def _refresh(_now: datetime) -> None:
            self._unsub_refresh = None
            # Through async_resume: it checks the window again and
            # plans for what is left of it, so a battery that gave more
            # than planned, to the house for instance, still lands on
            # the reserve at the end rather than below it.
            await self.async_resume()

        self._unsub_refresh = async_call_later(self.hass, interval, _refresh)

    @callback
    def _on_poll(self) -> None:
        """Hand the battery back as soon as a poll shows it down at the reserve.

        Until this, the reserve was only ever a divisor: the plan aimed
        at it when it worked out the power, and nothing looked again to
        see whether the battery had arrived early. It does arrive
        early, because the house discharges the same battery, and the
        plan then held a forced discharge below the reserve for the
        rest of the window.
        """
        if not self.settings.enabled or not self.planned_power_w:
            return
        ready = storage_profile_ready(self.coordinator)
        if ready is None:
            return
        soc = state_of_charge_pct(ready[1])
        if soc is None or soc > self.settings.reserve_pct:
            return
        _LOGGER.info(
            "Scheduled discharge: the battery is down to %.0f %%, at the %.0f %% reserve, "
            "handing it back before the end of the window",
            soc,
            self.settings.reserve_pct,
        )
        # Before the task rather than in it: the next poll must not find
        # a plan that still looks like it is discharging and stop it twice.
        self._stop_watching()
        self.planned_power_w = 0.0
        self.hass.async_create_task(self._set_automatic())

    @callback
    def _stop_watching(self) -> None:
        """Stop watching: the poll listener and the periodic write both go."""
        if self._unsub_refresh is not None:
            self._unsub_refresh()
            self._unsub_refresh = None
        if self._unsub_poll is not None:
            self._unsub_poll()
            self._unsub_poll = None

    async def _set_automatic(self) -> None:
        self._stop_watching()
        ready = storage_profile_ready(self.coordinator)
        if ready is None:
            return
        profile, _ = ready
        try:
            await async_apply_storage_mode(self.coordinator, profile, StorageMode.AUTO)
        except (SunSpecError, HomeAssistantError) as exc:
            _LOGGER.error("Scheduled discharge: could not hand the battery back: %s", exc)
            return
        # After the mode, not before: the timer only matters while a
        # rate is in force, and the short one the device came with must
        # not lapse between the two writes.
        await self._release_revert_timer()
        self.planned_power_w = 0.0
        self._notify()


def planner_for(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry
) -> DischargePlanner:
    """The entry's planner, built on first use and stopped when the entry unloads."""
    if coordinator.discharge_plan is None:
        coordinator.discharge_plan = DischargePlanner(coordinator.hass, coordinator)
        config_entry.async_on_unload(coordinator.discharge_plan.async_stop)
    return coordinator.discharge_plan


class _PlanEntity(SunSpecEntity):
    """What the five entities share: the planner and a unique id under model 124."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        device_info: SunSpecModelWrapper,
        model_info: dict[str, Any],
        prefix: str,
        planner: DischargePlanner,
        key: str,
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            device_info,
            model_info,
            prefix=prefix,
            model_id=STORAGE_CONTROL_MODEL,
        )
        self._planner = planner
        self._attr_translation_key = key
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, key, STORAGE_CONTROL_MODEL, 0
        )


class DischargePlanSwitch(_PlanEntity, SwitchEntity, RestoreEntity):
    """The plan itself. Its attributes show what the last plan asked for."""

    _attr_icon = "mdi:battery-clock"

    @property
    def is_on(self) -> bool:
        return self._planner.settings.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        settings = self._planner.settings
        return {
            "planned_power_w": self._planner.planned_power_w,
            "window_hours": window_hours(settings.start, settings.end),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._planner.async_add_listener(self.async_write_ha_state))
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self._planner.settings.enabled = True
            self._planner.async_check_after_restore()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._planner.async_enable()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._planner.async_disable()
        self.async_write_ha_state()


class DischargePlanTime(_PlanEntity, TimeEntity, RestoreEntity):
    """The start or the end of the window."""

    _attr_icon = "mdi:clock-outline"

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        device_info: SunSpecModelWrapper,
        model_info: dict[str, Any],
        prefix: str,
        planner: DischargePlanner,
        which: str,
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            device_info,
            model_info,
            prefix,
            planner,
            f"scheduled_discharge_{which}",
        )
        self._which = which

    @property
    def native_value(self) -> time | None:
        value: time = getattr(self._planner.settings, self._which)
        return value

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        try:
            restored = time.fromisoformat(last.state)
        except ValueError:
            return
        self._planner.async_set_window(**{self._which: restored})

    async def async_set_value(self, value: time) -> None:
        self._planner.async_set_window(**{self._which: value})
        self.async_write_ha_state()


class DischargePlanReserveNumber(_PlanEntity, RestoreNumber):
    """The state of charge the plan leaves in the battery."""

    _attr_icon = "mdi:battery-low"
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0

    @property
    def native_value(self) -> float:
        return self._planner.settings.reserve_pct

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._planner.settings.reserve_pct = float(last.native_value)

    async def async_set_native_value(self, value: float) -> None:
        self._planner.settings.reserve_pct = float(value)
        self.async_write_ha_state()


class DischargePlanCapacityNumber(_PlanEntity, RestoreNumber):
    """The battery's usable capacity, what turns a state of charge into watts.

    Pre-filled from the nameplate model where the device has one;
    otherwise the plan waits for a value.
    """

    _attr_icon = "mdi:battery-high"
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_native_min_value = 0.0
    _attr_native_max_value = 1000.0
    _attr_native_step = 0.1

    @property
    def native_value(self) -> float | None:
        return self._planner.settings.capacity_kwh

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None and last.native_value > 0:
            self._planner.settings.capacity_kwh = float(last.native_value)
            return
        self._planner.settings.capacity_kwh = nameplate_capacity_kwh(self.coordinator)

    async def async_set_native_value(self, value: float) -> None:
        self._planner.settings.capacity_kwh = float(value) if value > 0 else None
        self.async_write_ha_state()


def _plan_context(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> tuple[SunSpecModelWrapper, dict[str, Any], DischargePlanner] | None:
    ready = storage_profile_ready(coordinator)
    if ready is None or coordinator.device_info is None:
        return None
    _, wrapper = ready
    return coordinator.device_info, wrapper.getGroupMeta(), planner_for(coordinator, config_entry)


def discharge_plan_switch(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> list[DischargePlanSwitch]:
    """The plan's switch for this entry, or an empty list when the plan does not apply."""
    context = _plan_context(coordinator, config_entry, prefix)
    if context is None:
        return []
    device_info, model_info, planner = context
    return [
        DischargePlanSwitch(
            coordinator,
            config_entry,
            device_info,
            model_info,
            prefix,
            planner,
            "scheduled_discharge",
        )
    ]


def discharge_plan_times(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> list[DischargePlanTime]:
    """The window's start and end for this entry, or an empty list."""
    context = _plan_context(coordinator, config_entry, prefix)
    if context is None:
        return []
    device_info, model_info, planner = context
    return [
        DischargePlanTime(
            coordinator, config_entry, device_info, model_info, prefix, planner, which=which
        )
        for which in ("start", "end")
    ]


def discharge_plan_numbers(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> list[RestoreNumber]:
    """The reserve and the capacity for this entry, or an empty list."""
    context = _plan_context(coordinator, config_entry, prefix)
    if context is None:
        return []
    device_info, model_info, planner = context
    return [
        DischargePlanReserveNumber(
            coordinator,
            config_entry,
            device_info,
            model_info,
            prefix,
            planner,
            "scheduled_discharge_reserve_pct",
        ),
        DischargePlanCapacityNumber(
            coordinator,
            config_entry,
            device_info,
            model_info,
            prefix,
            planner,
            "battery_capacity_kwh",
        ),
    ]
