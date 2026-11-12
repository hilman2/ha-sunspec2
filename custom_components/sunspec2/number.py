"""Number platform for SunSpec write controls.

One entity class, driven by a :class:`~.write_controls.WriteControlSpec`,
serves every model :mod:`write_controls` has specs for. Setup only reads
the state of the controls.

- A write goes through ``coordinator.async_write_points_locked``, which
  holds the per-``(host, port)`` gateway lock for the write. A call to
  ``coordinator.api.async_write_points`` bypasses that lock and lets a
  write interleave with the poll cycle on the same connection.
- A successful write is followed by a coordinator refresh, so the state
  does not wait for the next scheduled cycle.
- ``native_value`` reads ``coordinator.data``, so the entity shows what
  the inverter reports and not what was last written. Inverters clamp
  and refuse writes for vendor-specific reasons, and a device that
  ignored a value should look like it ignored it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.components.number import NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .battery_plan import battery_plan_numbers
from .const import EXPORT_LIMIT_DEFAULT_STEP_PCT
from .const import EXPORT_LIMIT_MIN_STEP_PCT
from .coordinator import SunSpec2ConfigEntry
from .coordinator import SunSpecDataUpdateCoordinator
from .entity import SunSpecEntity
from .entity import get_sunspec_unique_id
from .errors import SunSpecError
from .errors import user_error
from .franklinwh_controls import franklinwh_numbers
from .models import SunSpecModelWrapper
from .storage_modes import storage_setpoint_numbers
from .vendor_blocks import raw_block_numbers
from .write_controls import PLATFORM_NUMBER
from .write_controls import WriteControlSpec
from .write_controls import active_specs_for_platform

_LOGGER: logging.Logger = logging.getLogger(__package__)

PARALLEL_UPDATES = 0

# SunSpec spells "percent of some reference quantity" several ways, and
# model_124.json ships one with a leading space (" % WChaMax"). Each of
# them is a plain percent to the frontend. The reference quantity
# belongs in the entity name.
_PERCENT_UNITS = frozenset(
    {"%", "Pct", "% WMax", "% WChaMax", "% WDisChaMax", "% VArMax", "% VArAval"}
)


def _step_from_scale_factor(wrapper: SunSpecModelWrapper, point_name: str) -> float:
    """Return the UI step for a point, derived from its SunSpec scale factor.

    The step is ``10 ** SF`` within the bounds given at
    ``EXPORT_LIMIT_DEFAULT_STEP_PCT`` (#17), and the default step when
    the point has no usable scale factor.
    """
    # The scale factor is read through ``getValue(<sf point name>)`` and
    # not through ``getPoint(point_name).sf_value``. pysunspec2 fills
    # ``Point.sf_value`` only as a side effect of the first computed get
    # or set, so ``sf_value`` is still None when the entity is built. A
    # device that answers its ``*_SF`` register with 0x8000 ("not
    # implemented") yields None here, and the default step applies.
    try:
        sf_name = wrapper.getMeta(point_name).get("sf")
        sf = wrapper.getValue(sf_name) if isinstance(sf_name, str) else sf_name
    except (KeyError, AttributeError, TypeError):
        return EXPORT_LIMIT_DEFAULT_STEP_PCT
    if not isinstance(sf, int) or isinstance(sf, bool):
        return EXPORT_LIMIT_DEFAULT_STEP_PCT
    return min(EXPORT_LIMIT_DEFAULT_STEP_PCT, max(10.0**sf, EXPORT_LIMIT_MIN_STEP_PCT))


def _unit_for(spec: WriteControlSpec, wrapper: SunSpecModelWrapper) -> str | None:
    """Return the entity's unit: the spec's override, else the unit of the model definition."""
    if spec.unit is not None:
        return spec.unit
    try:
        raw = wrapper.getMeta(spec.point_name).get("units")
    except (KeyError, AttributeError):
        return None
    if isinstance(raw, str) and raw.strip() in _PERCENT_UNITS:
        return PERCENTAGE
    return None


def build_specs(
    coordinator: SunSpecDataUpdateCoordinator, platform: str
) -> Iterator[tuple[WriteControlSpec, SunSpecModelWrapper]]:
    """Yield (spec, wrapper) for every control of ``platform`` this device supports.

    The number, switch and select platforms all build from this
    generator, so they agree on which models are present.
    """
    vendor = coordinator.vendor
    hidden = vendor.storage.hidden_points if vendor and vendor.storage else frozenset()
    unsupported = vendor.unsupported_controls if vendor else frozenset()
    for spec in active_specs_for_platform(coordinator.detected_models, platform, unsupported):
        if spec.unique_key in hidden:
            # The vendor profile writes this register through its own
            # entities, in watts. The generic percent entity stays
            # available but starts disabled, so the two cannot disagree
            # by default.
            spec = replace(spec, enabled_by_default=False)
        step = vendor.control_steps.get(spec.unique_key) if vendor else None
        if step is not None and spec.native_step is None:
            spec = replace(spec, native_step=step)
        model_wrapper = (coordinator.data or {}).get(spec.model_id)
        if model_wrapper is None and spec.model_id in coordinator.refused_control_models:
            # The coordinator already warned when the device refused it.
            continue
        if model_wrapper is None:
            # The coordinator polls every model it builds controls
            # for (write_model_filter), so a detected model without
            # data means that rule is broken somewhere. The warning
            # keeps the missing entities from going unnoticed.
            getattr(coordinator, "_log", _LOGGER).warning(
                "Model %s was detected, but it is not in the polled data, so no "
                "write entities will be created for it. write_model_filter=%s",
                spec.model_id,
                getattr(coordinator, "write_model_filter", None),
            )
            continue
        yield spec, model_wrapper


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the write Number entities the device and the options allow."""
    coordinator = entry.runtime_data
    device_info = coordinator.device_info
    if device_info is None:
        return

    prefix = entry.options.get("prefix", "")
    entities: list[NumberEntity] = [
        SunSpecWriteNumber(
            coordinator=coordinator,
            config_entry=entry,
            device_info=device_info,
            model_info=wrapper.getGroupMeta(),
            prefix=prefix,
            spec=spec,
            model_wrapper=wrapper,
        )
        for spec, wrapper in build_specs(coordinator, PLATFORM_NUMBER)
    ]
    # The watt setpoints of the vendor's battery modes, where a profile
    # defines them.
    entities.extend(storage_setpoint_numbers(coordinator, entry, prefix))
    # The reserve and capacity of the scheduled discharge, same condition.
    entities.extend(battery_plan_numbers(coordinator, entry, prefix))
    # The vendor's registers outside the models, where a profile has them.
    entities.extend(raw_block_numbers(coordinator, entry, prefix))
    entities.extend(franklinwh_numbers(coordinator, entry, prefix))
    async_add_devices(entities)


class SunSpecWriteNumber(SunSpecEntity, NumberEntity):
    """A writable continuous-value SunSpec point."""

    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        device_info: SunSpecModelWrapper,
        model_info: dict[str, Any],
        prefix: str,
        spec: WriteControlSpec,
        model_wrapper: SunSpecModelWrapper,
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
        # Every number spec sets both bounds. They default to None in
        # the dataclass because it also describes switches and selects.
        # A None bound is not assigned, so NumberEntity keeps its own
        # default and never compares a reading against None.
        if spec.native_min is not None:
            self._attr_native_min_value = spec.native_min
        if spec.native_max is not None:
            self._attr_native_max_value = spec.native_max
        self._attr_native_step = (
            spec.native_step
            if spec.native_step is not None
            else _step_from_scale_factor(model_wrapper, spec.point_name)
        )
        self._attr_native_unit_of_measurement = _unit_for(spec, model_wrapper)

    @property
    def _point_name(self) -> str:
        """Return the SunSpec point this entity writes. Read by the tests."""
        return self._spec.point_name

    @property
    def native_value(self) -> float | None:
        wrapper = self.coordinator.data.get(self._spec.model_id)
        if wrapper is None:
            return None
        try:
            value = wrapper.getValue(self._spec.point_name)
        except (KeyError, AttributeError):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_write_points_locked(
                self._spec.model_id, [(self._spec.point_name, value)]
            )
        except SunSpecError as exc:
            raise user_error(
                "write_failed", target=f"{self._spec.point_name}={value}", error=exc
            ) from exc
        # The refresh shows the inverter's response at once, which
        # differs from the written value when the inverter clamps it.
        # The request has to stay outside the gateway lock the write
        # held: asyncio.Lock is not reentrant and the refresh debouncer
        # runs the refresh inline, so a request under the lock deadlocks.
        await self.coordinator.async_request_refresh()
