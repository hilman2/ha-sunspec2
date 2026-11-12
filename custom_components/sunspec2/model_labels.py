"""Human-readable labels for SunSpec models.

``sunspec_model_label`` reads the group label from the bundled
pysunspec2 model JSON, so the model multi-select of the config flow and
the options flow shows "Inverter (Three Phase) (103)" instead of "103".
Each file is read at most once per process. A missing or unparseable
file yields ``"Model <id>"`` instead of an error, so the form always
renders.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# The model definitions ship inside the integration, with the embedded
# pysunspec2 fork.
_MODEL_DIR = Path(__file__).parent / "pysunspec2" / "models" / "json"

# Short names for the SunSpec models whose official group label is too
# long for a device name (#33). With ``has_entity_name`` Home Assistant
# puts the device name in front of every entity name, so a label like
# "Multiple MPPT Inverter Extension Model" would turn a DC current
# sensor into "bp 20.0 NX3 M2 Multiple MPPT Inverter Extension Model
# Module 0 DC Current". A model without an entry keeps the label of the
# specification, so its device name matches what the options flow shows.
#
# 201..204 have redundant labels ("Meter (Single Phase) single phase
# (AN or AB) meter"), and 211..214 are their float twins with lowercase
# labels. A device never exposes the int and the float variant of the
# same meter, so the identical short names cannot collide.
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

    ``gdef`` is the pysunspec2 group definition, which is already in
    memory. Unlike :func:`sunspec_model_label` this function never
    touches the filesystem, so an entity's ``device_info`` property can
    call it inside the event loop.

    The result is the entry in ``DEVICE_MODEL_NAME_OVERRIDES``, else the
    group label of the specification with underscores turned into
    spaces (model 122 is "Measurements_Status"), else the group name,
    else "Model <id>". It is None only when ``gdef`` names nothing and
    ``model_id`` is None.
    """
    if model_id is not None and model_id in DEVICE_MODEL_NAME_OVERRIDES:
        return DEVICE_MODEL_NAME_OVERRIDES[model_id]
    label = gdef.get("label") or gdef.get("name")
    if label:
        cleaned = str(label).replace("_", " ").strip()
        # Several official labels end in a redundant " Model"
        # ("Irradiance Model", "Battery Base Model"), which is noise
        # inside a composed entity name.
        if cleaned.endswith(" Model") and len(cleaned) > len(" Model"):
            cleaned = cleaned[: -len(" Model")]
        return cleaned
    if model_id is not None:
        return f"Model {model_id}"
    return None


@lru_cache(maxsize=512)
def sunspec_model_label(model_id: int) -> str:
    """Return the label of a SunSpec model with the id as suffix, or "Model <id>".

    Examples:

    >>> sunspec_model_label(103)
    'Inverter (Three Phase) (103)'
    >>> sunspec_model_label(160)
    'Multiple MPPT Inverter Extension Model (160)'
    >>> sunspec_model_label(99999)
    'Model 99999'
    """
    fallback = f"Model {model_id}"
    resource = _MODEL_DIR / f"model_{model_id}.json"
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
    """Return ``{model id: label}`` for ``cv.multi_select``, ordered by model id.

    The keys are strings. ``cv.multi_select`` coerces dict keys to
    ``str``, and int keys would not match the ``default`` list the form
    receives, which the frontend reports as "X is not a valid option".
    """
    return {str(mid): sunspec_model_label(mid) for mid in sorted(model_ids)}
