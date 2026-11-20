"""Run the blueprints of this repository in Home Assistant's automation engine.

The blueprints live in ``blueprints/automation/sunspec2`` and are not
part of the integration: a user imports them by URL. The tests load
them from the working tree, turn each into an automation with
``use_blueprint`` and watch which actions it calls.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.blueprint import models
from homeassistant.core import HomeAssistant
from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util import yaml as yaml_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.sunspec2.const import DOMAIN

REPOSITORY = pathlib.Path(__file__).parent.parent
BLUEPRINT_FOLDER = REPOSITORY / "blueprints" / "automation"
BLUEPRINT_NAMES = sorted(path.stem for path in (BLUEPRINT_FOLDER / DOMAIN).glob("*.yaml"))

ENTRY_ID = "blueprint_test_entry"


@pytest.fixture
async def repository_blueprints(hass: HomeAssistant) -> AsyncIterator[None]:
    """Make Home Assistant read blueprints from the working tree, not from its config folder."""

    def load(domain_blueprints: models.DomainBlueprints, blueprint_path: str) -> models.Blueprint:
        return models.Blueprint(
            yaml_util.load_yaml_dict(BLUEPRINT_FOLDER / blueprint_path),
            expected_domain=domain_blueprints.domain,
            path=blueprint_path,
            schema=domain_blueprints._blueprint_schema,
        )

    with patch(
        "homeassistant.components.blueprint.models.DomainBlueprints._load_blueprint",
        autospec=True,
        side_effect=load,
    ):
        yield
    # Home Assistant stops at the end of a test without unloading its
    # automations, and a template trigger that reads now() leaves a
    # timer behind that fails the test. A disabled automation has
    # detached its triggers.
    automations = hass.states.async_entity_ids("automation")
    if automations:
        await hass.services.async_call(
            "automation", "turn_off", {"entity_id": automations}, blocking=True
        )


async def start_automation(hass: HomeAssistant, name: str, inputs: dict[str, Any]) -> str:
    """Create an automation from the blueprint ``name`` and return its entity id."""
    # The blueprints report a problem with a notification in Home
    # Assistant, which a real installation always has. A test that
    # wants to read it registers a RecordedAction before this call.
    if not hass.services.has_service("persistent_notification", "create"):
        assert await async_setup_component(hass, "persistent_notification", {})
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": name,
                "alias": name,
                "use_blueprint": {"path": f"{DOMAIN}/{name}.yaml", "input": inputs},
            }
        },
    )
    await hass.async_block_till_done()
    entity_id = f"automation.{name}"
    state = hass.states.get(entity_id)
    # A blueprint or an input Home Assistant rejects leaves the
    # automation unavailable instead of raising.
    assert state is not None and state.state == "on", state
    return entity_id


def sunspec_entity(hass: HomeAssistant, entity_id: str, state: str, **attributes: Any) -> None:
    """Give ``entity_id`` a state and tie it to a config entry of the integration.

    The blueprints find the config entry of an entity with the template
    function ``config_entry_id``, which reads the entity registry.
    """
    entry = hass.config_entries.async_get_entry(ENTRY_ID)
    if entry is None:
        entry = MockConfigEntry(domain=DOMAIN, entry_id=ENTRY_ID)
        entry.add_to_hass(hass)
    domain, object_id = entity_id.split(".")
    er.async_get(hass).async_get_or_create(
        domain, DOMAIN, object_id, config_entry=entry, suggested_object_id=object_id
    )
    hass.states.async_set(entity_id, state, attributes)


class RecordedAction:
    """An action that records its calls and fails the first ``failures`` of them."""

    def __init__(self, hass: HomeAssistant, domain: str, action: str, failures: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failures = failures
        hass.services.async_register(domain, action, self._handle)

    async def _handle(self, call: ServiceCall) -> None:
        self.calls.append(dict(call.data))
        if self.failures > 0:
            self.failures -= 1
            raise HomeAssistantError("The inverter did not answer")


async def settle(hass: HomeAssistant) -> None:
    """Let the automations run as far as they get without waiting for one to finish.

    ``hass.async_block_till_done`` waits for every running automation,
    so it sits out a ``delay`` in real time. This returns while the
    automation is still in its delay.
    """
    for _ in range(20):
        await asyncio.sleep(0)


async def set_state(
    hass: HomeAssistant, entity_id: str, state: str, block: bool = True, **attributes: Any
) -> None:
    """Change a state, keeping the attributes it had, and let the automations run.

    Args:
        block: False for a change that leaves an automation in a delay,
            see ``settle``.
    """
    old = hass.states.get(entity_id)
    hass.states.async_set(entity_id, state, {**(old.attributes if old else {}), **attributes})
    await (hass.async_block_till_done() if block else settle(hass))


async def pass_time(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, block: bool = True, **delta: float
) -> None:
    """Move the clock forward and let everything that became due run.

    Args:
        block: False when an automation is in a delay afterwards, see ``settle``.
        delta: The step, as the keyword arguments of ``timedelta``.
    """
    freezer.tick(timedelta(**delta))
    async_fire_time_changed(hass, dt_util.utcnow())
    await (hass.async_block_till_done() if block else settle(hass))
