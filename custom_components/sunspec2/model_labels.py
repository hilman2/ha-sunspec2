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

_LOGGER = logging.getLogger(__name__)

_MODEL_PACKAGE = "sunspec2.models.json"


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


def sunspec_model_labels(model_ids: list[int] | set[int]) -> dict[int, str]:
    """Bulk-resolve labels for a set of model IDs.

    Returns a dict suitable for ``cv.multi_select``: ``{id: label}``.
    The dict is sorted by model ID for stable rendering order in the
    UI.
    """
    return {mid: sunspec_model_label(mid) for mid in sorted(model_ids)}
