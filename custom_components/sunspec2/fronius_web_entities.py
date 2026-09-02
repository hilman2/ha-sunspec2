"""The entities fed by the Fronius web interface.

Two temperatures, where the smart meter sits, the two grid charging
flags, whether Modbus control is allowed, and the Modbus reset. They
sit on the same devices as the Modbus entities: the battery ones on the
storage model's device, the rest on the inverter's.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor import SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SunSpecDataUpdateCoordinator
from . import get_sunspec_unique_id
from .const import OPERATING_STATE_MODEL_IDS
from .entity import device_info_for
from .fronius_web import CHARGE_FROM_AC
from .fronius_web import CHARGE_FROM_GRID
from .fronius_web import FroniusWebCoordinator
from .fronius_web import FroniusWebData
from .fronius_web import FroniusWebError
from .models import SunSpecModelWrapper
from .write_controls import STORAGE_CONTROL_MODEL

_LOGGER: logging.Logger = logging.getLogger(__package__)

METER_LOCATIONS: dict[int, str] = {0: "feed_in_point", 1: "consumption_path"}


@dataclass(frozen=True)
class _Home:
    """The device an entity sits on."""

    device_info: SunSpecModelWrapper
    model_info: dict[str, Any]
    model_id: int


class FroniusWebEntity(CoordinatorEntity[FroniusWebCoordinator]):
    """What the web entities share: the web coordinator and a Modbus device to sit on."""

    _attr_has_entity_name = True

    def __init__(
        self,
        web: FroniusWebCoordinator,
        config_entry: ConfigEntry,
        home: _Home,
        prefix: str,
        key: str,
    ) -> None:
        super().__init__(web)
        self._config_entry = config_entry
        self._home = home
        self._prefix = prefix
        self._attr_translation_key = key
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, f"web:{key}", home.model_id, 0
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for(
            self._config_entry,
            self._home.device_info,
            self._home.model_info,
            self._prefix,
            self._home.model_id,
        )

    @property
    def data(self) -> FroniusWebData | None:
        return self.coordinator.data


class WebTemperatureSensor(FroniusWebEntity, SensorEntity):
    """A temperature the inverter reports to its web UI."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        web: FroniusWebCoordinator,
        config_entry: ConfigEntry,
        home: _Home,
        prefix: str,
        key: str,
        read: Callable[[FroniusWebData], float | None],
        attributes: Callable[[FroniusWebData], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(web, config_entry, home, prefix, key)
        self._read = read
        self._attributes = attributes

    @property
    def native_value(self) -> float | None:
        return self._read(self.data) if self.data is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._attributes is None or self.data is None:
            return None
        return self._attributes(self.data)


class MeterLocationSensor(FroniusWebEntity, SensorEntity):
    """Where the primary smart meter sits: at the feed-in point or in the consumption path."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:meter-electric-outline"
    _attr_options = list(METER_LOCATIONS.values())

    @property
    def native_value(self) -> str | None:
        meter = self.data.primary_meter if self.data is not None else None
        if meter is None:
            return None
        location = meter.get("location")
        return METER_LOCATIONS.get(location) if isinstance(location, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"meters": list(self.data.meters) if self.data is not None else []}


@dataclass(frozen=True)
class WebSwitchSpec:
    """A flag of the web interface as a switch.

    Args:
        key (str): The translation key and unique id key.
        read (Callable[[FroniusWebData], bool|None]): Called as
            ``read(data)``. Returns the flag, or None when unknown.
        write (Callable[[FroniusWebCoordinator, bool], Awaitable[None]]):
            Called as ``write(web, on)``. Sets the flag.
        icon (str): The icon.
        category (EntityCategory): Where the device card files it.
    """

    key: str
    read: Callable[[FroniusWebData], bool | None]
    write: Callable[[FroniusWebCoordinator, bool], Awaitable[None]]
    icon: str
    category: EntityCategory = EntityCategory.CONFIG


async def _set_grid(web: FroniusWebCoordinator, on: bool) -> None:
    # Charging from the grid needs the AC flag too; the web UI sets
    # both when the grid one goes on. Off leaves the AC flag alone.
    await web.async_set_charge_sources(grid=on, ac=True if on else None)


async def _set_ac(web: FroniusWebCoordinator, on: bool) -> None:
    # Without AC charging there is no grid charging either.
    await web.async_set_charge_sources(grid=None if on else False, ac=on)


async def _set_modbus_control(web: FroniusWebCoordinator, on: bool) -> None:
    await web.async_allow_modbus_control(on)


WEB_SWITCHES: tuple[WebSwitchSpec, ...] = (
    WebSwitchSpec(
        "battery_charge_from_grid",
        lambda data: data.flag(CHARGE_FROM_GRID),
        _set_grid,
        "mdi:transmission-tower-import",
    ),
    WebSwitchSpec(
        "battery_charge_from_ac",
        lambda data: data.flag(CHARGE_FROM_AC),
        _set_ac,
        "mdi:battery-charging-wireless",
    ),
    WebSwitchSpec(
        "modbus_control_allowed",
        lambda data: data.modbus.get("control_allowed"),
        _set_modbus_control,
        "mdi:lan-connect",
    ),
)


class WebSwitch(FroniusWebEntity, SwitchEntity):
    def __init__(
        self,
        web: FroniusWebCoordinator,
        config_entry: ConfigEntry,
        home: _Home,
        prefix: str,
        spec: WebSwitchSpec,
    ) -> None:
        super().__init__(web, config_entry, home, prefix, spec.key)
        self._spec = spec
        self._attr_icon = spec.icon
        self._attr_entity_category = spec.category

    @property
    def is_on(self) -> bool | None:
        return self._spec.read(self.data) if self.data is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(False)

    async def _write(self, on: bool) -> None:
        try:
            await self._spec.write(self.coordinator, on)
        except FroniusWebError as exc:
            raise HomeAssistantError(f"The inverter's web interface refused: {exc}") from exc


class ModbusResetButton(FroniusWebEntity, ButtonEntity):
    """Hands the inverter's Modbus control state back to its defaults."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restart"

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_reset_modbus()
        except FroniusWebError as exc:
            raise HomeAssistantError(f"The inverter's web interface refused: {exc}") from exc


def _home(coordinator: SunSpecDataUpdateCoordinator, preferred: tuple[int, ...]) -> _Home | None:
    """The device to sit on: the first preferred model the device has, else its lowest model."""
    if coordinator.device_info is None or not coordinator.data:
        return None
    candidates = [model_id for model_id in preferred if model_id in coordinator.data]
    if not candidates:
        candidates = sorted(model_id for model_id in coordinator.data if model_id != 1)
    if not candidates:
        return None
    model_id = candidates[0]
    return _Home(coordinator.device_info, coordinator.data[model_id].getGroupMeta(), model_id)


def fronius_web_sensors(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> list[SensorEntity]:
    """The web sensors for this entry, or an empty list without a web login."""
    web = coordinator.web
    inverter = _home(coordinator, OPERATING_STATE_MODEL_IDS)
    if web is None or inverter is None:
        return []
    battery = _home(coordinator, (STORAGE_CONTROL_MODEL,)) or inverter
    return [
        WebTemperatureSensor(
            web,
            config_entry,
            inverter,
            prefix,
            "inverter_temperature",
            lambda data: data.inverter_temperature_c,
        ),
        WebTemperatureSensor(
            web,
            config_entry,
            battery,
            prefix,
            "battery_cell_temperature",
            lambda data: data.battery_cell_temperature_c,
            lambda data: dict(data.battery),
        ),
        MeterLocationSensor(web, config_entry, inverter, prefix, "smart_meter_location"),
    ]


def fronius_web_switches(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> list[SwitchEntity]:
    """The web switches for this entry, or an empty list without a web login."""
    web = coordinator.web
    inverter = _home(coordinator, OPERATING_STATE_MODEL_IDS)
    if web is None or inverter is None:
        return []
    battery = _home(coordinator, (STORAGE_CONTROL_MODEL,)) or inverter
    return [
        WebSwitch(
            web,
            config_entry,
            battery if spec.key.startswith("battery_") else inverter,
            prefix,
            spec,
        )
        for spec in WEB_SWITCHES
    ]


def fronius_web_buttons(
    coordinator: SunSpecDataUpdateCoordinator, config_entry: ConfigEntry, prefix: str
) -> list[ButtonEntity]:
    """The web buttons for this entry, or an empty list without a web login."""
    web = coordinator.web
    inverter = _home(coordinator, OPERATING_STATE_MODEL_IDS)
    if web is None or inverter is None:
        return []
    return [ModbusResetButton(web, config_entry, inverter, prefix, "reset_modbus_control")]
