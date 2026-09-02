"""Tests for the inverter write path (model 123 immediate controls).

These deliberately drive **real** pysunspec2 model objects instead of
``Mock()``. The pre-existing write tests in ``test_diagnostics.py`` hand
the API a ``Mock`` client whose points accept any attribute assignment,
which is exactly why they stayed green while every scaled write on real
hardware raised ``ModelError: SF field WMaxLimPct_SF value not
initialized`` (reported in #17). A mock cannot reproduce a scale-factor
bug, because a mock has no scale factor.

``custom_components.sunspec2.pysunspec2.device.Model`` gives us the genuine point definitions and the
genuine ``Point.set_value(computed=True)`` encoding logic. The only two
things it lacks are ``Model.read()`` and ``Point.write()``, which the
Modbus client subclasses add, so the helper below grafts those on as the
seam where the wire would be.
"""

from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest

import custom_components.sunspec2.pysunspec2.device as sunspec_device
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.errors import TransientError
from custom_components.sunspec2.pysunspec2.device import ModelError
from custom_components.sunspec2.pysunspec2.modbus.client import SunSpecModbusClientError
from custom_components.sunspec2.pysunspec2.modbus.client import SunSpecModbusClientException
from custom_components.sunspec2.pysunspec2.modbus.client import SunSpecModbusClientTimeout
from custom_components.sunspec2.pysunspec2.modbus.client import SunSpecModbusValueError
from custom_components.sunspec2.pysunspec2.modbus.modbus import ModbusClientError
from custom_components.sunspec2.pysunspec2.modbus.modbus import ModbusClientException
from custom_components.sunspec2.pysunspec2.modbus.modbus import ModbusClientTimeout


def _api_with_model(hass, model_id, sf_on_read):
    """Return an API client wired to one real pysunspec2 model.

    ``sf_on_read`` maps point name to the value the simulated block read
    puts there. Leave a scale-factor point out of it to simulate a device
    that answers its ``*_SF`` register with 0x8000 ("not implemented"),
    which pysunspec2 turns back into ``None`` in ``Point.set_mb``.

    The returned ``calls`` dict counts ``model.read()`` invocations so a
    test can assert the write path refreshes the block before encoding.
    """
    model = sunspec_device.Model(model_id=model_id)
    calls = {"read": 0}

    def _read():
        calls["read"] += 1
        for name, value in sf_on_read.items():
            model.points[name].value = value

    async def _async_read():
        _read()

    model.read = _read
    model.async_read = _async_read
    # The flush is one model.async_write() for the whole batch as of
    # v0.20.0, not one point.write() per point. Both are stubbed so a
    # test can assert which one the code actually reaches for.
    model.async_write = AsyncMock()
    for point in model.points.values():
        point.write = Mock()

    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)
    api._client = Mock(models={model_id: [model]})
    return api, model, calls


async def test_write_reads_the_model_before_encoding(hass):
    """A scaled write must refresh the block so the scale factor exists.

    Regression test for #17. The coordinator closes the client at the end
    of every update cycle, so the client this write path resolves through
    is freshly scanned with ``full_model_read=False`` and every point but
    ID and L is ``None``. Without the ``model.read()`` in
    ``_write_points_blocking`` the ``cvalue`` assignment below raises.
    """
    api, model, calls = _api_with_model(hass, 123, {"WMaxLimPct_SF": -2})
    point = model.points["WMaxLimPct"]
    assert point.sf_value is None, "precondition: scale factor starts unread"

    await api._async_write_points(123, [("WMaxLimPct", 50)])

    assert calls["read"] == 1
    # 50 % at SF -2 encodes to raw 5000. Getting the raw register value
    # right is the whole point: a write that skipped the scaling would
    # send 50 and set the inverter to 0.5 %.
    assert point.value == 5000
    assert point.cvalue == 50.0
    model.async_write.assert_awaited_once_with()
    point.write.assert_not_called()


async def test_unread_scale_factor_raises_in_pysunspec2(hass):
    """Characterisation test: this is the failure the read() call prevents.

    Pinned against pysunspec2 directly rather than through our API, so if
    a future pysunspec2 release starts tolerating an unread scale factor
    this goes red and tells us the workaround can be reconsidered.
    """
    _api, model, _calls = _api_with_model(hass, 123, {})

    with pytest.raises(ModelError, match="SF field WMaxLimPct_SF value not initialized"):
        model.points["WMaxLimPct"].cvalue = 50


async def test_write_enum_point_needs_no_scale_factor(hass):
    """Switch-backed points write fine even with every SF still unread.

    Conn, WMaxLim_Ena and OutPFSet_Ena are enum16 with ``sf: null``, so
    ``sf_required`` is False and the encoding path never looks for a
    scale factor. This is why #17 reported working switches next to
    broken numbers, and it must keep working after the fix.
    """
    api, model, _calls = _api_with_model(hass, 123, {})
    point = model.points["WMaxLim_Ena"]
    assert point.sf_required is False

    await api._async_write_points(123, [("WMaxLim_Ena", 1)])

    assert point.value == 1
    model.async_write.assert_awaited_once_with()
    point.write.assert_not_called()


async def test_write_surfaces_unimplemented_scale_factor_as_device_error(hass):
    """A device that does not implement the SF must not leak a raw traceback.

    ``model.read()`` cannot rescue this case: the device answers the
    ``*_SF`` register with 0x8000, pysunspec2 nulls ``sf_value`` again in
    ``Point.set_mb``, and the same ``ModelError`` comes back. What must
    not happen is the entity layer seeing it, because ``ModelError`` is a
    plain ``Exception`` and its ``except SunSpecError`` would not match.
    """
    api, _model, _calls = _api_with_model(hass, 123, {})

    with pytest.raises(DeviceError, match="scale factor"):
        await api.async_write_points(123, [("WMaxLimPct", 50)])


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        # Transient: retrying later is the right move.
        (ModbusClientTimeout("data time out"), TransientError),
        (SunSpecModbusClientTimeout("timeout"), TransientError),
        # Device said no, or said something we cannot encode. All four of
        # these roots are unrelated classes in pysunspec2 and only the two
        # SunSpecModbusClient* ones were handled before v0.13.4.
        (ModbusClientException("Modbus exception: 2"), DeviceError),
        (ModbusClientError("Socket write error: [Errno 32] Broken pipe"), DeviceError),
        (SunSpecModbusClientException("boom"), DeviceError),
        (SunSpecModbusClientError("boom"), DeviceError),
        (SunSpecModbusValueError("value out of range"), DeviceError),
        (ModelError("SF field WMaxLimPct_SF value not initialized"), DeviceError),
    ],
)
async def test_write_translates_pysunspec2_exceptions(hass, raised, expected):
    """Every pysunspec2 exception root must arrive as a typed SunSpecError.

    Anything uncaught here reaches ``async_set_native_value`` in
    ``number.py`` / ``async_turn_on`` in ``switch.py``, whose
    ``except SunSpecError`` does not match, and Home Assistant renders a
    raw traceback in the UI instead of a readable error.
    """
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)

    with (
        patch.object(SunSpecApiClient, "_async_write_points", side_effect=raised),
        pytest.raises(expected),
    ):
        await api.async_write_points(123, [("WMaxLimPct", 50)])


async def test_write_does_not_swallow_our_own_errors(hass):
    """Typed errors raised inside the blocking path must pass through as-is.

    ``_write_points_blocking`` raises DeviceError itself for a missing
    model or point, and ``get_client()`` raises TransportError. The
    widened handler chain must not re-wrap those into a different
    category or a misleading scale-factor message.
    """
    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)
    api._client = Mock(models={1: ["common"]})

    with pytest.raises(DeviceError, match="Model 123 not present"):
        await api.async_write_points(123, [("WMaxLimPct", 50)])


async def test_batch_write_flushes_once_for_all_points(hass):
    """Setpoint and enable go out together, not as two separate writes.

    Borrowed from milanhin/pv_curtailment. Beyond saving round trips it
    closes a real gap: with two writes the inverter can act on the first
    before the second arrives, so the new percentage briefly applies
    while still disabled, or the old one applies once enabled.

    Also asserts the model is read exactly once. Writing point by point
    meant re-entering the whole resolve-and-read path per point, so a
    two-point write paid for two block reads.
    """
    api, model, calls = _api_with_model(hass, 123, {"WMaxLimPct_SF": 0})

    await api._async_write_points(123, [("WMaxLimPct", 50), ("WMaxLim_Ena", 1)])

    assert calls["read"] == 1
    model.async_write.assert_awaited_once_with()
    assert model.points["WMaxLimPct"].cvalue == 50
    assert model.points["WMaxLim_Ena"].value == 1
