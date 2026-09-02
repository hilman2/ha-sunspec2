"""Switch platform for SunSpec write controls.

EXPERIMENTAL, opt-in, same gating as the Number platform. Covers the
SunSpec points that are booleans in everything but name: the spec
spells them as two-symbol enums (ENABLED / DISABLED,
CONNECTED / DISCONNECTED) rather than as a boolean type.

v0.19.0 made this spec-driven, see :mod:`write_controls`.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SunSpec2ConfigEntry
from . import SunSpecDataUpdateCoordinator
from . import get_sunspec_unique_id
from .const import CONF_WRITE_BETA_ENABLED
from .discharge_plan import discharge_plan_switch
from .entity import SunSpecEntity
from .errors import SunSpecError
from .models import SunSpecModelWrapper
from .number import build_specs
from .write_controls import PLATFORM_SWITCH
from .write_controls import WriteControlSpec

_LOGGER: logging.Logger = logging.getLogger(__package__)

PARALLEL_UPDATES = 0

# Symbol strings that mean "on". Some firmware returns the enum symbol
# rather than its ordinal through the wrapper's decoding path.
_TRUTHY_SYMBOLS = frozenset({"ENABLED", "ON", "CONNECTED", "ACTIVE", "1"})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the experimental write Switch entities, gated by the beta flag."""
    coordinator = entry.runtime_data
    if not entry.options.get(CONF_WRITE_BETA_ENABLED, False):
        return

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
    # The scheduled discharge, where the vendor's battery modes exist.
    entities.extend(discharge_plan_switch(coordinator, entry, prefix))
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
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, spec.point_name, spec.model_id, 0
        )
        self._attr_translation_key = spec.translation_key
        self._attr_icon = spec.icon
        self._attr_entity_registry_enabled_default = spec.enabled_by_default

    @property
    def _point_name(self) -> str:
        """The SunSpec point this entity writes. Read by the tests."""
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
        if isinstance(value, str):
            return value.upper() in _TRUTHY_SYMBOLS
        return bool(value == self._spec.on_value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(self._spec.on_value)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(self._spec.off_value)

    async def _write(self, raw_value: int) -> None:
        # Routed through the coordinator, not through ``coordinator.api``
        # directly, so the write holds the per-gateway lock and cannot
        # interleave with a poll cycle on the same socket. See number.py's
        # module docstring for the full rationale.
        try:
            await self.coordinator.async_write_points_locked(
                self._spec.model_id, [(self._spec.point_name, raw_value)]
            )
        except SunSpecError as exc:
            raise HomeAssistantError(
                f"Failed to write {self._spec.point_name}={raw_value}: {exc}"
            ) from exc
        # Outside the lock: the refresh debouncer runs inline and
        # asyncio.Lock is not reentrant.
        await self.coordinator.async_request_refresh()
