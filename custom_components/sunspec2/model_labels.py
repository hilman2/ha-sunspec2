"""Lookup helper for SunSpec model group labels.

Reads the bundled pysunspec2 model JSON files at runtime to extract
the human-readable group label for a given model ID. Used by the
config-flow and options-flow forms so the model multi-select can
show "Inverter (Three Phase) (103)" instead of just "103".

Lazy + cached: each model JSON is read at most once per process,
even if the same form renders dozens of times. The lookup is
read-only and never raises - missing files or unparseable JSON
fall back to a generic ``"Model <id>"`` label so the form always
renders.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from importlib.resources import files
from typing import Any

_LOGGER = logging.getLogger(__name__)

_MODEL_PACKAGE = "sunspec2.models.json"

# Curated short names for SunSpec models whose official group label is
# too unwieldy to sit inside a device name (issue #33). With
# ``has_entity_name`` the device name is composed in front of every
# entity's friendly name, so a label like "Multiple MPPT Inverter
# Extension Model" would turn a simple DC current sensor into
# "bp 20.0 NX3 M2 Multiple MPPT Inverter Extension Model Module 0
# DC Current". Only models with genuinely broken or bloated official
# labels get an entry; everything else uses the spec label as-is so
# the device name matches what the options-flow multi-select shows.
#
# 201..204 have redundant labels ("Meter (Single Phase) single phase
# (AN or AB) meter") and 211..214 are their FLOAT twins with
# lowercase-only labels; a device never exposes both the int and the
# float variant of the same meter, so the identical short names
# cannot collide in practice.
DEVICE_MODEL_NAME_OVERRIDES: dict[int, str] = {
    160: "MPPT",
    201: "Meter (Single Phase)",
    202: "Meter (Split Phase)",
    203: "Meter (Three Phase Wye)",
    204: "Meter (Three Phase Delta)",
    211: "Meter (Single Phase)",
    212: "Meter (Split Phase)",
    213: "Meter (Three Phase Wye)",
    214: "Meter (Three Phase Delta)",
    601: "Tracker Controller",
    801: "Energy Storage",
}


def device_model_suffix(model_id: int | None, gdef: dict[str, Any]) -> str | None:
    """Return the model part of a device name, e.g. "Inverter (Three Phase)".

    Pure in-memory lookup on the already-loaded pysunspec2 group
    definition - unlike :func:`sunspec_model_label` this never touches
    the filesystem, so it is safe to call from an entity's
    ``device_info`` property inside the event loop.

    Resolution order: curated override, then the spec's group label
    (underscores normalised to spaces, model 122 is
    "Measurements_Status"), then the group name, then a generic
    "Model <id>" so the caller always gets something usable when the
    model id is known.
    """
    if model_id is not None and model_id in DEVICE_MODEL_NAME_OVERRIDES:
        return DEVICE_MODEL_NAME_OVERRIDES[model_id]
    label = gdef.get("label") or gdef.get("name")
    if label:
        cleaned = str(label).replace("_", " ").strip()
        # Several official labels carry a redundant " Model" tail
        # ("Irradiance Model", "Battery Base Model", ...) that adds
        # noise but no information inside a composed entity name.
        if cleaned.endswith(" Model") and len(cleaned) > len(" Model"):
            cleaned = cleaned[: -len(" Model")]
        return cleaned
    if model_id is not None:
        return f"Model {model_id}"
    return None


@lru_cache(maxsize=512)
def sunspec_model_label(model_id: int) -> str:
    """Return the human label for a SunSpec model ID, with the ID as suffix.

    Examples:

    >>> sunspec_model_label(103)
    'Inverter (Three Phase) (103)'
    >>> sunspec_model_label(160)
    'Multiple MPPT Inverter Extension Model (160)'
    >>> sunspec_model_label(99999)
    'Model 99999'
    """
    fallback = f"Model {model_id}"
    try:
        resource = files(_MODEL_PACKAGE).joinpath(f"model_{model_id}.json")
    except (ModuleNotFoundError, OSError) as exc:
        _LOGGER.debug("Could not locate model_%s.json: %s", model_id, exc)
        return fallback
    if not resource.is_file():
        return fallback
    try:
        with resource.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _LOGGER.debug("Could not parse model_%s.json: %s", model_id, exc)
        return fallback
    label = data.get("group", {}).get("label")
    if not label:
        return fallback
    return f"{label} ({model_id})"


def sunspec_model_labels(model_ids: list[int] | set[int]) -> dict[str, str]:
    """Bulk-resolve labels for a set of model IDs.

    Returns a dict suitable for ``cv.multi_select``: ``{id: label}``.
    Keys are strings because ``cv.multi_select`` coerces dict keys to
    ``str`` internally; using string keys here avoids a type mismatch
    between the option keys and the ``default`` list the form widget
    receives, which otherwise causes "X is not a valid option" errors
    in the HA frontend.
    The dict is sorted by model ID for stable rendering order in the
    UI.
    """
    return {str(mid): sunspec_model_label(mid) for mid in sorted(model_ids)}
