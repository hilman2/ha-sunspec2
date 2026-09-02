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
to automatic. Switched on inside the window, or started inside it
after a restart, it plans for what is left of it.

Built where the battery modes are: the write beta on, a vendor with a
storage profile, a WChaMax above zero.
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
        self.planned_power_w = power
        self._notify()
        if power < profile.grid_power_step_w:
            _LOGGER.info(
                "Scheduled discharge: %.0f %% charge is not above the %.0f %% reserve, "
                "nothing planned",
                soc,
                self.settings.reserve_pct,
            )
            return
        self.coordinator.storage_setpoints[Rate.GRID_DISCHARGE.value] = power
        try:
            await async_apply_storage_mode(self.coordinator, profile, StorageMode.DISCHARGE_TO_GRID)
        except (SunSpecError, HomeAssistantError) as exc:
            _LOGGER.error("Scheduled discharge: could not start discharging: %s", exc)
            return
        _LOGGER.info(
            "Scheduled discharge: %.0f W for %.1f h, from %.0f %% down to the %.0f %% reserve",
            power,
            hours,
            soc,
            self.settings.reserve_pct,
        )

    async def _set_automatic(self) -> None:
        ready = storage_profile_ready(self.coordinator)
        if ready is None:
            return
        profile, _ = ready
        try:
            await async_apply_storage_mode(self.coordinator, profile, StorageMode.AUTO)
        except (SunSpecError, HomeAssistantError) as exc:
            _LOGGER.error("Scheduled discharge: could not hand the battery back: %s", exc)
            return
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
