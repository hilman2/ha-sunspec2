"""Entities over the registers a vendor keeps outside the SunSpec models.

A vendor profile declares the blocks (see ``raw_blocks.py``), the
devices they describe, and the sensors, numbers, selects and timed
rewrites over their fields; this module turns those into entities. An
entity sits either on the inverter's own model device or on a device
the profile adds, a battery for instance, whose identity comes from
the block itself.

Writes go through ``coordinator.async_write_raw_locked``, one register
run at a time, under the gateway lock, and are counted: SolarEdge
warns that its persistent registers wear the flash when written
periodically, and the count is the one number that tells a user how
their automation is doing on that.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import timedelta
from typing import Any

from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.number import NumberEntity
from homeassistant.components.number import NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import RestoreSensor
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor import SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from . import SunSpecDataUpdateCoordinator
from . import get_sunspec_unique_id
from .const import CONF_WRITE_BETA_ENABLED
from .const import DOMAIN
from .const import OPERATING_STATE_MODEL_IDS
from .entity import Home
from .entity import SunSpecEntity
from .entity import home_for
from .errors import SunSpecError
from .vendors.profile import RawDevice
from .vendors.profile import RawKeepAlive
from .vendors.profile import RawNumber
from .vendors.profile import RawSelect
from .vendors.profile import RawSensor
from .vendors.profile import VendorProfile

_LOGGER: logging.Logger = logging.getLogger(__package__)


class RawBlockEntity(SunSpecEntity):
    """What every entity over a raw block shares: the block, its field and the device it sits on."""

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        home: Home,
        prefix: str,
        block: str,
        field: str | None,
        key: str,
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
        self._block = block
        self._field = field
        self._device = device
        vendor = coordinator.vendor
        raw_block = vendor.raw_block(block) if vendor is not None else None
        # A block of write-only setpoints is never polled; its entities
        # are available whenever the profile is, and show what Home
        # Assistant last wrote.
        self._write_only = raw_block is not None and not raw_block.readable
        self._attr_translation_key = key
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, f"raw:{block}:{field if field is not None else key}", 0, 0
        )

    @property
    def available(self) -> bool:
        return super().available and (
            self._write_only or self._block in self.coordinator.raw_blocks
        )

    def _raw(self) -> Any:
        """The field as decoded on the last cycle, else what Home Assistant last wrote, else None."""
        if self._field is None:
            return None
        value = self.coordinator.raw_blocks.get(self._block, {}).get(self._field)
        if value is None:
            return self.coordinator.raw_setpoints.get((self._block, self._field))
        return value

    async def _write(self, value: float | int) -> None:
        """Write the field and read the block back."""
        vendor = self.coordinator.vendor
        block = vendor.raw_block(self._block) if vendor is not None else None
        raw_field = (
            next((f for f in block.fields if f.name == self._field), None) if block else None
        )
        if block is None or raw_field is None:
            raise HomeAssistantError(
                f"{self._block}.{self._field} is not a register of this device"
            )
        try:
            await self.coordinator.async_write_raw_locked(block, raw_field, value)
        except SunSpecError as exc:
            raise HomeAssistantError(
                f"Failed to write {self._block}.{self._field}={value}: {exc}"
            ) from exc
        # Outside the lock: the refresh debouncer runs inline and
        # asyncio.Lock is not reentrant.
        await self.coordinator.async_request_refresh()

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


class RawBlockSensor(RawBlockEntity, SensorEntity):
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
            coordinator, config_entry, home, prefix, spec.block, spec.field, spec.key, device
        )
        self._spec = spec
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
    def native_value(self) -> Any:
        value = self._raw()
        if value is None:
            return None
        if self._spec.transform is not None:
            value = self._spec.transform(value)
        if self._spec.options is not None:
            return self._spec.options.get(value) if isinstance(value, int) else None
        return value


class RawBlockNumber(RawBlockEntity, NumberEntity):
    """A writable field of a raw block."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        home: Home,
        prefix: str,
        spec: RawNumber,
        device: RawDevice | None,
    ) -> None:
        super().__init__(
            coordinator, config_entry, home, prefix, spec.block, spec.field, spec.key, device
        )
        self._attr_native_min_value = spec.min
        self._attr_native_max_value = spec.max
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit
        if spec.device_class is not None:
            self._attr_device_class = NumberDeviceClass(spec.device_class)
        if spec.icon is not None:
            self._attr_icon = spec.icon

    @property
    def native_value(self) -> float | None:
        value = self._raw()
        return float(value) if isinstance(value, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        await self._write(value)


class RawBlockSelect(RawBlockEntity, SelectEntity):
    """A writable choice in a field of a raw block."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        home: Home,
        prefix: str,
        spec: RawSelect,
        device: RawDevice | None,
    ) -> None:
        super().__init__(
            coordinator, config_entry, home, prefix, spec.block, spec.field, spec.key, device
        )
        self._spec = spec
        self._attr_options = list(spec.options.values())
        if spec.icon is not None:
            self._attr_icon = spec.icon

    @property
    def current_option(self) -> str | None:
        value = self._raw()
        if value is None:
            return None
        if self._spec.read is not None:
            value = self._spec.read(value)
        return self._spec.options.get(value) if isinstance(value, int) else None

    async def async_select_option(self, option: str) -> None:
        chosen = next((raw for raw, name in self._spec.options.items() if name == option), None)
        if chosen is None:
            raise HomeAssistantError(f"{option} is not a choice this device offers")
        if self._spec.write is not None:
            chosen = self._spec.write(self._raw(), chosen)
        await self._write(chosen)


class RawKeepAliveSwitch(RawBlockEntity, SwitchEntity, RestoreEntity):
    """Writes the profile's fields again on a timer while on.

    The values written are what Home Assistant last wrote to each
    field, or what the device shows when it never did. On across
    restarts, because a device that forgets its command at night does
    so whether or not Home Assistant was restarted in between.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        home: Home,
        prefix: str,
        spec: RawKeepAlive,
        device: RawDevice | None,
    ) -> None:
        super().__init__(
            coordinator, config_entry, home, prefix, spec.block, None, spec.key, device
        )
        self._spec = spec
        self._unsub: CALLBACK_TYPE | None = None
        if spec.icon is not None:
            self._attr_icon = spec.icon

    @property
    def is_on(self) -> bool:
        return self._unsub is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self._start()

    async def async_will_remove_from_hass(self) -> None:
        self._stop()
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._start()
        self.async_write_ha_state()
        await self._rewrite(None)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._stop()
        self.async_write_ha_state()

    def _start(self) -> None:
        if self._unsub is None:
            self._unsub = async_track_time_interval(
                self.hass, self._rewrite, timedelta(seconds=self._spec.interval_seconds)
            )

    def _stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _rewrite(self, now: datetime | None) -> None:
        vendor = self.coordinator.vendor
        block = vendor.raw_block(self._spec.block) if vendor is not None else None
        if block is None:
            return
        data = self.coordinator.raw_blocks.get(self._spec.block)
        if data is None:
            if block.readable:
                return
            data = {}
        # The device's view with Home Assistant's own writes on top:
        # what the condition and the rewrite go by. A write-only block
        # has nothing but the writes.
        view: dict[str, Any] = dict(data)
        for name in self._spec.fields:
            written = self.coordinator.raw_setpoints.get((self._spec.block, name))
            if written is not None:
                view[name] = written
        if self._spec.only_while is not None and not self._spec.only_while(view):
            return
        fields = {f.name: f for f in block.fields}
        first = True
        for name in self._spec.fields:
            value = view.get(name)
            if value is None or name not in fields:
                continue
            if not first and self._spec.settle_seconds > 0:
                await asyncio.sleep(self._spec.settle_seconds)
            first = False
            try:
                await self.coordinator.async_write_raw_locked(block, fields[name], value)
            except SunSpecError as exc:
                _LOGGER.warning("Rewrite of %s.%s failed: %s", self._spec.block, name, exc)
                return
        if block.readable:
            await self.coordinator.async_request_refresh()


class RawWriteCountSensor(SunSpecEntity, RestoreSensor):
    """How many times this entry wrote a persistent register of the device.

    Kept across restarts: flash wear is a property of the inverter's
    life, not of one Home Assistant run.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "register_writes"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        home: Home,
        prefix: str,
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            home.device_info,
            home.model_info,
            prefix=prefix,
            model_id=home.model_id,
        )
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, "raw:register_writes", 0, 0
        )

    @property
    def native_value(self) -> int:
        return self.coordinator.raw_write_count

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_raw_write_listener(self.async_write_ha_state)
        )
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, (int, float)):
            self.coordinator.raw_write_count = max(
                self.coordinator.raw_write_count, int(last.native_value)
            )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _context(
    coordinator: SunSpecDataUpdateCoordinator,
) -> tuple[VendorProfile, Home, dict[str, RawDevice]] | None:
    vendor = coordinator.vendor
    if vendor is None or not vendor.raw_blocks:
        return None
    home = home_for(coordinator, OPERATING_STATE_MODEL_IDS)
    if home is None:
        return None
    return vendor, home, {device.key: device for device in vendor.raw_devices}


def _block_answers(
    coordinator: SunSpecDataUpdateCoordinator, vendor: VendorProfile, key: str
) -> bool:
    """Whether entities over block ``key`` can be built: it answered, or it is write-only."""
    if key in coordinator.raw_blocks:
        return True
    block = vendor.raw_block(key)
    return block is not None and not block.readable


def _device_for(
    coordinator: SunSpecDataUpdateCoordinator,
    devices: dict[str, RawDevice],
    device_key: str,
    key: str,
) -> tuple[bool, RawDevice | None]:
    """Whether an entity for ``device_key`` can be built now, and the device if it is one."""
    if device_key == "inverter":
        return True, None
    device = devices.get(device_key)
    if device is None:
        _LOGGER.warning("Raw entity %s names an unknown device %s", key, device_key)
        return False, None
    if device.info_block not in coordinator.raw_blocks or (
        device.requires is not None and device.requires not in coordinator.raw_blocks
    ):
        return False, None
    return True, device


def raw_block_sensors(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> list[SensorEntity]:
    """The profile's sensors whose blocks the device answered, and the write counter.

    Called on every cycle by the sensor platform, which drops the ones
    it has already added, so a block that starts answering later, once
    the vendor's support switched it on, gets its sensors then.
    """
    context = _context(coordinator)
    if context is None:
        return []
    vendor, home, devices = context
    sensors: list[SensorEntity] = []
    for spec in vendor.raw_sensors:
        if spec.block not in coordinator.raw_blocks:
            continue
        buildable, device = _device_for(coordinator, devices, spec.device, spec.key)
        if buildable:
            sensors.append(RawBlockSensor(coordinator, config_entry, home, prefix, spec, device))
    if vendor.raw_numbers or vendor.raw_selects or vendor.raw_keepalives:
        sensors.append(RawWriteCountSensor(coordinator, config_entry, home, prefix))
    return sensors


def _writable(config_entry: ConfigEntry, beta: bool) -> bool:
    return not beta or bool(config_entry.options.get(CONF_WRITE_BETA_ENABLED, False))


def raw_block_numbers(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> list[NumberEntity]:
    """The profile's numbers whose blocks answered, minus the beta ones without the beta."""
    context = _context(coordinator)
    if context is None:
        return []
    vendor, home, devices = context
    numbers: list[NumberEntity] = []
    for spec in vendor.raw_numbers:
        if not _block_answers(coordinator, vendor, spec.block) or not _writable(
            config_entry, spec.beta
        ):
            continue
        buildable, device = _device_for(coordinator, devices, spec.device, spec.key)
        if buildable:
            numbers.append(RawBlockNumber(coordinator, config_entry, home, prefix, spec, device))
    return numbers


def raw_block_selects(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> list[SelectEntity]:
    """The profile's selects whose blocks answered, minus the beta ones without the beta."""
    context = _context(coordinator)
    if context is None:
        return []
    vendor, home, devices = context
    selects: list[SelectEntity] = []
    for spec in vendor.raw_selects:
        if not _block_answers(coordinator, vendor, spec.block) or not _writable(
            config_entry, spec.beta
        ):
            continue
        buildable, device = _device_for(coordinator, devices, spec.device, spec.key)
        if buildable:
            selects.append(RawBlockSelect(coordinator, config_entry, home, prefix, spec, device))
    return selects


def raw_keepalive_switches(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> list[SwitchEntity]:
    """The profile's timed rewrites as switches, for the blocks that answered."""
    context = _context(coordinator)
    if context is None:
        return []
    vendor, home, devices = context
    switches: list[SwitchEntity] = []
    for spec in vendor.raw_keepalives:
        if not _block_answers(coordinator, vendor, spec.block) or not _writable(
            config_entry, spec.beta
        ):
            continue
        buildable, device = _device_for(coordinator, devices, spec.device, spec.key)
        if buildable:
            switches.append(
                RawKeepAliveSwitch(coordinator, config_entry, home, prefix, spec, device)
            )
    return switches


__all__ = [
    "RawBlockNumber",
    "RawBlockSelect",
    "RawBlockSensor",
    "RawKeepAliveSwitch",
    "RawWriteCountSensor",
    "raw_block_numbers",
    "raw_block_selects",
    "raw_block_sensors",
    "raw_keepalive_switches",
]
