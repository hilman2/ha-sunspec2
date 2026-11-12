"""The actions of the integration: ``set_export_limit`` and ``set_battery_plan``.

Both name a config entry in ``config_entry_id`` instead of targeting an
entity, so they belong to the integration and are registered once.
"""

from __future__ import annotations

import math
from datetime import time
from typing import Any
from typing import cast

import probatio
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.core import ServiceCall
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .battery_plan import PlanDirection
from .const import DOMAIN
from .coordinator import SunSpecDataUpdateCoordinator
from .errors import SunSpecError
from .errors import user_error
from .write_controls import EXPORT_LIMIT_HARD_MAX_PCT
from .write_controls import export_limit_points

SERVICE_SET_EXPORT_LIMIT = "set_export_limit"
SERVICE_SET_BATTERY_PLAN = "set_battery_plan"

# Fields the action does not know are let through and ignored. The
# action was released without a schema, so an automation with a stray
# field worked, and one that sets an export limit must not stop working
# over an update.
EXPORT_LIMIT_SCHEMA = probatio.Schema(
    {
        probatio.Required("config_entry_id"): cv.string,
        probatio.Required("percent"): probatio.All(
            probatio.Coerce(float), probatio.Range(min=0, max=EXPORT_LIMIT_HARD_MAX_PCT)
        ),
        probatio.Optional("enable", default=True): cv.boolean,
    },
    extra=probatio.ALLOW_EXTRA,
)


def _finite(value: float) -> float:
    """Reject NaN and infinities before a value reaches the power calculation."""
    if not math.isfinite(value):
        raise probatio.Invalid("Expected a finite number")
    return value


def _plan_time(value: object) -> time:
    """Accept local times with minute precision, matching the daily plan's triggers."""
    result: time = cv.time(value)
    if result.tzinfo is not None or result.second or result.microsecond:
        raise probatio.Invalid("Expected a local time with minute precision")
    return result


PLAN_SCHEMA = probatio.Schema(
    {
        probatio.Required("config_entry_id"): cv.string,
        probatio.Optional("direction"): probatio.In(
            [direction.value for direction in PlanDirection]
        ),
        probatio.Optional("start"): _plan_time,
        probatio.Optional("end"): _plan_time,
        probatio.Optional("target_pct"): probatio.All(
            probatio.Coerce(float), _finite, probatio.Range(min=0, max=100)
        ),
        probatio.Optional("capacity_kwh"): probatio.All(
            probatio.Coerce(float), _finite, probatio.Range(min=0.1, max=1000)
        ),
        probatio.Optional("enabled"): cv.boolean,
    }
)


def _coordinator(call: ServiceCall) -> SunSpecDataUpdateCoordinator:
    """The coordinator of the entry a call names in ``config_entry_id``.

    Raises:
        HomeAssistantError: The entry does not exist, belongs to another
            integration or is not loaded.
    """
    entry = call.hass.config_entries.async_get_entry(call.data["config_entry_id"])
    # The state is what says "loaded". Home Assistant deletes
    # runtime_data on unload, so reading it from an entry that is not
    # loaded raises AttributeError.
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        raise user_error("entry_not_loaded", entry_id=call.data["config_entry_id"])
    coordinator: SunSpecDataUpdateCoordinator = entry.runtime_data
    return coordinator


async def _async_set_export_limit(call: ServiceCall) -> None:
    coordinator = _coordinator(call)
    # Resolve the model the same way the entities do, so the action and
    # the UI cannot end up driving different registers on a device that
    # exposes both 123 and 704.
    target = export_limit_points(coordinator.detected_models)
    if target is None:
        raise user_error("no_export_limit")
    model_id, percent_point, enable_point = target
    points: list[tuple[str, object]] = [(percent_point, call.data["percent"])]
    if call.data["enable"]:
        # One write, so one hold of the gateway lock. With two, a poll
        # or another write can run in between and leave the inverter
        # with the new percentage and the old enable state.
        points.append((enable_point, 1))
    try:
        await coordinator.async_write_points_locked(model_id, points)
    except SunSpecError as exc:
        raise user_error("export_limit_failed", error=exc) from exc
    await coordinator.async_request_refresh()


async def _async_set_battery_plan(call: ServiceCall) -> None:
    planner = _coordinator(call).battery_plan
    if planner is None:
        raise user_error("no_battery_plan")
    values = {key: value for key, value in call.data.items() if key != "config_entry_id"}
    if "direction" in values:
        values["direction"] = PlanDirection(values["direction"])
    await planner.async_configure(**values)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register both actions. Called once, from ``async_setup``."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_EXPORT_LIMIT,
        _async_set_export_limit,
        schema=cast(Any, EXPORT_LIMIT_SCHEMA),
    )
    # set_battery_plan has no entity target that Home Assistant could
    # check permissions on, so it takes an administrator.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_SET_BATTERY_PLAN,
        _async_set_battery_plan,
        schema=cast(Any, PLAN_SCHEMA),
    )
