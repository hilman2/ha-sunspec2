"""Select platform for SunSpec write controls.

EXPERIMENTAL, opt-in, same gating as the Number and Switch platforms.
New in v0.19.0, and it exists for one point in particular.

``StorCtl_Mod`` (model 124) is a ``bitfield16``: bit 0 gates the charge
rate, bit 1 gates the discharge rate. The obvious rendering is two
switches, and it is wrong. Each switch would read its read-modify-write
base out of ``coordinator.data``, which is at most one scan interval
fresh, so an automation flipping both in the same second would send two
writes computed from the same stale base and the second would silently
clobber the first. One entity with the four combinations cannot do that
to itself.

``WSetMod`` (model 704) is a genuine multi-value enum and lands here for
the ordinary reason.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SunSpec2ConfigEntry
from . import get_sunspec_unique_id
from .const import CONF_WRITE_BETA_ENABLED
from .entity import SunSpecEntity
from .errors import SunSpecError
from .number import build_specs
from .write_controls import PLATFORM_SELECT
from .write_controls import WriteControlSpec

_LOGGER: logging.Logger = logging.getLogger(__package__)

PARALLEL_UPDATES = 0


def storage_bits_to_int(symbols: list[str]) -> int:
    """Fold a decoded ``StorCtl_Mod`` bitfield back into its raw integer.

    pysunspec2 hands back the names of the set bits rather than the
    value. Bit 0 is charge, bit 1 is discharge.

    Matched by prefix and case-insensitively on purpose:
    model_124.json spells the second symbol "DiSCHARGE", and a device
    is free to report either the spec's spelling or its own. Checking
    the discharge prefix first matters, because "DISCHARGE" also starts
    with the letters of "CHARGE" under a naive substring test.
    """
    raw = 0
    for name in symbols:
        upper = name.upper()
        if upper.startswith("DISCHA"):
            raw |= 2
        elif upper.startswith("CHA"):
            raw |= 1
    return raw


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the experimental write Select entities, gated by the beta flag."""
    coordinator = entry.runtime_data
    if not entry.options.get(CONF_WRITE_BETA_ENABLED, False):
        return

    device_info = coordinator.device_info
    if device_info is None:
        return

    async_add_devices(
        [
            SunSpecWriteSelect(
                coordinator=coordinator,
                config_entry=entry,
                device_info=device_info,
                model_info=wrapper.getGroupMeta(),
                prefix=entry.options.get("prefix", ""),
                spec=spec,
            )
            for spec, wrapper in build_specs(coordinator, PLATFORM_SELECT)
        ]
    )


class SunSpecWriteSelect(SunSpecEntity, SelectEntity):
    """A writable SunSpec point with a fixed set of named values."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator,
        config_entry,
        device_info,
        model_info,
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
        self._attr_options = list(spec.options)
        self._value_to_option = {value: name for name, value in spec.options.items()}

    @property
    def _point_name(self) -> str:
        """The SunSpec point this entity writes. Read by the tests."""
        return self._spec.point_name

    @property
    def current_option(self) -> str | None:
        wrapper = self.coordinator.data.get(self._spec.model_id)
        if wrapper is None:
            return None
        try:
            value = wrapper.getValue(self._spec.point_name)
        except (KeyError, AttributeError):
            return None
        if value is None:
            return None
        # A bitfield decodes to a list of set bit names, an enum to its
        # symbol or its ordinal, depending on firmware and on whether
        # pysunspec2 could resolve the symbol table.
        if isinstance(value, list):
            value = storage_bits_to_int(value)
        if isinstance(value, str):
            return value if value in self._spec.options else None
        return self._value_to_option.get(int(value))

    async def async_select_option(self, option: str) -> None:
        if option not in self._spec.options:
            raise HomeAssistantError(f"{option} is not a valid option for {self._point_name}")
        raw_value = self._spec.options[option]
        # Routed through the coordinator so the write holds the
        # per-gateway lock. See number.py's module docstring.
        try:
            await self.coordinator.async_write_points_locked(
                self._spec.model_id, [(self._spec.point_name, raw_value)]
            )
        except SunSpecError as exc:
            raise HomeAssistantError(
                f"Failed to write {self._point_name}={raw_value}: {exc}"
            ) from exc
        # Outside the lock: the refresh debouncer runs inline and
        # asyncio.Lock is not reentrant.
        await self.coordinator.async_request_refresh()
