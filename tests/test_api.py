"""Tests for SunSpec api."""

import socket
from unittest.mock import Mock

import pytest
from sunspec2.modbus.client import SunSpecModbusClientError
from sunspec2.modbus.client import SunSpecModbusClientException
from sunspec2.modbus.client import SunSpecModbusClientTimeout
from sunspec2.modbus.modbus import ModbusClientError

from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.const import DEFAULT_SCAN_DELAY_SECONDS
from custom_components.sunspec2.const import MAX_SCAN_DELAY_SECONDS
from custom_components.sunspec2.const import MIN_SCAN_DELAY_SECONDS
from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.errors import ProtocolError
from custom_components.sunspec2.errors import TransientError
from custom_components.sunspec2.errors import TransportError


async def test_api(hass, sunspec_client_mock):
    """Test API calls."""

    # To test the api submodule, we first create an instance of our API client
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    models = await api.async_get_models()
    assert models == [
        1,
        103,
        160,
        304,
        701,
        702,
        703,
        704,
        705,
        706,
        707,
        708,
        709,
        710,
        711,
        712,
    ]

    device_info = await api.async_get_device_info()

    assert device_info.getValue("Mn") == "SunSpecTest"
    assert device_info.getValue("SN") == "sn-123456789"

    model = await api.async_get_data(701)
    assert model.getValue("W") == 9800
    assert model.getMeta("W")["label"] == "Active Power"

    model = await api.async_get_data(705)
    keys = model.getKeys()
    assert len(keys) == 22


async def test_get_client(hass, sunspec_modbus_client_mock):
    """Test API calls."""

    # To test the api submodule, we first create an instance of our API client
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    client = api.get_client()
    client.scan.assert_called_once()


async def test_modbus_connect(hass, sunspec_modbus_client_mock):
    """Test API calls."""

    # To test the api submodule, we first create an instance of our API client
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    client = api.get_client()
    client.scan.assert_called_once()


async def test_modbus_connect_fail(hass, mocker):
    mocker.patch(
        # api_call is from slow.py but imported to main.py
        "sunspec2.modbus.client.SunSpecModbusClientDeviceTCP.connect",
        return_value={},
    )
    mocker.patch(
        # api_call is from slow.py but imported to main.py
        "sunspec2.modbus.client.SunSpecModbusClientDeviceTCP.is_connected",
        return_value=False,
    )
    """Test API calls."""

    # To test the api submodule, we first create an instance of our API client
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    with pytest.raises(Exception):
        api.modbus_connect()


async def test_modbus_connect_exception(hass, mocker):
    mocker.patch(
        # api_call is from slow.py but imported to main.py
        "sunspec2.modbus.client.SunSpecModbusClientDeviceTCP.connect",
        side_effect=ModbusClientError,
    )
    mocker.patch(
        # api_call is from slow.py but imported to main.py
        "sunspec2.modbus.client.SunSpecModbusClientDeviceTCP.is_connected",
        return_value=False,
    )
    mocker.patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True)
    """Test API calls."""

    # To test the api submodule, we first create an instance of our API client
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    with pytest.raises(TransportError):
        api.modbus_connect()


async def test_read_model_timeout(hass, mocker):
    mocker.patch(
        "custom_components.sunspec2.api.SunSpecApiClient.read_model",
        side_effect=SunSpecModbusClientTimeout,
    )
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    with pytest.raises(TransientError):
        await api.async_get_data(1)


async def test_read_model_error(hass, mocker):
    mocker.patch(
        "custom_components.sunspec2.api.SunSpecApiClient.read_model",
        side_effect=SunSpecModbusClientException,
    )
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    with pytest.raises(DeviceError):
        await api.async_get_data(1)


# ---------------------------------------------------------------------------
# Connection teardown on the failure path (#25)
# ---------------------------------------------------------------------------


async def test_failed_scan_tears_down_the_connected_client(hass, mocker):
    """A client that connects and then fails its scan must not leak.

    This is the exact shape reported in #25: the TCP connect succeeds,
    the inverter resets the connection during the base-address walk, and
    scan() raises. Before the fix nothing could reach that client -
    get_client() only assigns self._client on success, so both close()
    and _force_disconnect() saw None and did nothing. On a single-slot
    inverter the abandoned session then occupies the only Modbus slot,
    which makes the failure sticky and makes the second attempt's error
    message describe our own leak instead of the original fault.
    """
    mocker.patch("sunspec2.modbus.client.SunSpecModbusClientDeviceTCP.connect", return_value=None)
    mocker.patch(
        "sunspec2.modbus.client.SunSpecModbusClientDeviceTCP.is_connected", return_value=True
    )
    mocker.patch(
        "sunspec2.modbus.client.SunSpecModbusClientDeviceTCP.scan",
        side_effect=SunSpecModbusClientError("Error scanning SunSpec base addresses"),
    )
    mocker.patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True)
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    torn_down = mocker.patch.object(SunSpecApiClient, "_force_disconnect")

    with pytest.raises(ProtocolError):
        api.modbus_connect()

    assert torn_down.call_count == 1
    # Called with the client explicitly, because self._client is still
    # None at this point and the argument-free form would be a no-op.
    assert torn_down.call_args.args and torn_down.call_args.args[0] is not None


async def test_successful_connect_does_not_tear_down(hass, mocker):
    """The teardown must not fire on the happy path."""
    mocker.patch("sunspec2.modbus.client.SunSpecModbusClientDeviceTCP.connect", return_value=None)
    mocker.patch(
        "sunspec2.modbus.client.SunSpecModbusClientDeviceTCP.is_connected", return_value=True
    )
    mocker.patch("sunspec2.modbus.client.SunSpecModbusClientDeviceTCP.scan", return_value=None)
    mocker.patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True)
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    torn_down = mocker.patch.object(SunSpecApiClient, "_force_disconnect")

    client = api.modbus_connect()

    assert client is not None
    torn_down.assert_not_called()


async def test_force_disconnect_accepts_an_explicit_client(hass):
    """The explicit-client form must work while self._client is None."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    orphan = Mock()

    api._force_disconnect(orphan)

    orphan.disconnect.assert_called_once_with()
    # The instance's own (absent) client is untouched.
    assert api._client is None


async def test_check_port_does_not_mutate_the_process_default_timeout(hass, mocker):
    """check_port must use a per-socket timeout, not the global default.

    socket.setdefaulttimeout() is process-wide, so our 3s leaked into
    every other integration in the same Home Assistant process that
    created a socket without setting its own timeout.
    """
    before = socket.getdefaulttimeout()
    fake_sock = Mock()
    fake_sock.connect_ex.return_value = 0
    mocker.patch("socket.socket", return_value=fake_sock)
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    assert api.check_port() is True

    fake_sock.settimeout.assert_called_once_with(3.0)
    assert socket.getdefaulttimeout() == before


# ---------- scan delay (#17) ------------------------------------------------


async def test_scan_delay_defaults_and_reaches_pysunspec2(hass, sunspec_modbus_client_mock):
    """The configured pacing is what scan() is actually called with.

    The coordinator rebuilds its client on every cycle, so scan() runs
    once per poll and pysunspec2 sleeps this long after every model it
    walks. A default that never reaches scan() would be invisible.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    client = api.get_client()

    assert api._scan_delay == DEFAULT_SCAN_DELAY_SECONDS
    assert client.scan.call_args.kwargs["delay"] == DEFAULT_SCAN_DELAY_SECONDS


async def test_scan_delay_is_configurable(hass, sunspec_modbus_client_mock):
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass, scan_delay=0.75)
    client = api.get_client()

    assert client.scan.call_args.kwargs["delay"] == 0.75


async def test_scan_delay_zero_passes_none(hass, sunspec_modbus_client_mock):
    """0 has to become None: pysunspec2 only skips the sleep on None."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass, scan_delay=0)
    client = api.get_client()

    assert client.scan.call_args.kwargs["delay"] is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (-1.0, MIN_SCAN_DELAY_SECONDS),
        (99.0, MAX_SCAN_DELAY_SECONDS),
        ("0.5", 0.5),
    ],
)
async def test_scan_delay_is_clamped(hass, given, expected):
    """A corrupted options save must not reach time.sleep() as-is.

    A negative delay raises ValueError deep inside pysunspec2's scan
    walk, where the error message says nothing about our options form.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass, scan_delay=given)

    assert api._scan_delay == expected
