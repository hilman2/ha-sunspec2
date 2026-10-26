"""FranklinWH battery controls over the shared Home Assistant Modbus connection.

The watt sequence and zero-power handoff are adapted from
david2069/franklinwh-modbus, commit 9afbb9f2c1b81cdea42784dd879e3fe4f75e892d.
Copyright (c) 2026 david2069, MIT license; see LICENSE.
Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.number import NumberMode
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

from . import SunSpecDataUpdateCoordinator
from . import get_sunspec_unique_id
from .entity import Home
from .entity import SunSpecEntity
from .entity import home_for
from .errors import DeviceError
from .errors import SunSpecError
from .models import SunSpecModelWrapper
from .vendor_blocks import raw_device_info
from .vendors.franklinwh import AC_CAPACITY_MODEL
from .vendors.franklinwh import CONTROL_MODEL
from .vendors.franklinwh import DC_MODEL
from .vendors.franklinwh import RAW_DEVICES

ENABLE_SETTLE_SECONDS = 0.3
VERIFY_SETTLE_SECONDS = 0.5
HANDOFF_SECONDS = 1.0
STATE_DEADBAND_W = 50.0

# The smallest aPower is rated 5000 W, and the reference aGate reports
# 5000 W in both rating points. An aGate X with 30 kWh of batteries,
# firmware V10R11B40D00 by its owner's account, reported a charge rating
# of 160 and a discharge rating of 192 at W_SF 0, beside WMaxRtg 19200
# (debug log on issue #145, reported as a bug in #149). power_limits
# treats a rating below this floor like a missing one and uses WMaxRtg
# instead.
MIN_PLAUSIBLE_RATING_W = 1000.0


def _point(wrapper: SunSpecModelWrapper, name: str, index: int = 0) -> Any:
    try:
        return wrapper.getValue(name, index)
    except (KeyError, AttributeError, IndexError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def power_limits(wrapper: SunSpecModelWrapper | None) -> tuple[float, float] | None:
    """Reported charge and discharge ratings in watts, or None without valid ratings.

    A direction whose rating is missing or below MIN_PLAUSIBLE_RATING_W
    takes WMaxRtg instead, subject to the same floor.
    """
    if wrapper is None:
        return None

    def plausible(name: str) -> float | None:
        rating = _number(_point(wrapper, name))
        return rating if rating is not None and rating >= MIN_PLAUSIBLE_RATING_W else None

    fallback = plausible("WMaxRtg")
    ratings = []
    for name in ("WChaRteMaxRtg", "WDisChaRteMaxRtg"):
        rating = plausible(name)
        if rating is None:
            rating = fallback
        if rating is None:
            return None
        ratings.append(rating)
    return ratings[0], ratings[1]


def battery_power(wrapper: SunSpecModelWrapper | None) -> float | None:
    """The sum of model 714 aggregate powers, or None when any aggregate is unavailable."""
    if wrapper is None:
        return None
    total = 0.0
    for index in range(wrapper.num_models):
        power = _number(_point(wrapper, "DCW", index))
        if power is None:
            return None
        # DCW already aggregates the ports. Adding the port powers too
        # would double-count on a populated block, while the V12 snapshot
        # has a zero-filled placeholder port alongside valid aggregate DCW.
        total += power
    return total


def _enabled(wrapper: SunSpecModelWrapper) -> bool:
    value = _point(wrapper, "WSetEna")
    if value in (0, "DISABLED"):
        return False
    if value in (1, "ENABLED"):
        return True
    raise DeviceError("The aGate did not report whether battery control is enabled")


def _watts_mode(wrapper: SunSpecModelWrapper) -> bool:
    return _point(wrapper, "WSetMod") in (1, "WATTS")


def _ready(coordinator: SunSpecDataUpdateCoordinator, model: int) -> bool:
    return (
        coordinator.vendor is not None
        and coordinator.vendor.slug == "franklinwh"
        and model in (coordinator.data or {})
    )


async def async_set_power(coordinator: SunSpecDataUpdateCoordinator, watts: float | None) -> None:
    """Set signed battery watts, or release control when watts is None.

    Positive discharges, negative charges and zero holds standby.
    Raises HomeAssistantError when the value is outside the device's
    ratings, a write fails, or the settings do not read back correctly.
    """
    if not _ready(coordinator, CONTROL_MODEL):
        raise HomeAssistantError("FranklinWH battery controls are not available")
    if watts is not None and _number(watts) is None:
        raise HomeAssistantError("Battery power must be a finite number")
    try:
        async with coordinator.async_write_transaction():
            controls = await coordinator.api.async_get_data(CONTROL_MODEL)
            enabled = _enabled(controls)
            if watts is None:
                if enabled:
                    await coordinator.api.async_write_points(
                        CONTROL_MODEL, [("WSet", 0), ("WSetPct", 0)]
                    )
                    await asyncio.sleep(HANDOFF_SECONDS)
                await coordinator.api.async_write_points(
                    CONTROL_MODEL, [("WSetEna", 0), ("WSet", 0), ("WSetPct", 0)]
                )
            else:
                if watts != 0:
                    capacity = await coordinator.api.async_get_data(AC_CAPACITY_MODEL)
                    limits = power_limits(capacity)
                    if limits is None:
                        raise DeviceError("The aGate reports no usable battery power ratings")
                    charge, discharge = limits
                    if not -charge <= watts <= discharge:
                        raise DeviceError(
                            f"Battery target {watts:g} W exceeds the reported range "
                            f"{-charge:g} to {discharge:g} W"
                        )
                # Mode and int32 watts are adjacent. Flush them as one
                # FC16 before enabling; an already enabled command needs
                # neither an off/on cycle nor another enable write.
                await coordinator.api.async_write_points(
                    CONTROL_MODEL, [("WSetMod", 1), ("WSet", watts)]
                )
                if not enabled:
                    await asyncio.sleep(ENABLE_SETTLE_SECONDS)
                    await coordinator.api.async_write_points(CONTROL_MODEL, [("WSetEna", 1)])
            await asyncio.sleep(VERIFY_SETTLE_SECONDS)
            confirmed = await coordinator.api.async_get_data(CONTROL_MODEL)
            actual = _number(_point(confirmed, "WSet"))
            if watts is None:
                applied = (
                    not _enabled(confirmed)
                    and actual == 0
                    and _number(_point(confirmed, "WSetPct")) == 0
                )
            else:
                scale = _number(_point(confirmed, "WSet_SF"))
                tolerance = 10**scale / 2 if scale is not None else 0
                applied = (
                    _enabled(confirmed)
                    and _watts_mode(confirmed)
                    and actual is not None
                    and math.isclose(actual, watts, rel_tol=0, abs_tol=tolerance)
                )
            if not applied:
                raise DeviceError("The aGate acknowledged the command but its readback differs")
    except SunSpecError as exc:
        raise HomeAssistantError(f"FranklinWH battery command failed: {exc}") from exc
    # Publish the verified control block without waiting for an unrelated
    # full poll. The other models and failure counters retain their age.
    if coordinator.data is not None:
        coordinator.data[CONTROL_MODEL] = confirmed
    coordinator.async_update_listeners()


class FranklinEntity(SunSpecEntity):
    """An additional entity on an existing SunSpec model device."""

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        entry: ConfigEntry,
        prefix: str,
        home: Home,
        key: str,
    ) -> None:
        super().__init__(
            coordinator, entry, home.device_info, home.model_info, prefix, home.model_id
        )
        self._attr_translation_key = key
        self._attr_unique_id = get_sunspec_unique_id(entry.entry_id, f"franklinwh:{key}", 0, 0)


class FranklinPowerNumber(FranklinEntity, NumberEntity):
    """A bounded watt target that configures, enables and verifies the dispatch."""

    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "W"
    _attr_native_step = 1.0
    _attr_icon = "mdi:battery-sync"

    @property
    def available(self) -> bool:
        return (
            super().available
            and power_limits(self.coordinator.data.get(AC_CAPACITY_MODEL)) is not None
        )

    @property
    def native_min_value(self) -> float:
        limits = power_limits(self.coordinator.data.get(AC_CAPACITY_MODEL))
        return -limits[0] if limits is not None else 0

    @property
    def native_max_value(self) -> float:
        limits = power_limits(self.coordinator.data.get(AC_CAPACITY_MODEL))
        return limits[1] if limits is not None else 0

    @property
    def native_value(self) -> float | None:
        wrapper = self.coordinator.data.get(CONTROL_MODEL)
        if wrapper is None or not _watts_mode(wrapper):
            return None
        return _number(_point(wrapper, "WSet"))

    async def async_set_native_value(self, value: float) -> None:
        await async_set_power(self.coordinator, value)


class FranklinBatteryButton(FranklinEntity, ButtonEntity):
    """Hold zero watts or hand battery control back to the aGate."""

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        entry: ConfigEntry,
        prefix: str,
        home: Home,
        release: bool,
    ) -> None:
        key = "franklinwh_release" if release else "franklinwh_standby"
        super().__init__(coordinator, entry, prefix, home, key)
        self._release = release
        self._attr_icon = "mdi:battery-auto" if release else "mdi:battery-lock"

    async def async_press(self) -> None:
        await async_set_power(self.coordinator, None if self._release else 0)


class FranklinBatteryState(FranklinEntity, SensorEntity):
    """Battery activity derived from measured aggregate DC power."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["charging", "discharging", "idle"]
    _attr_icon = "mdi:battery"

    @property
    def native_value(self) -> str | None:
        power = battery_power(self.coordinator.data.get(DC_MODEL))
        if power is None:
            return None
        if power < -STATE_DEADBAND_W:
            return "charging"
        if power > STATE_DEADBAND_W:
            return "discharging"
        return "idle"


class FranklinHomeLoad(FranklinEntity, SensorEntity):
    """Home consumption from 16000 once it has proven finer than 15506, else from 15506."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "W"

    # 16000 measured in about 1 W steps on V10R01B04D00 and V12, where
    # 15506 has 100 W steps on V10. On an aGate X reported as
    # V10R11B40D00, 16000 moved in 1000 W steps and 15506 in 100 W steps
    # (issue #149). A single reading cannot tell the two apart, so the
    # sensor uses 15506 until 16000 has once reported a value that is not
    # a multiple of 100, and 16000 from then on.
    _high_resolution_proven = False

    @property
    def device_info(self) -> DeviceInfo:
        # Beside the extension sensors it is computed from.
        return raw_device_info(self.coordinator, self.config_entry, self._prefix, RAW_DEVICES[0])

    @property
    def native_value(self) -> float | None:
        blocks = self.coordinator.raw_blocks
        fine = _number(blocks.get("franklinwh_high_resolution", {}).get("home_load"))
        coarse = _number(blocks.get("franklinwh_extensions", {}).get("home_load"))
        if fine is not None and fine % 100:
            self._high_resolution_proven = True
        if self._high_resolution_proven or coarse is None:
            return fine if fine is not None else coarse
        return coarse


def franklinwh_numbers(
    coordinator: SunSpecDataUpdateCoordinator, entry: ConfigEntry, prefix: str
) -> list[NumberEntity]:
    home = home_for(coordinator, (CONTROL_MODEL,))
    if not _ready(coordinator, CONTROL_MODEL) or home is None:
        return []
    if power_limits(coordinator.data.get(AC_CAPACITY_MODEL)) is None:
        return []
    return [FranklinPowerNumber(coordinator, entry, prefix, home, "franklinwh_power_target")]


def franklinwh_buttons(
    coordinator: SunSpecDataUpdateCoordinator, entry: ConfigEntry, prefix: str
) -> list[ButtonEntity]:
    home = home_for(coordinator, (CONTROL_MODEL,))
    if not _ready(coordinator, CONTROL_MODEL) or home is None:
        return []
    return [
        FranklinBatteryButton(coordinator, entry, prefix, home, release)
        for release in (False, True)
    ]


def franklinwh_sensors(
    coordinator: SunSpecDataUpdateCoordinator, entry: ConfigEntry, prefix: str
) -> list[SensorEntity]:
    home = home_for(coordinator, (701, DC_MODEL))
    if coordinator.vendor is None or coordinator.vendor.slug != "franklinwh" or home is None:
        return []
    sensors: list[SensorEntity] = []
    battery_home = home_for(coordinator, (DC_MODEL,))
    if _ready(coordinator, DC_MODEL) and battery_home is not None:
        sensors.append(
            FranklinBatteryState(
                coordinator, entry, prefix, battery_home, "franklinwh_battery_state"
            )
        )
    if any(name.startswith("franklinwh_") for name in coordinator.raw_blocks):
        sensors.append(
            FranklinHomeLoad(coordinator, entry, prefix, home, "franklinwh_home_consumption")
        )
    return sensors
