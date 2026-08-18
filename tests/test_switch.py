"""Tests for the experimental Switch platform and the unload contract.

First coverage for this platform, alongside tests/test_number.py.
"""

from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.sunspec2.const import CONF_WRITE_BETA_ENABLED
from custom_components.sunspec2.errors import DeviceError

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG_WRITE
from .test_number import _live_entities


async def _setup_write_entry(hass, beta=True):
    config_entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    if beta:
        hass.config_entries.async_update_entry(
            config_entry, options={CONF_WRITE_BETA_ENABLED: True}
        )
    return await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)


async def test_switch_entities_appear_with_beta_on(hass, sunspec_write_client_mock):
    """All three model 123 switches register when the beta flag is on."""
    await _setup_write_entry(hass)

    points = {e._point_name for e in _live_entities(hass, "switch")}

    assert points == {"WMaxLim_Ena", "OutPFSet_Ena", "Conn"}


async def test_switch_reads_state_from_the_device(hass, sunspec_write_client_mock):
    """Switch state mirrors the fixture: WMaxLim_Ena on, OutPFSet_Ena off."""
    switches = {e._point_name: e for e in _live_entities(hass, "switch")}
    await _setup_write_entry(hass)
    switches = {e._point_name: e for e in _live_entities(hass, "switch")}

    assert switches["WMaxLim_Ena"].is_on is True
    assert switches["OutPFSet_Ena"].is_on is False
    assert switches["Conn"].is_on is True


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

    async def _spy(model_id, point_name, value):
        observed.append((model_id, point_name, value, coordinator._gateway_lock.locked()))

    with (
        patch.object(coordinator.api, "async_write_point", side_effect=_spy),
        patch.object(coordinator, "async_request_refresh"),
    ):
        await switch.async_turn_on()

    assert observed == [(123, "OutPFSet_Ena", 1, True)]
    assert not coordinator._gateway_lock.locked()


async def test_turn_off_surfaces_write_errors(hass, sunspec_write_client_mock):
    """A rejected write reaches the UI as HomeAssistantError, not a traceback."""
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data
    switch = next(e for e in _live_entities(hass, "switch") if e._point_name == "Conn")

    with (
        patch.object(
            coordinator.api,
            "async_write_point",
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

    async def _spy(model_id, point_name, value):
        written.append((point_name, value))

    with (
        patch.object(coordinator.api, "async_write_point", side_effect=_spy),
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


async def test_turning_the_beta_off_unloads_the_write_platforms(hass, sunspec_write_client_mock):
    """Unticking the beta flag must actually remove the write entities.

    Before v0.14.0 ``async_unload_entry`` re-derived the platform list
    from ``entry.options``, but HA writes the new options BEFORE it
    dispatches the update listener. Unticking the flag therefore made
    the unload read False and unload only the sensor platform, leaving
    the Number and Switch entities bound to a coordinator that had
    already been replaced: available forever off its frozen data, and
    writing through its closed api on the next press.

    Asserting on the live platform entities rather than on
    ``hass.states``: HA leaves a restored placeholder state behind for
    every registry entry whose platform is unloaded, so the states stay
    visible as "unavailable" either way.
    """
    entry = await _setup_write_entry(hass)
    assert len(_live_entities(hass, "number")) == 2
    assert len(_live_entities(hass, "switch")) == 3
    first_coordinator = entry.runtime_data

    hass.config_entries.async_update_entry(entry, options={CONF_WRITE_BETA_ENABLED: False})
    await hass.async_block_till_done()

    assert _live_entities(hass, "number") == []
    assert _live_entities(hass, "switch") == []
    # The reload really happened, so this is not just a stale read.
    assert entry.runtime_data is not first_coordinator
    # Sensors survive the toggle - only the write platforms go.
    assert _live_entities(hass, "sensor")


async def test_beta_toggle_off_then_on_restores_the_entities(hass, sunspec_write_client_mock):
    """The unload fix must not make the platforms unrecoverable."""
    entry = await _setup_write_entry(hass)

    hass.config_entries.async_update_entry(entry, options={CONF_WRITE_BETA_ENABLED: False})
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(entry, options={CONF_WRITE_BETA_ENABLED: True})
    await hass.async_block_till_done()

    assert len(_live_entities(hass, "number")) == 2
    assert len(_live_entities(hass, "switch")) == 3
