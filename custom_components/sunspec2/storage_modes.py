"""Battery modes in watts: a Select of vendor-defined modes and the Numbers it uses.

Vendor-agnostic on purpose. The modes, the recipes and the sign
conventions come from the vendor profile (see ``vendors/``); this module
turns them into entities. The Select writes the profile's recipe for a
mode. The Numbers hold the watts the recipes fill in, restored across
restarts, because the registers can hold only what is active: a grid
charge power has nowhere to live in the device while the battery is in
a PV charge limit. Both read the device for their state rather than
remembering what they asked for.

Only built when the vendor has a storage profile and model 124 reports
a WChaMax above zero, which is how the device says it has a battery.
No option to tick: the battery block is not behind the write beta.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberMode
from homeassistant.components.number import RestoreNumber
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.const import UnitOfPower
from homeassistant.exceptions import HomeAssistantError

from . import SunSpecDataUpdateCoordinator
from . import get_sunspec_unique_id
from .entity import SunSpecEntity
from .errors import SunSpecError
from .models import SunSpecModelWrapper
from .vendors.profile import POINT_OF_RATE
from .vendors.profile import SETPOINT_RATES
from .vendors.profile import Rate
from .vendors.profile import StorageMode
from .vendors.profile import StorageModeProfile
from .vendors.profile import resolve_rate
from .write_controls import STORAGE_CONTROL_MODEL
from .write_controls import storage_bits_to_int

_LOGGER: logging.Logger = logging.getLogger(__package__)

_SETPOINT_PRESENTATION: dict[Rate, tuple[str, str]] = {
    Rate.CHARGE_LIMIT: ("battery_pv_charge_limit_w", "mdi:battery-charging-medium"),
    Rate.DISCHARGE_LIMIT: ("battery_discharge_limit_w", "mdi:battery-arrow-down"),
    Rate.GRID_CHARGE: ("battery_grid_charge_power_w", "mdi:transmission-tower-import"),
    Rate.GRID_DISCHARGE: ("battery_grid_discharge_power_w", "mdi:transmission-tower-export"),
}


def storage_profile_ready(
    coordinator: SunSpecDataUpdateCoordinator,
) -> tuple[StorageModeProfile, SunSpecModelWrapper] | None:
    """The vendor's storage profile and the model 124 wrapper, when the device has a battery."""
    vendor = coordinator.vendor
    if vendor is None or vendor.storage is None:
        return None
    wrapper = (coordinator.data or {}).get(STORAGE_CONTROL_MODEL)
    if wrapper is None or wchamax_of(wrapper) is None:
        return None
    return vendor.storage, wrapper


def wchamax_of(wrapper: SunSpecModelWrapper) -> float | None:
    """WChaMax in watts, or None when the point is missing or zero (no battery)."""
    try:
        value = wrapper.getValue("WChaMax")
    except (KeyError, AttributeError):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def device_rates(wrapper: SunSpecModelWrapper) -> tuple[int, float, float] | None:
    """(ctl_mod, in_pct, out_pct) as the device reports them, or None if a point is missing."""
    try:
        ctl = wrapper.getValue("StorCtl_Mod")
        in_pct = wrapper.getValue("InWRte")
        out_pct = wrapper.getValue("OutWRte")
    except (KeyError, AttributeError):
        return None
    if isinstance(ctl, list):
        ctl = storage_bits_to_int(ctl)
    if not isinstance(ctl, (int, float)) or isinstance(ctl, bool):
        return None
    if not isinstance(in_pct, (int, float)) or not isinstance(out_pct, (int, float)):
        return None
    return int(ctl), float(in_pct), float(out_pct)


class StorageModeSelect(SunSpecEntity, SelectEntity):
    """The battery's mode, as the vendor names it."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "battery_mode"
    _attr_icon = "mdi:battery-sync"

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        device_info: SunSpecModelWrapper,
        model_info: dict[str, Any],
        prefix: str,
        profile: StorageModeProfile,
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            device_info,
            model_info,
            prefix=prefix,
            model_id=STORAGE_CONTROL_MODEL,
        )
        self._profile = profile
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, "storage_mode", STORAGE_CONTROL_MODEL, 0
        )
        self._attr_options = [mode.value for mode in profile.modes]

    @property
    def current_option(self) -> str | None:
        wrapper = self.coordinator.data.get(STORAGE_CONTROL_MODEL)
        if wrapper is None:
            return None
        rates = device_rates(wrapper)
        if rates is None:
            return None
        mode = self._profile.infer_mode(*rates)
        return mode.value if mode is not None else None

    async def async_select_option(self, option: str) -> None:
        try:
            mode = StorageMode(option)
            self._profile.recipes[mode]
        except (ValueError, KeyError) as exc:
            raise HomeAssistantError(f"{option} is not a battery mode this device offers") from exc
        try:
            await async_apply_storage_mode(self.coordinator, self._profile, mode)
        except SunSpecError as exc:
            raise HomeAssistantError(f"Failed to set battery mode {option}: {exc}") from exc


async def async_apply_storage_mode(
    coordinator: SunSpecDataUpdateCoordinator,
    profile: StorageModeProfile,
    mode: StorageMode,
) -> None:
    """Put the battery into ``mode``, with the watt setpoints the coordinator holds.

    Raises:
        HomeAssistantError: The device reports no battery.
        SunSpecError: A write failed.
    """
    recipe = profile.recipes[mode]
    wrapper = coordinator.data.get(STORAGE_CONTROL_MODEL)
    wchamax = wchamax_of(wrapper) if wrapper is not None else None
    if wchamax is None:
        raise HomeAssistantError("The inverter reports no battery (WChaMax is 0)")
    setpoints = coordinator.storage_setpoints
    in_pct = resolve_rate(recipe.in_rate, setpoints, wchamax)
    out_pct = resolve_rate(recipe.out_rate, setpoints, wchamax)
    # The rates first, in one frame (the two registers are adjacent),
    # the mode last. Mode first leaves the inverter acting on the
    # previous rates for a moment, and a window it cannot serve is
    # refused with Modbus exception 3 (callifo/fronius_modbus#126).
    await coordinator.async_write_points_locked(
        STORAGE_CONTROL_MODEL, [("OutWRte", out_pct), ("InWRte", in_pct)]
    )
    await coordinator.async_write_points_locked(
        STORAGE_CONTROL_MODEL, [("StorCtl_Mod", recipe.ctl_mod)]
    )
    # Outside the lock: the refresh debouncer runs inline and
    # asyncio.Lock is not reentrant.
    await coordinator.async_request_refresh()


class StorageSetpointNumber(SunSpecEntity, RestoreNumber):
    """One of the watt values the mode recipes fill in.

    Its state is the setpoint, not a register: the register holds this
    value only while a mode uses it, and holds another setpoint's value
    otherwise. While the value is in use, changing it writes the register
    at once; otherwise the next mode change picks it up.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 0.0

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        device_info: SunSpecModelWrapper,
        model_info: dict[str, Any],
        prefix: str,
        profile: StorageModeProfile,
        rate: Rate,
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            device_info,
            model_info,
            prefix=prefix,
            model_id=STORAGE_CONTROL_MODEL,
        )
        self._profile = profile
        self._rate = rate
        translation_key, icon = _SETPOINT_PRESENTATION[rate]
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, f"{rate.value}_w", STORAGE_CONTROL_MODEL, 0
        )
        self._attr_native_step = (
            profile.grid_power_step_w if rate in (Rate.GRID_CHARGE, Rate.GRID_DISCHARGE) else 1.0
        )

    @property
    def native_max_value(self) -> float:
        wrapper = self.coordinator.data.get(STORAGE_CONTROL_MODEL)
        wchamax = wchamax_of(wrapper) if wrapper is not None else None
        return wchamax if wchamax is not None else 0.0

    @property
    def native_value(self) -> float | None:
        return self.coordinator.storage_setpoints.get(self._rate.value)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        setpoints = self.coordinator.storage_setpoints
        if self._rate.value in setpoints:
            return
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            setpoints[self._rate.value] = float(last.native_value)
            return
        # First run: a limit that limits nothing, a grid power of none.
        # The device's own maximum is the honest starting point for a
        # limit; 0 W is the honest one for a forced power the user has
        # not chosen yet.
        if self._rate in (Rate.CHARGE_LIMIT, Rate.DISCHARGE_LIMIT):
            setpoints[self._rate.value] = self.native_max_value
        else:
            setpoints[self._rate.value] = 0.0

    def _in_use(self) -> bool:
        """True while the mode the device is in writes this setpoint's register."""
        wrapper = self.coordinator.data.get(STORAGE_CONTROL_MODEL)
        if wrapper is None:
            return False
        rates = device_rates(wrapper)
        if rates is None:
            return False
        mode = self._profile.infer_mode(*rates)
        return mode is not None and self._rate in self._profile.rates_of(mode)

    async def async_set_native_value(self, value: float) -> None:
        step = self._attr_native_step
        if step and step > 1:
            value = round(value / step) * step
        self.coordinator.storage_setpoints[self._rate.value] = float(value)
        self.async_write_ha_state()
        if not self._in_use():
            return
        wrapper = self.coordinator.data.get(STORAGE_CONTROL_MODEL)
        wchamax = wchamax_of(wrapper) if wrapper is not None else None
        if wchamax is None:
            return
        pct = resolve_rate(self._rate, self.coordinator.storage_setpoints, wchamax)
        point = POINT_OF_RATE[self._rate]
        try:
            await self.coordinator.async_write_points_locked(STORAGE_CONTROL_MODEL, [(point, pct)])
        except SunSpecError as exc:
            raise HomeAssistantError(f"Failed to write {point}={pct}: {exc}") from exc
        await self.coordinator.async_request_refresh()


def storage_mode_select(
    coordinator: SunSpecDataUpdateCoordinator,
    config_entry: ConfigEntry,
    prefix: str,
) -> list[StorageModeSelect]:
    """The mode Select for this entry, or an empty list when it does not apply."""
    ready = storage_profile_ready(coordinator)
    if ready is None or coordinator.device_info is None:
        return []
    profile, wrapper = ready
    return [
        StorageModeSelect(
            coordinator=coordinator,
            config_entry=config_entry,
            device_info=coordinator.device_info,
            model_info=wrapper.getGroupMeta(),
            prefix=prefix,
            profile=profile,
        )
    ]


def storage_setpoint_numbers(
    coordinator: SunSpecDataUpdateCoordinator,
    config_entry: ConfigEntry,
    prefix: str,
) -> list[StorageSetpointNumber]:
    """The four watt Numbers for this entry, or an empty list when they do not apply."""
    ready = storage_profile_ready(coordinator)
    if ready is None or coordinator.device_info is None:
        return []
    profile, wrapper = ready
    return [
        StorageSetpointNumber(
            coordinator=coordinator,
            config_entry=config_entry,
            device_info=coordinator.device_info,
            model_info=wrapper.getGroupMeta(),
            prefix=prefix,
            profile=profile,
            rate=rate,
        )
        for rate in SETPOINT_RATES
    ]
