"""Switch platform for SunSpec experimental write controls.

v0.12.0 EXPERIMENTAL. Same opt-in beta gating as the Number
platform - see ``number.py`` for the rationale.

Three switches from model 123 (Immediate Controls):

- ``WMaxLim_Ena``: enable / disable the export-limit Number. The
  ``WMaxLimPct`` value only takes effect while this switch is ON.
- ``OutPFSet_Ena``: enable / disable the power-factor setpoint.
- ``Conn``: inverter grid connection. Setting this to OFF
  disconnects the inverter from the grid entirely. **The most
  dangerous one** - users who flip this off and forget about it
  will be confused when the inverter "stops working".
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SunSpec2ConfigEntry
from . import get_sunspec_unique_id
from .const import CONF_WRITE_BETA_ENABLED
from .const import WRITE_CONTROLS_MODEL_ID
from .entity import SunSpecEntity
from .errors import SunSpecError

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the experimental write Switch entities, gated by the beta flag."""
    coordinator = entry.runtime_data
    if not entry.options.get(CONF_WRITE_BETA_ENABLED, False):
        return
    if WRITE_CONTROLS_MODEL_ID not in coordinator.detected_models:
        return

    device_info = coordinator.device_info
    if device_info is None:
        return
    model_wrapper = coordinator.data.get(WRITE_CONTROLS_MODEL_ID)
    if model_wrapper is None:
        return
    group_meta = model_wrapper.getGroupMeta()
    common = {
        "coordinator": coordinator,
        "config_entry": entry,
        "device_info": device_info,
        "model_info": group_meta,
        "prefix": entry.options.get("prefix", ""),
    }
    async_add_devices(
        [
            SunSpecExportLimitEnabledSwitch(**common),
            SunSpecPowerFactorEnabledSwitch(**common),
            SunSpecInverterConnectionSwitch(**common),
        ]
    )


class _SunSpecWritablePointSwitch(SunSpecEntity, SwitchEntity):
    """Base class for the model 123 boolean writable points.

    Subclasses set ``_point_name`` and ``_attr_translation_key``.
    pysunspec2 returns the underlying enum value as int (0 or 1)
    via ``getValue()`` for these CONNECTED-style enum16 points;
    we coerce to bool for HA's switch interface and write back
    the same int.
    """

    _point_name: str = ""
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator,
        config_entry,
        device_info,
        model_info,
        prefix: str = "",
    ) -> None:
        super().__init__(coordinator, config_entry, device_info, model_info, prefix=prefix)
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, self._point_name, WRITE_CONTROLS_MODEL_ID, 0
        )

    @property
    def is_on(self) -> bool | None:
        wrapper = self.coordinator.data.get(WRITE_CONTROLS_MODEL_ID)
        if wrapper is None:
            return None
        try:
            value = wrapper.getValue(self._point_name)
        except (KeyError, AttributeError):
            return None
        if value is None:
            return None
        # SunSpec model 123 enables: 1 = on, 0 = off. Some firmware
        # returns the value as a symbol string ("ENABLED" / "DISABLED")
        # via the wrapper's enum-decoding path; accept either form.
        if isinstance(value, str):
            return value.upper() in {"ENABLED", "ON", "CONNECTED", "1"}
        return bool(value)

    async def async_turn_on(self, **kwargs) -> None:
        await self._write(1)

    async def async_turn_off(self, **kwargs) -> None:
        await self._write(0)

    async def _write(self, raw_value: int) -> None:
        try:
            await self.coordinator.api.async_write_point(
                WRITE_CONTROLS_MODEL_ID, self._point_name, raw_value
            )
        except SunSpecError as exc:
            raise HomeAssistantError(
                f"Failed to write {self._point_name}={raw_value}: {exc}"
            ) from exc
        await self.coordinator.async_request_refresh()


class SunSpecExportLimitEnabledSwitch(_SunSpecWritablePointSwitch):
    """Enable / disable the export-limit setpoint (model 123 WMaxLim_Ena)."""

    _point_name = "WMaxLim_Ena"
    _attr_translation_key = "export_limit_enabled"
    _attr_icon = "mdi:transmission-tower-export"


class SunSpecPowerFactorEnabledSwitch(_SunSpecWritablePointSwitch):
    """Enable / disable the power-factor setpoint (model 123 OutPFSet_Ena)."""

    _point_name = "OutPFSet_Ena"
    _attr_translation_key = "power_factor_enabled"
    _attr_icon = "mdi:angle-acute"


class SunSpecInverterConnectionSwitch(_SunSpecWritablePointSwitch):
    """Connect / disconnect the inverter from the grid (model 123 Conn).

    Most dangerous switch in the platform: turning this OFF
    disconnects the inverter from the grid entirely. Use with care.
    """

    _point_name = "Conn"
    _attr_translation_key = "inverter_grid_connection"
    _attr_icon = "mdi:transmission-tower"
