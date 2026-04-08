"""SunSpecEntity class"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .const import STALE_DATA_TOLERANCE_CYCLES
from .models import SunSpecModelWrapper


class SunSpecEntity(CoordinatorEntity):
    # Bronze rule has-entity-name: the per-entity ``name`` property
    # carries only the point label (e.g. "Watts", "DC Voltage") and
    # the device name supplies the make-and-model prefix
    # (e.g. "Powador 7.8 TL3"). Home Assistant composes the two for
    # display, so the user sees "Powador 7.8 TL3 Watts" in the UI
    # without us hand-rolling the prefix in every sensor name.
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        config_entry: ConfigEntry,
        device_info: SunSpecModelWrapper,
        model_info: dict[str, Any],
        prefix: str = "",
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
    def device_info(self) -> dict[str, Any]:
        """Return the HA device registry payload for this entity.

        Naming priority for the device header (and therefore the
        prefix HA composes in front of every entity with
        ``has_entity_name`` set):

        1. ``self._prefix`` if the user supplied one via
           ``CONF_PREFIX`` - explicit user override wins, useful for
           multi-inverter setups where the same model needs a
           location-based label.
        2. ``Md`` from common model 1 - the inverter's own model
           string (e.g. "Powador 7.8 TL3"). The default for almost
           every install.
        3. The SunSpec block label or name as a last-ditch fallback
           when the device omits Md.
        """
        try:
            md = self._device_data.getValue("Md")
        except (KeyError, AttributeError):
            md = None
        device_name = (
            self._prefix or md or self.model_info.get("label") or self.model_info.get("name")
        )
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id, self.model_info["name"])},
            "name": device_name,
            "model": md,
            "sw_version": self._device_data.getValue("Vr"),
            "manufacturer": self._device_data.getValue("Mn"),
        }
