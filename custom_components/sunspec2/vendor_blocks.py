"""Sensors over the registers a vendor keeps outside the SunSpec models.

A vendor profile declares the blocks (see ``raw_blocks.py``), the
devices they describe and the sensors over their fields; this module
turns the sensors into entities. A sensor sits either on the
inverter's own model device or on a device the profile adds, a
battery for instance, whose identity comes from the block itself.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo

from . import SunSpecDataUpdateCoordinator
from . import get_sunspec_unique_id
from .const import DOMAIN
from .const import OPERATING_STATE_MODEL_IDS
from .entity import Home
from .entity import SunSpecEntity
from .entity import home_for
from .vendors.profile import RawDevice
from .vendors.profile import RawSensor

_LOGGER: logging.Logger = logging.getLogger(__package__)


class RawBlockSensor(SunSpecEntity, SensorEntity):
    """One field of a raw block, as the profile describes it."""

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        home: Home,
        prefix: str,
        spec: RawSensor,
        device: RawDevice | None,
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            home.device_info,
            home.model_info,
            prefix=prefix,
            model_id=home.model_id,
        )
        self._spec = spec
        self._device = device
        self._attr_translation_key = spec.key
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, f"raw:{spec.block}:{spec.field}", 0, 0
        )
        self._attr_native_unit_of_measurement = spec.unit
        if spec.device_class is not None:
            self._attr_device_class = SensorDeviceClass(spec.device_class)
        if spec.state_class is not None:
            self._attr_state_class = SensorStateClass(spec.state_class)
        if spec.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if spec.options is not None:
            self._attr_options = list(spec.options.values())
        if spec.icon is not None:
            self._attr_icon = spec.icon

    @property
    def available(self) -> bool:
        return super().available and self._spec.block in self.coordinator.raw_blocks

    @property
    def native_value(self) -> Any:
        value = self.coordinator.raw_blocks.get(self._spec.block, {}).get(self._spec.field)
        if value is None:
            return None
        if self._spec.transform is not None:
            value = self._spec.transform(value)
        if self._spec.options is not None:
            return self._spec.options.get(value) if isinstance(value, int) else None
        return value

    @property
    def device_info(self) -> DeviceInfo:
        if self._device is None:
            return super().device_info
        identity = self.coordinator.raw_blocks.get(self._device.info_block, {})
        base = self._prefix or _text(self._device_data.getValue("Md"))
        info = DeviceInfo(
            # Three-element identifiers, like every device of this
            # integration; see SunSpecEntity.device_info.
            identifiers={
                (DOMAIN, self.config_entry.entry_id, f"raw:{self._device.key}")  # type: ignore[arg-type]
            },
            name=f"{base} {self._device.name}" if base else self._device.name,
            manufacturer=_text(identity.get(self._device.manufacturer)),
            model=_text(identity.get(self._device.model)),
            serial_number=_text(identity.get(self._device.serial)),
        )
        if self._device.version is not None:
            info["sw_version"] = _text(identity.get(self._device.version))
        return info


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def raw_block_sensors(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> list[SensorEntity]:
    """The profile's sensors whose blocks the device answered.

    Called on every cycle by the sensor platform, which drops the ones
    it has already added, so a block that starts answering later, once
    the vendor's support switched it on, gets its sensors then.
    """
    vendor = coordinator.vendor
    if vendor is None or not vendor.raw_sensors:
        return []
    home = home_for(coordinator, OPERATING_STATE_MODEL_IDS)
    if home is None:
        return []
    devices = {device.key: device for device in vendor.raw_devices}
    sensors: list[SensorEntity] = []
    for spec in vendor.raw_sensors:
        if spec.block not in coordinator.raw_blocks:
            continue
        device = devices.get(spec.device) if spec.device != "inverter" else None
        if spec.device != "inverter" and device is None:
            _LOGGER.warning("Raw sensor %s names an unknown device %s", spec.key, spec.device)
            continue
        if device is not None and (
            device.info_block not in coordinator.raw_blocks
            or (device.requires is not None and device.requires not in coordinator.raw_blocks)
        ):
            continue
        sensors.append(RawBlockSensor(coordinator, config_entry, home, prefix, spec, device))
    return sensors
