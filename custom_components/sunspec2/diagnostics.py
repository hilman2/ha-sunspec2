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
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from . import SunSpec2ConfigEntry
from .const import CONF_HOST
from .const import CONF_MAX_AC_POWER_KW
from .const import MEASURED_POWER_POINT_HEADROOM
from .const import NAMEPLATE_FILTER_HEADROOM
from .const import VERSION
from .const import effective_peak_power_kw

TO_REDACT = {CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SunSpec2ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

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
        # #17: scanned_models below is built from coordinator.data,
        # which is the FILTERED poll result. A model the inverter
        # exposes but the user never ticked can therefore never show up
        # there, which is exactly why a KACO Blueplanet that had been
        # answering writes to registers 40295 and 40299 for two years
        # appeared to have no model 123 at all.
        #
        # detected_models is the raw scan result and is the field to
        # look at when the question is "does this device have model N".
        "detected_models": sorted(getattr(coordinator, "detected_models", set()) or []),
        "model_filters": {
            "option_model_filter": sorted(getattr(coordinator, "option_model_filter", set()) or []),
            "write_model_filter": sorted(getattr(coordinator, "write_model_filter", set()) or []),
            "polled_models": sorted(coordinator.data or {}),
        },
        "scanned_models": scanned_models,
        "latest_values": latest_values,
        # #45: without this block a diagnostics download could not
        # diagnose the plausibility filter at all. Models 120 / 121 are
        # not in DEFAULT_MODELS, so WRtg / WMax normally never reach
        # latest_values either, and when the peak power option is unset
        # the ceiling that actually dropped the reading was invisible.
        "plausibility_filter": _plausibility_filter_dump(coordinator, entry),
        "recent_errors": _recent_errors_dump(coordinator),
        "consecutive_failures": dict(getattr(coordinator, "_consecutive_failures", {})),
        # #52: whether the transport repair issue is currently allowed
        # to fire at all. A dump taken from an install that "never warns
        # about the dead inverter" is otherwise impossible to read: both
        # inputs to that decision live only in memory.
        "standby": {
            "last_operating_state": getattr(coordinator, "_last_operating_state", None),
            "standby_when_idle_option": getattr(coordinator, "standby_when_idle", False),
            "downtime_is_expected": coordinator._downtime_is_expected()
            if hasattr(coordinator, "_downtime_is_expected")
            else None,
        },
        "raw_captures": list(getattr(coordinator.api, "_captured_reads", [])),
        "versions": {
            "homeassistant": HA_VERSION,
            "pysunspec2": sunspec2_version,
            "sunspec2_integration": VERSION,
        },
    }


def _plausibility_filter_dump(coordinator, entry) -> dict[str, Any]:
    """State of the power plausibility filter, in the units it works in.

    ``effective_peak_power_kw`` is the same function the sensor platform
    calls, so this can never disagree with what actually happened to a
    reading. ``per_quantity_ceiling_w`` is spelled out because the
    headroom differs by physical quantity and "why is DC Watts unknown
    but Watts fine" is exactly the question a bug report opens with.
    """
    configured = entry.options.get(CONF_MAX_AC_POWER_KW)
    detected = getattr(coordinator, "detected_max_ac_power_kw", None)
    peak_kw = effective_peak_power_kw(configured, detected)
    ceilings: dict[str, float | None] = {}
    for point in ("W", "VA", "VAr", "DCW"):
        headroom = MEASURED_POWER_POINT_HEADROOM[point]
        ceilings[point] = None if peak_kw is None else round(peak_kw * 1000.0 * headroom, 1)
    return {
        "configured_max_ac_power_kw": configured,
        "detected_max_ac_power_kw": detected,
        "detected_max_ac_power_source": getattr(coordinator, "detected_max_ac_power_source", None),
        "nameplate_filter_headroom": NAMEPLATE_FILTER_HEADROOM,
        "effective_peak_power_kw": peak_kw,
        "per_quantity_ceiling_w": ceilings,
        "enabled": peak_kw is not None,
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
