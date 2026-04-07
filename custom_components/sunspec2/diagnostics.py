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

    sunspec2_version = await hass.async_add_executor_job(_read_pysunspec2_version)

    return {
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "scanned_models": scanned_models,
        "latest_values": latest_values,
        "recent_errors": _recent_errors_dump(coordinator),
        "consecutive_failures": dict(
            getattr(coordinator, "_consecutive_failures", {})
        ),
        "raw_captures": list(getattr(coordinator.api, "_captured_reads", [])),
        "versions": {
            "homeassistant": HA_VERSION,
            "pysunspec2": sunspec2_version,
            "sunspec2_integration": VERSION,
        },
    }


def _recent_errors_dump(coordinator) -> dict[str, list]:
    """Serialise the per-category recent_errors dict for the JSON dump.

    Phase 3 stores _recent_errors as ``dict[str, deque[dict]]`` keyed by
    category. We turn each deque into a plain list so the dump is
    JSON-serialisable.

    Defensive against the Phase-2 shape (a single ``deque``) in case a
    test stub coordinator hands us the older form: in that case we wrap
    it under "transport" and leave the others empty. The integration
    itself never produces the old shape any more.
    """
    raw = getattr(coordinator, "_recent_errors", None)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {cat: list(buf) for cat, buf in raw.items()}
    # Phase-2 fallback: a flat sequence-like buffer.
    return {
        "transport": list(raw),
        "protocol": [],
        "device": [],
        "transient": [],
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


def _read_pysunspec2_version() -> str:
    """Read the pysunspec2 version via importlib.metadata.

    importlib.metadata.version() walks site-packages and reads the wheel
    METADATA file synchronously, which is forbidden inside the HA event
    loop. This helper exists so the diagnostics handler can offload it
    to an executor via hass.async_add_executor_job.
    """
    try:
        from importlib.metadata import version as _version

        return _version("pysunspec2")
    except Exception:  # noqa: BLE001
        return "unknown"
