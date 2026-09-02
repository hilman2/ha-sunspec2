"""Sensors for what a vendor's model 160 modules carry.

Model 160 lists DC inputs as numbered modules, and the generic sensors
name them that way: "Module 2 DC power". Some vendors put more than PV
strings there. A Fronius GEN24 reports its battery as two further
modules, one for charging and one for discharging, and the only thing
that says so is the ``IDStr`` label. The vendor profile reads the
label, and this module turns the answer into sensors named by role:
battery charge and discharge power, the two lifetime energies, and PV
power as the sum over the string modules.

The generic module sensors stay. These read the same registers under
a name that says what they are, which is what the Energy Dashboard and
an automation want to bind to.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.helpers.entity import Entity

from . import SunSpecDataUpdateCoordinator
from . import get_sunspec_unique_id
from .const import DOMAIN
from .entity import SunSpecEntity
from .models import SunSpecModelWrapper
from .sensor import SunSpecEnergySensor
from .sensor import SunSpecSensor
from .vendors import ModuleRole

_LOGGER: logging.Logger = logging.getLogger(__package__)

MODEL_MPPT = 160

#: One sensor per row: the role whose module is read, the point read
#: from it, and the translation key that names the sensor.
CHANNEL_SENSORS: tuple[tuple[ModuleRole, str, str], ...] = (
    (ModuleRole.BATTERY_CHARGE, "DCW", "battery_charge_power"),
    (ModuleRole.BATTERY_DISCHARGE, "DCW", "battery_discharge_power"),
    (ModuleRole.BATTERY_CHARGE, "DCWH", "battery_charged_energy"),
    (ModuleRole.BATTERY_DISCHARGE, "DCWH", "battery_discharged_energy"),
)


def module_roles(
    module_role: Callable[[str], ModuleRole | None],
    wrapper: SunSpecModelWrapper,
    model_index: int,
) -> dict[int, tuple[str, ModuleRole]]:
    """The labelled modules of one model 160 instance.

    Args:
        module_role (Callable[[str], ModuleRole|None]): The vendor's
            label reader, see ``VendorProfile.module_role``.
        wrapper (SunSpecModelWrapper): The model 160 wrapper.
        model_index (int): Which instance, for a device with several.

    Returns:
        dict: Module index to ``(label, role)``, for the modules whose
        label the vendor recognises.
    """
    count = wrapper.getValue("N", model_index)
    if not isinstance(count, int):
        return {}
    roles: dict[int, tuple[str, ModuleRole]] = {}
    for idx in range(count):
        label = wrapper.getValue(f"module:{idx}:IDStr", model_index)
        if not isinstance(label, str):
            continue
        role = module_role(label)
        if role is not None:
            roles[idx] = (label, role)
    return roles


class DcChannelSensor(SunSpecSensor):
    """One model 160 module point, named by what the module carries.

    Built from the same ``data`` dict as a generic sensor, with three
    more keys: ``role``, ``translation_key`` and ``module_label``.
    """

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        data: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, config_entry, data)
        self._role: ModuleRole = data["role"]
        self._module_label: str = data["module_label"]
        self._attr_translation_key = data["translation_key"]
        # Keyed by role, not by module index: the generic sensor for the
        # same register keeps its "module:3:DCW" id, and this one follows
        # the battery if a firmware update renumbers the modules.
        point = self.key.rsplit(":", 1)[-1]
        self._unique_id = get_sunspec_unique_id(
            config_entry.entry_id, f"{self._role.value}:{point}", self.model_id, self.model_index
        )

    @property
    def name(self) -> str:
        """The translated role name, the way Home Assistant names entities.

        SunSpecSensor hand-rolls its name from the SunSpec label and the
        module index, and by overriding ``name`` it steps around the
        translation key. Handing the call back to ``Entity.name`` puts
        the key to use again; the hand-rolled name stays the fallback
        for a language file without the key.
        """
        name = Entity.name.__get__(self, type(self))
        return name if isinstance(name, str) else self._name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["module_label"] = self._module_label
        return attrs


class DcChannelEnergySensor(DcChannelSensor, SunSpecEnergySensor):
    """The lifetime counter of a channel, with the energy sensor's guards.

    The order of the bases matters: ``DcChannelSensor`` names the entity
    and ``SunSpecEnergySensor`` reads it, so the first must come first
    for ``name`` and the second for ``native_value``.
    """


class PvPowerSensor(SunSpecEntity, SensorEntity):
    """DC power summed over the modules the vendor labels as PV strings."""

    _attr_translation_key = "pv_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        device_info: SunSpecModelWrapper,
        wrapper: SunSpecModelWrapper,
        prefix: str,
        model_index: int,
        modules: tuple[int, ...],
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            device_info,
            wrapper.getGroupMeta(),
            prefix=prefix,
            model_id=MODEL_MPPT,
        )
        self._model_index = model_index
        self._modules = modules
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, f"{ModuleRole.PV.value}:DCW", MODEL_MPPT, model_index
        )

    @property
    def native_value(self) -> float | None:
        """The sum of the strings' ``DCW``, or None when no string reports one.

        A string that reports nothing is left out rather than voiding
        the sum: a two-string inverter with one string unused keeps
        showing what the other makes.
        """
        wrapper = self.coordinator.data.get(MODEL_MPPT)
        if wrapper is None:
            return None
        total = 0.0
        seen = False
        for idx in self._modules:
            value = wrapper.getValue(f"module:{idx}:DCW", self._model_index)
            if isinstance(value, (int, float)):
                total += value
                seen = True
        return total if seen else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "integration": DOMAIN,
            "modules": list(self._modules),
        }


def dc_channel_sensors(
    coordinator: SunSpecDataUpdateCoordinator,
    config_entry: ConfigEntry,
    device_info: SunSpecModelWrapper,
    wrapper: SunSpecModelWrapper,
    prefix: str,
) -> list[SensorEntity]:
    """The role-named sensors for a device's model 160.

    Args:
        coordinator (SunSpecDataUpdateCoordinator): Carries the vendor
            profile; without one, or with one that does not label its
            modules, there is nothing to build.
        config_entry (ConfigEntry): The entry the sensors belong to.
        device_info (SunSpecModelWrapper): Common model 1.
        wrapper (SunSpecModelWrapper): Model 160.
        prefix (str): The user's device prefix.

    Returns:
        list[SensorEntity]: The sensors, possibly none.
    """
    vendor = coordinator.vendor
    if vendor is None or vendor.module_role is None:
        return []
    sensors: list[SensorEntity] = []
    for model_index in range(wrapper.num_models):
        roles = module_roles(vendor.module_role, wrapper, model_index)
        if not roles:
            continue
        _LOGGER.debug("Model 160 instance %d modules by role: %s", model_index, roles)
        for role, point, translation_key in CHANNEL_SENSORS:
            for idx, (label, module_role) in roles.items():
                if module_role is not role:
                    continue
                data = {
                    "device_info": device_info,
                    "key": f"module:{idx}:{point}",
                    "model_id": MODEL_MPPT,
                    "model_index": model_index,
                    "model": wrapper,
                    "prefix": prefix,
                    "role": role,
                    "translation_key": translation_key,
                    "module_label": label,
                }
                cls = DcChannelEnergySensor if point == "DCWH" else DcChannelSensor
                sensors.append(cls(coordinator, config_entry, data))
                # One battery: a second module with the same role would
                # be a second entity under the same unique id.
                break
        pv_modules = tuple(
            idx for idx, (_, module_role) in roles.items() if module_role is ModuleRole.PV
        )
        if pv_modules:
            sensors.append(
                PvPowerSensor(
                    coordinator, config_entry, device_info, wrapper, prefix, model_index, pv_modules
                )
            )
    return sensors
