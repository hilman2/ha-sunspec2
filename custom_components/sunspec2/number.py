"""Number platform for SunSpec write controls.

EXPERIMENTAL, opt-in. Entities only register when the user has ticked
"Enable experimental write controls (BETA)" in the options flow and the
inverter exposes a model :mod:`write_controls` has specs for.

v0.19.0 replaced one hand-written class per point with one class driven
by a :class:`~.write_controls.WriteControlSpec`. The platform now spans
three models (123 immediate controls, 704 DER AC controls, 124 basic
storage control) and the per-point classes were already mostly
identical boilerplate.

Behavioural notes:

- Writes go through ``coordinator.async_write_points_locked``, which
  takes the per-``(host, port)`` gateway lock for the duration of the
  write. The session is handed back before the lock is released only
  where it is not held open anyway (``release_slot_between_polls``);
  since v0.22.0 one session normally stays up across polls and writes
  alike. Releasing it under the lock is deliberate: the next
  coordinator on this gateway must not inherit a lock it cannot
  connect under.
  Calling ``coordinator.api.async_write_point`` directly, which is what
  this platform did before v0.14.0, bypasses that lock entirely and
  lets a write interleave with the poll cycle on one socket.
- After a successful write we trigger a coordinator refresh so the
  read-side state catches up immediately instead of waiting for the
  next scheduled cycle.
- ``native_value`` reads from ``coordinator.data``, populated by the
  normal read cycle, so the entity state reflects what the inverter
  reports rather than what we last wrote. Inverters clamp and refuse
  writes for vendor-specific reasons, and a device that ignored a value
  should look like it ignored it.
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SunSpec2ConfigEntry
from . import SunSpecDataUpdateCoordinator
from . import get_sunspec_unique_id
from .const import CONF_WRITE_BETA_ENABLED
from .const import EXPORT_LIMIT_DEFAULT_STEP_PCT
from .const import EXPORT_LIMIT_MIN_STEP_PCT
from .discharge_plan import discharge_plan_numbers
from .entity import SunSpecEntity
from .errors import SunSpecError
from .models import SunSpecModelWrapper
from .storage_modes import storage_setpoint_numbers
from .vendor_blocks import raw_block_numbers
from .write_controls import PLATFORM_NUMBER
from .write_controls import STORAGE_CONTROL_MODEL
from .write_controls import WriteControlSpec
from .write_controls import active_specs_for_platform

_LOGGER: logging.Logger = logging.getLogger(__package__)

PARALLEL_UPDATES = 0

# SunSpec spells "percent of some reference quantity" several ways, and
# model_124.json even ships one with a leading space (" % WChaMax").
# They are all a percent as far as the frontend is concerned; which
# quantity it is a percent OF belongs in the entity name.
_PERCENT_UNITS = frozenset(
    {"%", "Pct", "% WMax", "% WChaMax", "% WDisChaMax", "% VArMax", "% VArAval"}
)


def has_point(wrapper: SunSpecModelWrapper, point_name: str) -> bool:
    """True if the device actually implements this point.

    A device can implement a model without implementing every optional
    point in it. Building a control for a missing point would give the
    user an entity whose every write fails.
    """
    try:
        wrapper.getPoint(point_name)
    except (KeyError, AttributeError, IndexError, TypeError):
        return False
    return True


def _step_from_scale_factor(wrapper: SunSpecModelWrapper, point_name: str) -> float:
    """Derive the UI step from the point's SunSpec scale factor.

    A device reporting SF -1 stores 995 for 99.5 %, and with a hardcoded
    step of 1 the frontend number box rejects 99.5: the inverter's own
    value becomes unenterable (#17).

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


def _unit_for(spec: WriteControlSpec, wrapper: SunSpecModelWrapper) -> str | None:
    """Resolve the entity's unit: spec override first, model definition second."""
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
    """Yield (spec, wrapper) for every control this device can support.

    Shared by the number, switch and select platforms so the three
    cannot drift on which models they consider present. The battery
    controls of model 124 come for any device that has the block; the
    export and grid controls of 123 and 704 only while the write beta
    is on.
    """
    vendor = coordinator.vendor
    hidden = vendor.storage.hidden_points if vendor and vendor.storage else frozenset()
    write_beta = coordinator.entry.options.get(CONF_WRITE_BETA_ENABLED, False)
    for spec in active_specs_for_platform(coordinator.detected_models, platform):
        if spec.model_id != STORAGE_CONTROL_MODEL and not write_beta:
            continue
        if spec.unique_key in hidden:
            # The vendor profile writes this register through its own
            # entities, in watts. The generic percent entity stays
            # available for whoever wants it, but disabled, so the two
            # cannot disagree by default.
            spec = replace(spec, enabled_by_default=False)
        model_wrapper = (coordinator.data or {}).get(spec.model_id)
        if model_wrapper is None:
            # Unreachable in the normal path since v0.14.0: the
            # coordinator adds every model it builds controls for to
            # the polled set. If it trips again something new is
            # broken, and silently returning is what made this class
            # of bug invisible for three releases.
            getattr(coordinator, "_log", _LOGGER).warning(
                "Model %s was detected, but it is not in the polled data, so no "
                "write entities will be created for it. write_model_filter=%s",
                spec.model_id,
                getattr(coordinator, "write_model_filter", None),
            )
            continue
        if not has_point(model_wrapper, spec.point_name):
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
    entities.extend(discharge_plan_numbers(coordinator, entry, prefix))
    # The vendor's registers outside the models, where a profile has them.
    entities.extend(raw_block_numbers(coordinator, entry, prefix))
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
        # Every number spec sets both bounds. The dataclass defaults them
        # to None because the same class also describes switches and
        # selects, which have no bounds at all. Skipping the assignment
        # leaves NumberEntity's own defaults in place, rather than handing
        # it a None it would later compare a reading against.
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
        """The SunSpec point this entity writes. Read by the tests."""
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
            raise HomeAssistantError(
                f"Failed to write {self._spec.point_name}={value}: {exc}"
            ) from exc
        # Refresh the read-side state immediately so the UI shows the
        # inverter's actual response, which may differ from the value we
        # just sent if the inverter clamps it.
        #
        # Deliberately OUTSIDE the gateway lock the write just held:
        # asyncio.Lock is not reentrant and the refresh debouncer runs
        # the refresh inline, so requesting it under the lock deadlocks.
        await self.coordinator.async_request_refresh()
