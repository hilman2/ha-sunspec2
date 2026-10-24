"""Tests for the Switch platform and the unload contract.

First coverage for this platform, alongside tests/test_number.py.
"""

from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.sunspec2.errors import DeviceError

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG_WRITE
from .test_number import _live_entities


async def _setup_write_entry(hass):
    config_entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    return await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)


async def test_switch_entities_appear_by_default(hass, sunspec_write_client_mock):
    """The model 123 switches register without an option.

    ``Conn`` ships disabled: off disconnects the inverter from the grid.
    """
    await _setup_write_entry(hass)

    points = {e._point_name for e in _live_entities(hass, "switch")}

    assert points == {"WMaxLim_Ena", "OutPFSet_Ena"}


async def test_switch_reads_state_from_the_device(hass, sunspec_write_client_mock):
    """Switch state mirrors the fixture: WMaxLim_Ena on, OutPFSet_Ena off."""
    switches = {e._point_name: e for e in _live_entities(hass, "switch")}
    await _setup_write_entry(hass)
    switches = {e._point_name: e for e in _live_entities(hass, "switch")}

    assert switches["WMaxLim_Ena"].is_on is True
    assert switches["OutPFSet_Ena"].is_on is False


async def test_turn_on_holds_the_gateway_lock(hass, sunspec_write_client_mock):
    """Switch writes take the same lock the Number platform does.

    #17 reported working switches next to broken numbers because
    enum16 points need no scale factor. That difference must not extend
    to the locking: both platforms share the socket with the poll cycle.
    """
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data
    switch = next(e for e in _live_entities(hass, "switch") if e._point_name == "OutPFSet_Ena")

    observed = []

    async def _spy(model_id, points):
        for point_name, value in points:
            observed.append((model_id, point_name, value, coordinator._gateway_lock.locked()))

    with (
        patch.object(coordinator.api, "async_write_points", side_effect=_spy),
        patch.object(coordinator, "async_request_refresh"),
    ):
        await switch.async_turn_on()

    assert observed == [(123, "OutPFSet_Ena", 1, True)]
    assert not coordinator._gateway_lock.locked()


async def test_turn_off_surfaces_write_errors(hass, sunspec_write_client_mock):
    """A rejected write reaches the UI as HomeAssistantError, not a traceback."""
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data
    switch = next(e for e in _live_entities(hass, "switch") if e._point_name == "OutPFSet_Ena")

    with (
        patch.object(
            coordinator.api,
            "async_write_points",
            side_effect=DeviceError("model 123 is read only on this firmware"),
        ),
        pytest.raises(HomeAssistantError, match="read only"),
    ):
        await switch.async_turn_off()

    assert not coordinator._gateway_lock.locked()


async def test_set_export_limit_service_uses_one_lock_hold(hass, sunspec_write_client_mock):
    """Setting percentage and enable flag is one operation, so one lock hold.

    With two acquisitions a poll cycle or a competing service call can
    slip between them and leave the inverter running with the new
    percentage and the old enable state.
    """
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data

    acquisitions = []
    original_acquire = coordinator._gateway_lock.acquire

    async def _counting_acquire():
        acquisitions.append(True)
        return await original_acquire()

    written = []

    async def _spy(model_id, points):
        written.extend(points)

    with (
        patch.object(coordinator.api, "async_write_points", side_effect=_spy),
        patch.object(coordinator, "async_request_refresh"),
        patch.object(coordinator._gateway_lock, "acquire", side_effect=_counting_acquire),
    ):
        await hass.services.async_call(
            "sunspec2",
            "set_export_limit",
            {"config_entry_id": entry.entry_id, "percent": 70, "enable": True},
            blocking=True,
        )

    assert written == [("WMaxLimPct", 70), ("WMaxLim_Ena", 1)]
    assert len(acquisitions) == 1


async def test_options_reload_preserves_control_entity_ids(hass, sunspec_write_client_mock):
    entry = await _setup_write_entry(hass)
    before = {e.entity_id for domain in ("number", "switch") for e in _live_entities(hass, domain)}
    first_coordinator = entry.runtime_data

    hass.config_entries.async_update_entry(entry, options={"prefix": "Garage"})
    await hass.async_block_till_done()

    after = {e.entity_id for domain in ("number", "switch") for e in _live_entities(hass, domain)}
    assert before == after
    assert entry.runtime_data is not first_coordinator
    assert all(
        e.coordinator is entry.runtime_data
        for domain in ("number", "switch")
        for e in _live_entities(hass, domain)
    )


async def test_unload_removes_all_write_entities(hass, sunspec_write_client_mock):
    entry = await _setup_write_entry(hass)
    assert len(_live_entities(hass, "number")) == 3
    assert len(_live_entities(hass, "switch")) == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert _live_entities(hass, "number") == []
    assert _live_entities(hass, "switch") == []
