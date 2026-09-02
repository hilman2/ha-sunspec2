"""SunSpecEntity class"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .const import STALE_DATA_TOLERANCE_CYCLES
from .model_labels import device_model_suffix
from .models import SunSpecModelWrapper

if TYPE_CHECKING:
    # Imported for typing only. The coordinator lives in the package
    # __init__, which imports half the integration, so importing it for
    # real here would put every platform module behind that import.
    from . import SunSpecDataUpdateCoordinator


class SunSpecEntity(CoordinatorEntity["SunSpecDataUpdateCoordinator"]):
    # Bronze rule has-entity-name: the per-entity ``name`` property
    # carries only the point label (e.g. "Watts", "DC Voltage") and
    # the device name supplies the make-and-model prefix
    # (e.g. "Powador 7.8 TL3"). Home Assistant composes the two for
    # display, so the user sees "Powador 7.8 TL3 Watts" in the UI
    # without us hand-rolling the prefix in every sensor name.
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
        # Optional user prefix from CONF_PREFIX. When set it becomes
        # the device's display name (overriding the inverter's Md
        # field), so a user with two KACO Powadors can label them
        # "Garage" and "Cellar" instead of seeing two devices with
        # the same model name.
        self._prefix = prefix
        # Numeric SunSpec model id (103, 160, ...). Feeds the device
        # name suffix and the device-info "model_id" field so the per
        # model devices are distinguishable (issue #33). Optional only
        # for backwards compatibility with test stubs.
        self._model_id = model_id

    @property
    def available(self) -> bool:
        """Return whether the entity should be reported as available.

        Stale-data tolerance: inverters frequently see brief connectivity
        blips. Instead of bouncing the entity to "unavailable" on every
        single failed cycle (which leaves a gap in the long-term
        statistics graphs), we keep serving the last successfully read
        value through the coordinator's cached ``data`` for up to
        :data:`STALE_DATA_TOLERANCE_CYCLES` consecutive failures. The
        coordinator resets ``consecutive_failed_cycles`` to zero on the
        first successful read, so a recovered inverter immediately stops
        being "assumed".

        Note that this deliberately does NOT defer to
        ``coordinator.last_update_success`` the way the upstream
        ``CoordinatorEntity.available`` does. The coordinator's own
        ``_after_failed_cycle`` fires a manual listener notification
        when the failure counter crosses the tolerance threshold (HA's
        DataUpdateCoordinator stops dispatching listeners after the
        first consecutive failure on its own), and that notification
        happens BEFORE HA flips ``last_update_success`` to False. So
        we have to drive the available decision off the counter, not
        off ``last_update_success``, otherwise the manual notification
        would still see the entity as "available" and never write the
        unavailable state.
        """
        coordinator = self.coordinator
        if coordinator.data is None:
            return False
        counter = getattr(coordinator, "consecutive_failed_cycles", 0)
        return counter <= STALE_DATA_TOLERANCE_CYCLES

    @property
    def device_info(self) -> DeviceInfo:
        """Return the HA device registry payload for this entity.

        The device name is "<base> <model suffix>", e.g.
        "Powador 7.8 TL3 Inverter (Three Phase)". The base is
        ``self._prefix`` if the user supplied one via ``CONF_PREFIX``
        (explicit user override wins, useful for multi-inverter setups
        where the same model needs a location-based label), otherwise
        ``Md`` from common model 1, the inverter's own model string.

        The model suffix exists because the integration creates one HA
        device per SunSpec model: without it every device of an entry
        carries the identical Md string and the device list becomes a
        wall of indistinguishable entries (issue #33). Users who set
        their own device name in HA keep it - the registry only tracks
        this value as the original name and never overwrites
        ``name_by_user``.

        The numeric model id additionally lands in the ``model_id``
        field (HA 2024.8+), which the device-info card renders as
        "<Md> (SunSpec <id>) by <manufacturer>" and which matches the
        "(<id>)" suffix in the options-flow model multi-select.
        """
        return device_info_for(
            self.config_entry, self._device_data, self.model_info, self._prefix, self._model_id
        )


def device_info_for(
    config_entry: ConfigEntry,
    device_data: SunSpecModelWrapper,
    model_info: dict[str, Any],
    prefix: str,
    model_id: int | None,
) -> DeviceInfo:
    """The device registry payload for one SunSpec model of one entry.

    Shared by every entity that sits on such a device, whichever
    coordinator feeds it. See ``SunSpecEntity.device_info`` for how
    the name is composed.

    Args:
        config_entry (ConfigEntry): The entry the device belongs to.
        device_data (SunSpecModelWrapper): Common model 1.
        model_info (dict): The model's group definition.
        prefix (str): The user's device name, or "".
        model_id (int|None): The SunSpec model id.
    """
    try:
        md = device_data.getValue("Md")
    except (KeyError, AttributeError):
        md = None
    suffix = device_model_suffix(model_id, model_info)
    base = prefix or md
    device_name: str | None
    if base and suffix:
        device_name = f"{base} {suffix}"
    else:
        device_name = base or suffix
    info = DeviceInfo(
        # HA types identifiers as set[tuple[str, str]] and this is a
        # three-tuple. The integration creates one device per SunSpec
        # model, so the entry id alone does not identify a device, and
        # the registry stores whatever tuple it is handed. These
        # identifiers are already written on every installed system:
        # narrowing them to two elements would orphan every existing
        # device and take the user's history, automations and
        # dashboards with it.
        identifiers={
            (DOMAIN, config_entry.entry_id, model_info["name"])  # type: ignore[arg-type]
        },
        name=device_name,
        model=md,
        sw_version=device_data.getValue("Vr"),
        manufacturer=device_data.getValue("Mn"),
    )
    if model_id is not None:
        info["model_id"] = f"SunSpec {model_id}"
    return info
