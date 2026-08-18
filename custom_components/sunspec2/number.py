"""Number platform for SunSpec experimental write controls.

v0.12.0 EXPERIMENTAL. Disabled by default. Only registers entities
when:

1. The user has explicitly ticked "Enable experimental write
   controls (BETA)" in the options flow (CONF_WRITE_BETA_ENABLED).
2. The inverter actually exposes SunSpec model 123 (Immediate
   Controls), checked against ``coordinator.detected_models``.

The Number platform exposes the two writable continuous-value
points from model 123: ``WMaxLimPct`` (export limit as percent of
WMax; 100 is full nameplate, higher values are allowed because
some firmware uses them to mean "no limit") and ``OutPFSet``
(power factor setpoint, -1.0..1.0).
The matching enable/disable booleans live on the Switch platform
because mixing modes in one entity makes the UI confusing.

Behavioural notes:

- Writes go through ``coordinator.async_write_points_locked``,
  which takes the per-``(host, port)`` gateway lock for the
  duration of the write and closes the Modbus session again before
  releasing it. Calling ``coordinator.api.async_write_point``
  directly, which is what this platform did before v0.14.0,
  bypasses that lock entirely and lets a write interleave with the
  poll cycle on one socket.
- After a successful write we trigger a coordinator refresh so the
  read-side state catches up immediately instead of waiting for
  the next scheduled cycle.
- Reading the current value comes from ``coordinator.data[123]``
  which is populated by the normal read cycle - so the entity
  state always reflects what the inverter actually reports, not
  what we last wrote (the inverter may clamp or refuse a write
  for vendor-specific reasons).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.components.number import NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SunSpec2ConfigEntry
from . import get_sunspec_unique_id
from .const import CONF_WRITE_BETA_ENABLED
from .const import EXPORT_LIMIT_DEFAULT_STEP_PCT
from .const import EXPORT_LIMIT_HARD_MAX_PCT
from .const import EXPORT_LIMIT_MIN_STEP_PCT
from .const import WRITE_CONTROLS_MODEL_ID
from .entity import SunSpecEntity
from .errors import SunSpecError

_LOGGER: logging.Logger = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


def _step_from_scale_factor(wrapper, point_name: str) -> float:
    """Derive the UI step from the point's SunSpec scale factor.

    model_123.json declares ``WMaxLimPct`` as uint16 with sf
    ``WMaxLimPct_SF``, so the register resolution is ``10 ** SF``
    percent. A device reporting SF -1 stores 995 for 99.5 %, and with a
    hardcoded step of 1 the frontend number box rejects 99.5: the
    inverter's own value becomes unenterable.

    Reads the SF through ``getValue(<sf point name>)`` and NOT through
    ``getPoint(point_name).sf_value``. pysunspec2 only backfills
    ``Point.sf_value`` as a side effect of the first computed get or
    set, so at entity-construction time it is still None. The scale
    factor point's own value is populated directly by ``model.read()``.

    Falls back to the default step when the device answers its ``*_SF``
    register with 0x8000 ("not implemented"), which pysunspec2 turns
    back into None.
    """
    try:
        sf_name = wrapper.getMeta(point_name).get("sf")
        sf = wrapper.getValue(sf_name) if isinstance(sf_name, str) else sf_name
    except (KeyError, AttributeError, TypeError):
        return EXPORT_LIMIT_DEFAULT_STEP_PCT
    if not isinstance(sf, int) or isinstance(sf, bool):
        return EXPORT_LIMIT_DEFAULT_STEP_PCT
    return min(EXPORT_LIMIT_DEFAULT_STEP_PCT, max(10.0**sf, EXPORT_LIMIT_MIN_STEP_PCT))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the experimental write Number entities, gated by the beta flag."""
    coordinator = entry.runtime_data
    if not entry.options.get(CONF_WRITE_BETA_ENABLED, False):
        # Beta flag is off - do not expose any write entities. The
        # user can still enable them later via the options flow,
        # which triggers a config-entry reload and a fresh setup.
        return
    if WRITE_CONTROLS_MODEL_ID not in coordinator.detected_models:
        # The inverter does not expose model 123, so writes are
        # impossible regardless of the beta flag.
        return

    device_info = coordinator.device_info
    if device_info is None:
        return

    model_wrapper = coordinator.data.get(WRITE_CONTROLS_MODEL_ID)
    if model_wrapper is None:
        # Unreachable in the normal path since v0.14.0: the coordinator
        # adds model 123 to the polled set whenever the beta flag is on.
        # If it ever trips again something new is broken, and silently
        # returning is what made this class of bug invisible for three
        # releases.
        getattr(coordinator, "_log", _LOGGER).warning(
            "Write beta is on and model %s was detected, but it is not in the "
            "polled data, so no write entities will be created. write_model_filter=%s",
            WRITE_CONTROLS_MODEL_ID,
            getattr(coordinator, "write_model_filter", None),
        )
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
            SunSpecExportLimitNumber(
                **common,
                native_step=_step_from_scale_factor(model_wrapper, "WMaxLimPct"),
            ),
            SunSpecPowerFactorNumber(**common),
        ]
    )


class _SunSpecWritablePointNumber(SunSpecEntity, NumberEntity):
    """Base class for the model 123 writable Number points.

    Subclasses set ``_point_name``, ``_attr_translation_key``, range
    bounds and unit. The base class handles the
    coordinator->wrapper->getValue read path and the
    api.async_write_point write path.
    """

    _point_name: str = ""
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator,
        config_entry,
        device_info,
        model_info,
        prefix: str = "",
        native_step: float | None = None,
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            device_info,
            model_info,
            prefix=prefix,
            model_id=WRITE_CONTROLS_MODEL_ID,
        )
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, self._point_name, WRITE_CONTROLS_MODEL_ID, 0
        )
        if native_step is not None:
            self._attr_native_step = native_step

    @property
    def native_value(self) -> float | None:
        wrapper = self.coordinator.data.get(WRITE_CONTROLS_MODEL_ID)
        if wrapper is None:
            return None
        try:
            value = wrapper.getValue(self._point_name)
        except (KeyError, AttributeError):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_write_points_locked(
                WRITE_CONTROLS_MODEL_ID, [(self._point_name, value)]
            )
        except SunSpecError as exc:
            raise HomeAssistantError(f"Failed to write {self._point_name}={value}: {exc}") from exc
        # Refresh the read-side state immediately so the UI shows
        # the inverter's actual response (which may differ from the
        # value we just sent if the inverter clamps it).
        #
        # Deliberately OUTSIDE the gateway lock the write just held:
        # asyncio.Lock is not reentrant and the refresh debouncer runs
        # the refresh inline, so requesting it under the lock deadlocks.
        await self.coordinator.async_request_refresh()


class SunSpecExportLimitNumber(_SunSpecWritablePointNumber):
    """Inverter export limit as a percentage of WMax (model 123 WMaxLimPct).

    Setting this to 0 caps the inverter's AC output at zero (the
    zero-export use case). 100 lets the inverter run at full
    nameplate. Note that the limit only takes effect while the
    matching ``WMaxLim_Ena`` switch is ON - the Number alone does
    not enable the limit.

    Values above 100 are permitted up to
    ``EXPORT_LIMIT_HARD_MAX_PCT`` because some firmware uses them as
    a "no limit" sentinel: the KACO in #17 shipped with 110 and its
    owner could not put it back. The inverter is free to clamp or
    refuse anything it will not honour, and ``native_value`` always
    re-reads what the device actually reports, so a rejected write
    shows up as the value snapping back rather than as a lie in the
    UI.
    """

    _point_name = "WMaxLimPct"
    _attr_translation_key = "export_limit_pct"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 0
    _attr_native_max_value = EXPORT_LIMIT_HARD_MAX_PCT
    # Per-instance in __init__, derived from the device's own
    # WMaxLimPct_SF. This class-level value is only the fallback for a
    # device that does not implement that register.
    _attr_native_step = EXPORT_LIMIT_DEFAULT_STEP_PCT
    _attr_icon = "mdi:transmission-tower-export"


class SunSpecPowerFactorNumber(_SunSpecWritablePointNumber):
    """Inverter power factor setpoint (model 123 OutPFSet).

    Range -1.0 to 1.0 in cos() units. Negative values mean leading
    (capacitive), positive mean lagging (inductive). Only effective
    while the matching ``OutPFSet_Ena`` switch is ON.
    """

    _point_name = "OutPFSet"
    _attr_translation_key = "power_factor_set"
    _attr_native_min_value = -1.0
    _attr_native_max_value = 1.0
    _attr_native_step = 0.01
    _attr_icon = "mdi:angle-acute"

    @property
    def native_value(self) -> Any:
        v = super().native_value
        if v is None:
            return None
        return round(v, 3)
