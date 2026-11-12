"""Switch platform for SunSpec write controls.

The switches are the SunSpec points that are booleans in everything but
name: the specification spells them as two-symbol enums (ENABLED /
DISABLED, CONNECTED / DISCONNECTED). Which points get a switch is
decided by the specs in :mod:`write_controls`.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .battery_plan import battery_plan_switch
from .coordinator import SunSpec2ConfigEntry
from .coordinator import SunSpecDataUpdateCoordinator
from .entity import SunSpecEntity
from .entity import get_sunspec_unique_id
from .errors import SunSpecError
from .errors import user_error
from .fronius_web_entities import fronius_web_switches
from .models import SunSpecModelWrapper
from .number import build_specs
from .vendor_blocks import raw_keepalive_switches
from .write_controls import PLATFORM_SWITCH
from .write_controls import WriteControlSpec

_LOGGER: logging.Logger = logging.getLogger(__package__)

PARALLEL_UPDATES = 0

# What a firmware that stores the flag as text reads back: ord("0"), ord("1").
_ASCII_DIGITS = {48: 0, 49: 1}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the write Switch entities the device and the options allow."""
    coordinator = entry.runtime_data
    device_info = coordinator.device_info
    if device_info is None:
        return

    prefix = entry.options.get("prefix", "")
    entities: list[SwitchEntity] = [
        SunSpecWriteSwitch(
            coordinator=coordinator,
            config_entry=entry,
            device_info=device_info,
            model_info=wrapper.getGroupMeta(),
            prefix=prefix,
            spec=spec,
        )
        for spec, wrapper in build_specs(coordinator, PLATFORM_SWITCH)
    ]
    # The battery plan, where the vendor's battery modes exist.
    entities.extend(battery_plan_switch(coordinator, entry, prefix))
    # The flags of the Fronius web interface, with a stored login.
    entities.extend(fronius_web_switches(coordinator, entry, prefix))
    # The vendor's timed rewrites, where a profile has them.
    entities.extend(raw_keepalive_switches(coordinator, entry, prefix))
    async_add_devices(entities)


class SunSpecWriteSwitch(SunSpecEntity, SwitchEntity):
    """A writable two-state SunSpec point."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        device_info: SunSpecModelWrapper,
        model_info: dict[str, Any],
        prefix: str,
        spec: WriteControlSpec,
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            device_info,
            model_info,
            prefix=prefix,
            model_id=spec.model_id,
        )
        self._spec = spec
        vendor = coordinator.vendor
        self._ascii_flag = vendor is not None and spec.unique_key in vendor.ascii_flag_points
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, spec.point_name, spec.model_id, 0
        )
        self._attr_translation_key = spec.translation_key
        self._attr_entity_registry_enabled_default = spec.enabled_by_default

    @property
    def _point_name(self) -> str:
        """Return the SunSpec point this entity writes. Read by the tests."""
        return self._spec.point_name

    @property
    def is_on(self) -> bool | None:
        wrapper = self.coordinator.data.get(self._spec.model_id)
        if wrapper is None:
            return None
        try:
            value = wrapper.getValue(self._spec.point_name)
        except (KeyError, AttributeError):
            return None
        if value is None:
            return None
        if self._ascii_flag:
            # The flag is written as 1 or 0 and read back as the
            # character code of the digit.
            value = _ASCII_DIGITS.get(value, value)
        return bool(value == self._spec.on_value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(self._spec.on_value)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(self._spec.off_value)

    async def _write(self, raw_value: int) -> None:
        # The write goes through the coordinator and so holds the
        # per-gateway lock. See the module docstring of number.py.
        try:
            await self.coordinator.async_write_points_locked(
                self._spec.model_id, [(self._spec.point_name, raw_value)]
            )
        except SunSpecError as exc:
            raise user_error(
                "write_failed", target=f"{self._spec.point_name}={raw_value}", error=exc
            ) from exc
        # The refresh is requested outside the lock: the refresh
        # debouncer runs inline and asyncio.Lock is not reentrant.
        await self.coordinator.async_request_refresh()
