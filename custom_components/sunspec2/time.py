"""Time platform: the window of the scheduled battery discharge.

The entities come from :mod:`discharge_plan`, which builds them only
for a device with a battery and a vendor storage profile.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SunSpec2ConfigEntry
from .discharge_plan import discharge_plan_times

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    prefix = entry.options.get("prefix", "")
    async_add_devices(discharge_plan_times(entry.runtime_data, entry, prefix))
