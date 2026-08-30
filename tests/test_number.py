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
from custom_components.sunspec2.models import SunSpecModelWrapper

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
    """All model 123 Number entities register when the beta flag is on.

    MOCK_CONFIG_WRITE deliberately does NOT list 123 in its enabled
    models, which is the realistic case: 123 is not in DEFAULT_MODELS,
    so nobody has it ticked unless they went looking. Before v0.14.0
    the entities silently never appeared for those users (#17). The
    beta flag alone must be enough.
    """
    entry = await _setup_write_entry(hass)

    entities = _live_entities(hass, "number")

    assert len(entities) == 3
    points = {e._point_name for e in entities}
    assert points == {"WMaxLimPct", "WMaxLimPct_RvrtTms", "OutPFSet"}
    # The user's own selection stays untouched - only the separate
    # write filter pulled the control models in. It carries every
    # write-capable model, not just the ones this device has: the
    # coordinator intersects it with what the scan actually found.
    coordinator = entry.runtime_data
    assert coordinator.option_model_filter == {160}
    assert coordinator.write_model_filter == {123, 124, 704}


async def test_beta_off_does_not_poll_model_123(hass, sunspec_write_client_mock):
    """Without the beta flag we must not poll 123 behind the user's back."""
    entry = await _setup_write_entry(hass, beta=False)
    coordinator = entry.runtime_data

    assert coordinator.write_model_filter == set()
    assert 123 not in coordinator.data


async def test_model_123_control_points_do_not_become_sensors(hass, sunspec_write_client_mock):
    """The controls and the unmappable-unit points stay off the sensor platform.

    A sensor for a writable point is a read-only duplicate of a control
    the user can operate, and the two disagree during a write. The
    percentage points carry units HA has no mapping for ("% WMax",
    "cos()", "% VArMax", "% VArAval"), where sensor.py falls back to the
    raw SunSpec string as the native unit with state_class MEASUREMENT
    and starts a long-term statistics series the recorder can never
    convert or merge.
    """
    entry = await _setup_write_entry(hass)
    assert 123 in entry.runtime_data.data, "precondition: 123 really is polled"

    sensor_ids = [e.entity_id for e in _live_entities(hass, "sensor")]

    for excluded in (
        "wmaxlimpct_123",
        "outpfset_123",
        "wmaxlim_ena",
        "outpfset_ena",
        "varmaxpct",
        "varavalpct",
        "varwmaxpct",
    ):
        assert not any(eid.endswith(excluded) for eid in sensor_ids), excluded
    assert sensor_ids, "the other models still produce sensors"


async def test_model_123_timer_points_do_become_sensors(hass, sunspec_write_client_mock):
    """The Secs timers come back as sensors (v0.18.0, #17).

    @tisoft's KACO drops the export limit after WMaxLimPct_RvrtTms
    seconds while WMaxLim_Ena and WMaxLimPct keep reporting the old
    setpoint, so these are the only points that reveal a limit has
    lapsed. The model-wide exclusion took them out as collateral.
    """
    await _setup_write_entry(hass)

    sensor_ids = [e.entity_id for e in _live_entities(hass, "sensor")]

    assert any(eid.endswith("wmaxlimpct_rmptms") for eid in sensor_ids)
    assert any(eid.endswith("wmaxlimpct_wintms") for eid in sensor_ids)


async def test_revert_time_is_a_number_not_a_sensor(hass, sunspec_write_client_mock):
    """WMaxLimPct_RvrtTms is writable, so it is a control, not a reading."""
    await _setup_write_entry(hass)

    sensor_ids = [e.entity_id for e in _live_entities(hass, "sensor")]
    number_points = {e._point_name for e in _live_entities(hass, "number")}

    assert "WMaxLimPct_RvrtTms" in number_points
    assert not any(eid.endswith("wmaxlimpct_rvrttms") for eid in sensor_ids)


async def test_number_entities_absent_with_beta_off(hass, sunspec_write_client_mock):
    """No write entities without the opt-in, even though the device has model 123."""
    await _setup_write_entry(hass, beta=False)

    assert _live_entities(hass, "number") == []


async def test_export_limit_reports_the_device_value_above_100(hass, sunspec_write_client_mock):
    """A device shipping at 110 % must be readable, not clamped away.

    The fixture stores raw 11000 at SF -2, which is tisoft's KACO in
    #17: it shipped at 110 % and its owner could not put it back,
    because the entity was hardcoded to a 0..100 range and HA rejects
    an out-of-range service call outright.
    """
    await _setup_write_entry(hass)

    limit = next(e for e in _live_entities(hass, "number") if e._point_name == "WMaxLimPct")

    assert limit.native_value == 110.0
    assert limit.native_max_value >= 110


async def test_export_limit_step_follows_the_device_scale_factor(hass, sunspec_write_client_mock):
    """The UI step must be fine enough to express what the device stores.

    The fixture reports WMaxLimPct_SF as -2, so the register resolution
    is 0.01 %. A hardcoded step of 1 would make the device's own values
    unenterable in the frontend box whenever the scale factor is finer
    than whole percent.
    """
    await _setup_write_entry(hass)

    limit = next(e for e in _live_entities(hass, "number") if e._point_name == "WMaxLimPct")

    assert limit.native_step == 0.01


def test_step_falls_back_when_the_scale_factor_is_missing():
    """A device that does not implement its *_SF register gets the default.

    pysunspec2 turns the 0x8000 "not implemented" sentinel back into
    None, so the helper must not crash or produce a nonsense step.
    """
    from custom_components.sunspec2.number import _step_from_scale_factor

    class _Wrapper(SunSpecModelWrapper):
        def __init__(self, sf):
            super().__init__([])
            self._sf = sf

        def getMeta(self, name):
            return {"sf": "WMaxLimPct_SF"}

        def getValue(self, name, model_index=0):
            return self._sf

    assert _step_from_scale_factor(_Wrapper(None), "WMaxLimPct") == 1.0
    assert _step_from_scale_factor(_Wrapper(0), "WMaxLimPct") == 1.0
    assert _step_from_scale_factor(_Wrapper(-1), "WMaxLimPct") == 0.1
    # Never finer than the clamp, and never coarser than the old
    # hardcoded 1 even for a positive scale factor.
    assert _step_from_scale_factor(_Wrapper(-9), "WMaxLimPct") == 0.01
    assert _step_from_scale_factor(_Wrapper(2), "WMaxLimPct") == 1.0


async def test_set_value_holds_the_gateway_lock(hass, sunspec_write_client_mock):
    """The write must run while the per-gateway lock is held.

    Regression for the pre-v0.14.0 behaviour, where number.py called
    ``coordinator.api.async_write_points`` directly. Mid-cycle that
    shared one socket with ``read_model`` running in another executor
    thread, and pysunspec2 hardcodes the MBAP transaction id to 0, so
    neither side could tell whose response it had just read.
    """
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data
    limit = next(e for e in _live_entities(hass, "number") if e._point_name == "WMaxLimPct")

    observed = []

    async def _spy(model_id, points):
        for point_name, value in points:
            observed.append((model_id, point_name, value, coordinator._gateway_lock.locked()))

    with (
        patch.object(coordinator.api, "async_write_points", side_effect=_spy),
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
        patch.object(coordinator.api, "async_write_points"),
        patch.object(coordinator, "async_request_refresh", side_effect=_spy_refresh),
    ):
        await limit.async_set_native_value(80)

    assert locked_during_refresh == [False]


async def test_set_value_keeps_the_session_by_default(hass, sunspec_write_client_mock):
    """A write no longer throws the Modbus session away when it is done.

    Until v0.22.0 every write closed the session on the way out, for the
    same reason every poll did: give a single-slot inverter its slot
    back. Measured on the hardware that motivated it, that is what
    breaks it. Reconnecting per poll failed 5 of 6 cycles on a KACO
    Powador 7.8 TL3, one session held open served 20 of 20.

    ``async_request_refresh`` is patched out so this isolates the write
    path from the refresh that follows it.
    """
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data
    limit = next(e for e in _live_entities(hass, "number") if e._point_name == "WMaxLimPct")

    closes = []

    with (
        patch.object(coordinator.api, "async_write_points"),
        patch.object(coordinator, "async_request_refresh"),
        patch.object(
            coordinator.api,
            "close",
            side_effect=lambda *a, **kw: closes.append(coordinator._gateway_lock.locked()),
        ),
    ):
        await limit.async_set_native_value(80)

    assert closes == []


async def test_set_value_closes_the_session_when_sharing_the_slot(hass, sunspec_write_client_mock):
    """With the slot shared, the old contract still has to hold.

    Whoever opens a session under the lock closes it under the lock.
    Handing the lock to a queued waiter with our socket still open makes
    that waiter fail to connect, which surfaces as a bogus
    TransportError in an unrelated config entry.
    """
    entry = await _setup_write_entry(hass)
    coordinator = entry.runtime_data
    limit = next(e for e in _live_entities(hass, "number") if e._point_name == "WMaxLimPct")

    closes = []

    with (
        patch.object(
            type(coordinator),
            "release_slot_between_polls",
            property(lambda self: True),
        ),
        patch.object(coordinator.api, "async_write_points"),
        patch.object(coordinator, "async_request_refresh"),
        patch.object(
            coordinator.api,
            "close",
            side_effect=lambda *a, **kw: closes.append(coordinator._gateway_lock.locked()),
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
            "async_write_points",
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
