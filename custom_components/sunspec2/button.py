"""Button platform: explicit battery and vendor web-interface actions."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SunSpec2ConfigEntry
from .franklinwh_controls import franklinwh_buttons
from .fronius_web_entities import fronius_web_buttons

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    prefix = entry.options.get("prefix", "")
    async_add_devices(
        fronius_web_buttons(entry.runtime_data, entry, prefix)
        + franklinwh_buttons(entry.runtime_data, entry, prefix)
    )
