"""SunSpecEntity class"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .const import STALE_DATA_TOLERANCE_CYCLES
from .models import SunSpecModelWrapper


class SunSpecEntity(CoordinatorEntity):
    def __init__(
        self,
        coordinator,
        config_entry: ConfigEntry,
        device_info: SunSpecModelWrapper,
        model_info: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._device_data = device_info
        self.config_entry = config_entry
        self.model_info = model_info

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
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id, self.model_info["name"])},
            "name": self.model_info["label"],
            "model": self._device_data.getValue("Md"),
            "sw_version": self._device_data.getValue("Vr"),
            "manufacturer": self._device_data.getValue("Mn"),
        }
