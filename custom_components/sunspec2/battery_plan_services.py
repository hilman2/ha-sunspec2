"""Configure a battery plan through one Home Assistant action."""

from __future__ import annotations

import math
from datetime import time

import probatio
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.core import ServiceCall
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from . import SunSpecDataUpdateCoordinator
from .battery_plan import PlanDirection
from .const import DOMAIN

SERVICE_SET_BATTERY_PLAN = "set_battery_plan"


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


@callback
def async_register_plan_service(hass: HomeAssistant) -> None:
    """Register the plan action once; each call resolves its current config entry."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_BATTERY_PLAN):
        return

    async def async_set_battery_plan(call: ServiceCall) -> None:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise HomeAssistantError("The SunSpec config entry is not loaded")
        coordinator = entry.runtime_data
        if (
            not isinstance(coordinator, SunSpecDataUpdateCoordinator)
            or coordinator.battery_plan is None
        ):
            raise HomeAssistantError("This device does not support a battery plan")
        values = {key: value for key, value in call.data.items() if key != "config_entry_id"}
        if "direction" in values:
            values["direction"] = PlanDirection(values["direction"])
        await coordinator.battery_plan.async_configure(**values)

    # This action addresses a config entry instead of an entity target.
    # Require admin access so it cannot bypass HA's entity permissions.
    async_register_admin_service(
        hass, DOMAIN, SERVICE_SET_BATTERY_PLAN, async_set_battery_plan, schema=PLAN_SCHEMA
    )
