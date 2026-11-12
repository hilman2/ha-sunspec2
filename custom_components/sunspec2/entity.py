"""The base entity, its unique id, and the device registry payload of a SunSpec model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .const import STALE_DATA_TOLERANCE_CYCLES
from .coordinator import SunSpecDataUpdateCoordinator
from .model_labels import device_model_suffix
from .models import SunSpecModelWrapper
from .models import point_text


def get_sunspec_unique_id(config_entry_id: str, key: str, model_id: int, model_index: int) -> str:
    """The unique id of an entity.

    Its form is fixed: existing installations have it in their entity
    registry, and ``migration.py`` rewrites the ids of cjne/ha-sunspec
    into it. A change orphans every entity and its history.
    """
    return f"{config_entry_id}_{key}-{model_id}-{model_index}"


class SunSpecEntity(CoordinatorEntity[SunSpecDataUpdateCoordinator]):
    # An entity names only itself ("AC power"). Home Assistant puts the
    # device name in front ("Powador 7.8 TL3 Inverter (Three Phase)").
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        device_info: SunSpecModelWrapper,
        model_info: dict[str, Any],
        prefix: str = "",
        model_id: int | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._device_data = device_info
        self.config_entry = config_entry
        self.model_info = model_info
        # The user's device name from CONF_PREFIX, or "". It takes the
        # place of the inverter's Md string in the device name, so two
        # inverters of one model can be told apart.
        self._prefix = prefix
        # The SunSpec model id (103, 160, ...). It feeds the device name
        # suffix and the device's "model_id" field, which tell the
        # devices of one entry apart (#33). Optional only for test stubs.
        self._model_id = model_id

    @property
    def available(self) -> bool:
        """Whether the entity reports a state, a stale one included.

        True while the coordinator has data and no more than
        STALE_DATA_TOLERANCE_CYCLES update cycles in a row have failed.
        A brief connectivity blip therefore leaves no gap in the
        long-term statistics.
        """
        # The decision reads consecutive_failed_cycles and must not defer
        # to coordinator.last_update_success, as CoordinatorEntity.available
        # does. Home Assistant sets last_update_success to False at the
        # first failed refresh, which would end the tolerance before it
        # starts. The coordinator's _count_failed_cycle notifies the
        # listeners when the counter crosses the tolerance, because Home
        # Assistant does not notify them on consecutive failures.
        coordinator = self.coordinator
        if coordinator.data is None:
            return False
        counter = getattr(coordinator, "consecutive_failed_cycles", 0)
        return counter <= STALE_DATA_TOLERANCE_CYCLES

    @property
    def device_info(self) -> DeviceInfo:
        """The device registry payload of this entity's SunSpec model, see ``device_info_for``."""
        return device_info_for(
            self.config_entry, self._device_data, self.model_info, self._prefix, self._model_id
        )


@dataclass(frozen=True)
class Home:
    """The SunSpec model device an entity from outside the models sits on.

    Args:
        device_info: Common model 1.
        model_info: The model's group definition.
        model_id: The SunSpec model id.
    """

    device_info: SunSpecModelWrapper
    model_info: dict[str, Any]
    model_id: int


def home_for(coordinator: SunSpecDataUpdateCoordinator, preferred: tuple[int, ...]) -> Home | None:
    """The device to sit on: the first preferred model the device has, else its lowest model.

    None before the first cycle, when there is no model to sit on yet.
    """
    if coordinator.device_info is None or not coordinator.data:
        return None
    candidates = [model_id for model_id in preferred if model_id in coordinator.data]
    if not candidates:
        candidates = sorted(model_id for model_id in coordinator.data if model_id != 1)
    if not candidates:
        return None
    model_id = candidates[0]
    return Home(coordinator.device_info, coordinator.data[model_id].getGroupMeta(), model_id)


def device_info_for(
    config_entry: ConfigEntry,
    device_data: SunSpecModelWrapper,
    model_info: dict[str, Any],
    prefix: str,
    model_id: int | None,
) -> DeviceInfo:
    """The device registry payload for one SunSpec model of one entry.

    Shared by every entity that sits on such a device, whichever
    coordinator feeds it. The device name is "<base> <model suffix>",
    e.g. "Powador 7.8 TL3 Inverter (Three Phase)". The base is ``prefix``
    when the user gave one, otherwise ``Md`` of common model 1.

    Args:
        config_entry: The entry the device belongs to.
        device_data: Common model 1.
        model_info: The model's group definition.
        prefix: The user's device name from CONF_PREFIX, or "".
        model_id: The SunSpec model id.
    """
    md = point_text(device_data, "Md") or None
    # The integration creates one device per SunSpec model. Without the
    # suffix every device of an entry carries the same Md string (#33).
    # A name the user set in Home Assistant stays: the registry keeps
    # the name built here only as the original name and never overwrites
    # ``name_by_user``.
    suffix = device_model_suffix(model_id, model_info)
    base = prefix or md
    device_name: str | None
    if base and suffix:
        device_name = f"{base} {suffix}"
    else:
        device_name = base or suffix
    info = DeviceInfo(
        # HA types identifiers as set[tuple[str, str]] and this is a
        # three-tuple: with one device per SunSpec model the entry id
        # alone does not identify a device, and the registry stores
        # whatever tuple it is handed. These identifiers are written on
        # every installed system. Narrowing them to two elements would
        # orphan every existing device and take the user's history,
        # automations and dashboards with it.
        identifiers={
            (DOMAIN, config_entry.entry_id, model_info["name"])  # type: ignore[arg-type]
        },
        name=device_name,
        model=md,
        sw_version=device_data.getValue("Vr"),
        manufacturer=device_data.getValue("Mn"),
    )
    # The device page renders model_id as "<Md> (SunSpec <id>) by
    # <manufacturer>", and the id matches the "(<id>)" suffix in the
    # options flow's model multi-select.
    if model_id is not None:
        info["model_id"] = f"SunSpec {model_id}"
    return info
