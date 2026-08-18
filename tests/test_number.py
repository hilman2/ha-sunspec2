"""Tests for the experimental Number platform and the write locking.

First coverage for this platform. It has shipped since v0.12.0 with
none, which is how the scale-factor bug in #17 reached real hardware
and how the missing gateway lock survived three releases.
"""

from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.sunspec2.const import CONF_WRITE_BETA_ENABLED
from custom_components.sunspec2.errors import DeviceError

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG_WRITE


def _live_entities(hass, domain):
    """Entities the platform currently holds, not the state machine.

    ``hass.states`` is the wrong thing to assert on after an unload:
    HA leaves a restored placeholder state behind for every registry
    entry whose platform went away, so ``states.async_all("number")``
    still returns the entities with ``state == "unavailable"`` and
    ``attributes["restored"] is True``.
    """
    component = hass.data.get("entity_components", {}).get(domain)
    return list(component.entities) if component else []


async def _setup_write_entry(hass, beta=True):
    config_entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    if beta:
        hass.config_entries.async_update_entry(
            config_entry, options={CONF_WRITE_BETA_ENABLED: True}
        )
    return await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)


async def test_number_entities_appear_with_beta_on(hass, sunspec_write_client_mock):
    """Both model 123 Number entities register when the beta flag is on.

    MOCK_CONFIG_WRITE deliberately does NOT list 123 in its enabled
    models, which is the realistic case: 123 is not in DEFAULT_MODELS,
    so nobody has it ticked unless they went looking. Before v0.14.0
    the entities silently never appeared for those users (#17). The
    beta flag alone must be enough.
    """
    entry = await _setup_write_entry(hass)

    entities = _live_entities(hass, "number")

    assert len(entities) == 2
    points = {e._point_name for e in entities}
    assert points == {"WMaxLimPct", "OutPFSet"}
    # The user's own selection stays untouched - only the separate
    # write filter pulled 123 in.
    coordinator = entry.runtime_data
    assert coordinator.option_model_filter == {160}
    assert coordinator.write_model_filter == {123}


async def test_beta_off_does_not_poll_model_123(hass, sunspec_write_client_mock):
    """Without the beta flag we must not poll 123 behind the user's back."""
    entry = await _setup_write_entry(hass, beta=False)
    coordinator = entry.runtime_data

    assert coordinator.write_model_filter == set()
    assert 123 not in coordinator.data


async def test_model_123_does_not_become_sensors(hass, sunspec_write_client_mock):
    """Polling 123 must not spawn 21 read-only sensors.

    Five of its points carry units HA has no mapping for ("% WMax",
    "cos()", "% VArMax", "% VArAval"). sensor.py would fall back to the
    raw SunSpec string as the native unit with state_class MEASUREMENT,
    starting a long-term statistics series under a unit the recorder
    can never convert or merge. The writable points are Number and
    Switch entities anyway.
    """
    entry = await _setup_write_entry(hass)
    assert 123 in entry.runtime_data.data, "precondition: 123 really is polled"

    sensor_ids = [e.entity_id for e in _live_entities(hass, "sensor")]

    assert not any("wmaxlimpct" in eid or "outpfset" in eid for eid in sensor_ids)
    assert sensor_ids, "the other models still produce sensors"


async def test_number_entities_absent_with_beta_off(hass, sunspec_write_client_mock):
    """No write entities without the opt-in, even though the device has model 123."""
    await _setup_write_entry(hass, beta=False)

    assert _live_entities(hass, "number") == []


async def test_export_limit_reports_the_device_value_above_100(hass, sunspec_write_client_mock):
    """A device shipping at 110 % must be readable, not clamped away.

    The fixture stores raw 11000 at SF -2, which is tisoft's KACO in
    #17. Reading it works; writing it back is what the hardcoded
    0..100 ceiling blocks, and that is a separate change.
    """
    await _setup_write_entry(hass)

    limit = next(e for e in _live_entities(hass, "number") if e._point_name == "WMaxLimPct")

    assert limit.native_value == 110.0


async def test_set_value_holds_the_gateway_lock(hass, sunspec_write_client_mock):
    """The write must run while the per-gateway lock is held.

    Regression for the pre-v0.14.0 behaviour, where number.py called
    ``coordinator.api.async_write_point`` directly. Mid-cycle that
    shared one socket with ``read_model`` running in another executor
    thread, and pysunspec2 hardcodes the MBAP transaction id to 0, so
    neither side could tell whose response it had just read.
    """
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data
    limit = next(e for e in _live_entities(hass, "number") if e._point_name == "WMaxLimPct")

    observed = []

    async def _spy(model_id, point_name, value):
        observed.append((model_id, point_name, value, coordinator._gateway_lock.locked()))

    with (
        patch.object(coordinator.api, "async_write_point", side_effect=_spy),
        patch.object(coordinator, "async_request_refresh"),
    ):
        await limit.async_set_native_value(80)

    assert observed == [(123, "WMaxLimPct", 80, True)]
    # And the lock is handed back afterwards, whatever happened.
    assert not coordinator._gateway_lock.locked()


async def test_set_value_refreshes_outside_the_lock(hass, sunspec_write_client_mock):
    """The follow-up refresh must not run while we still hold the lock.

    ``asyncio.Lock`` is not reentrant and the coordinator's refresh
    debouncer runs the refresh inline, so a refresh requested under the
    lock deadlocks with no timeout and no error.
    """
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data
    limit = next(e for e in _live_entities(hass, "number") if e._point_name == "WMaxLimPct")

    locked_during_refresh = []

    async def _spy_refresh():
        locked_during_refresh.append(coordinator._gateway_lock.locked())

    with (
        patch.object(coordinator.api, "async_write_point"),
        patch.object(coordinator, "async_request_refresh", side_effect=_spy_refresh),
    ):
        await limit.async_set_native_value(80)

    assert locked_during_refresh == [False]


async def test_set_value_closes_the_session(hass, sunspec_write_client_mock):
    """The write closes its Modbus session before releasing the lock.

    Whoever opens a session under the lock closes it under the lock.
    Handing the lock to a queued waiter with our socket still open
    makes that waiter fail to connect, which surfaces as a bogus
    TransportError in an unrelated config entry.

    ``async_request_refresh`` is patched out so the count isolates the
    write path: the refresh runs a full update cycle, which closes the
    client again at the end and would make this assert 2.
    """
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data
    limit = next(e for e in _live_entities(hass, "number") if e._point_name == "WMaxLimPct")

    closes = []

    with (
        patch.object(coordinator.api, "async_write_point"),
        patch.object(coordinator, "async_request_refresh"),
        patch.object(
            coordinator.api,
            "close",
            side_effect=lambda: closes.append(coordinator._gateway_lock.locked()),
        ),
    ):
        await limit.async_set_native_value(80)

    assert closes == [True]


async def test_set_value_releases_the_lock_when_the_write_fails(hass, sunspec_write_client_mock):
    """A failed write must not leave the gateway locked forever."""
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data
    limit = next(e for e in _live_entities(hass, "number") if e._point_name == "WMaxLimPct")

    with (
        patch.object(
            coordinator.api,
            "async_write_point",
            side_effect=DeviceError("inverter said no"),
        ),
        pytest.raises(HomeAssistantError, match="inverter said no"),
    ):
        await limit.async_set_native_value(80)

    assert not coordinator._gateway_lock.locked()


async def test_set_value_times_out_when_the_gateway_stays_busy(hass, sunspec_write_client_mock):
    """A wedged poll cycle must surface as an error, not an infinite wait.

    The timeout constant is patched in the ``__init__`` module
    namespace, not in ``const``: ``__init__.py`` does ``from .const
    import WRITE_LOCK_TIMEOUT_SECONDS``, so the value is bound at
    import time and patching the const module would leave the
    coordinator reading the real 120 and hang the test for two minutes.
    """
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data
    limit = next(e for e in _live_entities(hass, "number") if e._point_name == "WMaxLimPct")

    await coordinator._gateway_lock.acquire()
    try:
        with (
            patch("custom_components.sunspec2.WRITE_LOCK_TIMEOUT_SECONDS", 0.05),
            pytest.raises(HomeAssistantError, match="Timed out"),
        ):
            await limit.async_set_native_value(80)
    finally:
        coordinator._gateway_lock.release()
