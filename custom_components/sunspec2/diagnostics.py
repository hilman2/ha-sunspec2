"""Diagnostics support for the SunSpec 2 integration.

Implements the standard Home Assistant diagnostics platform hook.
Users reach this via Settings -> Devices & Services -> SunSpec 2 ->
three dots -> Download diagnostics. The resulting JSON is meant to be
attached to GitHub issues.

The host field is redacted because it is often a public IP. Port and
unit_id are kept (they are non-sensitive and we need them to triage).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, DOMAIN, VERSION

TO_REDACT = {CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    scanned_models: list[dict[str, Any]] = []
    latest_values: dict[str, dict[str, Any]] = {}
    if coordinator.data:
        for model_id, wrapper in coordinator.data.items():
            try:
                gdef = wrapper.getGroupMeta()
                scanned_models.append(
                    {
                        "model_id": model_id,
                        "name": gdef.get("name"),
                        "label": gdef.get("label"),
                        "num_models": wrapper.num_models,
                        "keys": list(wrapper.getKeys()),
                    }
                )
                latest_values[str(model_id)] = {
                    key: _safe_value(wrapper, key) for key in wrapper.getKeys()
                }
            except Exception as exc:  # noqa: BLE001 - defensive: never break the dump
                scanned_models.append({"model_id": model_id, "error": str(exc)})

    try:
        from importlib.metadata import version as _version

        sunspec2_version = _version("pysunspec2")
    except Exception:  # noqa: BLE001
        sunspec2_version = "unknown"

    return {
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "scanned_models": scanned_models,
        "latest_values": latest_values,
        "recent_errors": list(getattr(coordinator, "_recent_errors", [])),
        "raw_captures": list(getattr(coordinator.api, "_captured_reads", [])),
        "versions": {
            "homeassistant": HA_VERSION,
            "pysunspec2": sunspec2_version,
            "sunspec2_integration": VERSION,
        },
    }


def _safe_value(wrapper, key: str) -> Any:
    """Read one point and coerce to a JSON-friendly type, or capture the error."""
    try:
        value = wrapper.getValue(key)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
