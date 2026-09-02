"""Button platform: actions the Fronius web interface offers.

The entities come from :mod:`fronius_web_entities`, which builds them
only for an entry with a stored web interface login.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SunSpec2ConfigEntry
from .fronius_web_entities import fronius_web_buttons

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    prefix = entry.options.get("prefix", "")
    async_add_devices(fronius_web_buttons(entry.runtime_data, entry, prefix))
