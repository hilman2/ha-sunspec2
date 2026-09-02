"""Global fixtures for SunSpec integration."""

import logging
from typing import Any
from unittest.mock import Mock
from unittest.mock import PropertyMock
from unittest.mock import patch

import pytest

import custom_components.sunspec2.pysunspec2.file.client as modbus_client
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.errors import TransientError
from custom_components.sunspec2.errors import TransportError

pytest_plugins = "pytest_homeassistant_custom_component"
_LOGGER: logging.Logger = logging.getLogger(__package__)


class MockFileClientDeviceNotConnected(modbus_client.FileClientDevice):
    def is_connected(self):
        return False

    def connect(self):
        return True


class MockFileClientDevice(modbus_client.FileClientDevice):
    def is_connected(self):
        return True

    def scan(self, progress=None):
        print(progress)
        if progress is not None and not progress("Mock scan"):
            return None
        return super().scan()

    def connect(self):
        return True


# This fixture is used to prevent HomeAssistant from attempting to create and dismiss persistent
# notifications. These calls would fail without this fixture since the persistent_notification
# integration is never loaded during a test.
@pytest.fixture(name="skip_notifications", autouse=True)
def skip_notifications_fixture():
    """Skip notification calls."""
    with (
        patch("homeassistant.components.persistent_notification.async_create"),
        patch("homeassistant.components.persistent_notification.async_dismiss"),
    ):
        yield


@pytest.fixture(autouse=True)
def no_model_read_pacing():
    """Switch off the pause read_model puts before every model read.

    It paces real inverters; the file-backed test client answers at
    once. With it on, every test that sets the integration up waited
    about two seconds for nothing.
    """
    with patch("custom_components.sunspec2.api.MODEL_READ_PACING_SECONDS", 0):
        yield


@pytest.fixture(name="auto_enable_custom_integrations", autouse=True)
def auto_enable_custom_integrations(
    hass: Any,
    enable_custom_integrations: Any,  # noqa: F811
) -> None:
    """Enable custom integrations defined in the test dir."""


@pytest.fixture(autouse=True)
def clear_sunspec_client_cache():
    """No-op since Phase 4: each SunSpecApiClient owns its own client.

    Kept as an autouse fixture so any test that grew to depend on it as
    a synchronisation point still has the hook. The body is empty -
    cross-test isolation now comes for free from instance-scoped state.
    """
    yield


# This fixture, when used, will result in calls to async_get_data to return None. To have the call
# return a value, we would add the `return_value=<VALUE_TO_RETURN>` parameter to the patch call.
@pytest.fixture(name="bypass_get_device_info")
def bypass_get_device_info_fixture():
    """Skip calls to get data from API."""
    with patch("custom_components.sunspec2.SunSpecApiClient.async_get_device_info"):
        yield


# This fixture, when used, will result in calls to async_get_data to return None. To have the call
# return a value, we would add the `return_value=<VALUE_TO_RETURN>` parameter to the patch call.
@pytest.fixture(name="bypass_get_data")
def bypass_get_data_fixture():
    """Skip calls to get data from API."""
    with patch("custom_components.sunspec2.SunSpecApiClient.async_get_data"):
        yield


@pytest.fixture
def sunspec_client_mock():
    """Skip calls to get data from API."""
    client = MockFileClientDevice("./tests/test_data/inverter.json")
    client.scan()
    with (
        patch("custom_components.sunspec2.SunSpecApiClient.modbus_connect", return_value=client),
        patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True),
    ):
        yield


# In this fixture, we are forcing calls to async_get_data to raise an Exception. This is useful
# for exception handling.
@pytest.fixture
def sunspec_write_client_mock():
    """Device file exposing model 123, for the write platforms.

    Separate from ``sunspec_client_mock`` because inverter.json's
    model 103 has Evt1 bits set, which renders as a comma-joined
    bitfield string that fails HA's strict ENUM validation on any
    refresh after setup. The write tests drive refreshes constantly
    (every write triggers one), so they need a device file without
    that trap. Models here: 1 (device info), 160 (a harmless
    measurement model) and 123 with realistic scale factors -
    WMaxLimPct_SF -2 and a raw 11000, i.e. the 110 % a real KACO
    shipped with in #17.
    """
    client = MockFileClientDevice("./tests/test_data/inverter_writecontrols.json")
    client.scan()
    with (
        patch("custom_components.sunspec2.SunSpecApiClient.modbus_connect", return_value=client),
        patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True),
    ):
        yield client


@pytest.fixture
def sunspec_fronius_client_mock():
    """A Fronius GEN24 with a battery: model 1 says Fronius, 124 has WChaMax 5000.

    Model 160 carries the four modules a GEN24 with storage reports,
    two MPPT strings and the "ST CHA" / "ST DISCHA" battery channels,
    and 124 the scale factors the GEN24 uses (rates in 0.01 %).
    """
    client = MockFileClientDevice("./tests/test_data/inverter_fronius.json")
    client.scan()
    with (
        patch("custom_components.sunspec2.SunSpecApiClient.modbus_connect", return_value=client),
        patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True),
    ):
        yield client


@pytest.fixture(autouse=True)
def clear_gateway_locks():
    """Drop the class-level per-gateway locks between tests.

    ``SunSpecDataUpdateCoordinator._GATEWAY_LOCKS`` is keyed by
    ``(host, port)`` and lives for the whole process, so a lock created
    in one test's event loop would be handed to a coordinator running
    in the next test's loop. An uncontended ``asyncio.Lock.acquire``
    never touches its loop reference, so this only bites when two tests
    actually contend the same key - which the write tests do.
    """
    from custom_components.sunspec2 import SunSpecDataUpdateCoordinator

    SunSpecDataUpdateCoordinator._GATEWAY_LOCKS.clear()
    yield
    SunSpecDataUpdateCoordinator._GATEWAY_LOCKS.clear()


@pytest.fixture
def sunspec_client_mock_connect_error():
    """Simulate connection error when retrieving data from API."""
    client = MockFileClientDevice("./tests/test_data/inverter.json")
    with (
        patch("custom_components.sunspec2.SunSpecApiClient.modbus_connect", return_value=client),
        patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True),
        patch(
            "custom_components.sunspec2.SunSpecApiClient.async_get_models",
            side_effect=TransportError,
        ),
    ):
        yield


@pytest.fixture
def sunspec_client_mock_not_connected():
    """Skip calls to get data from API."""
    client = MockFileClientDeviceNotConnected("./tests/test_data/inverter.json")
    client.scan()
    with (
        patch("custom_components.sunspec2.SunSpecApiClient.modbus_connect", return_value=client),
        patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True),
    ):
        yield


@pytest.fixture(name="sunspec_modbus_client_mock")
def sunspec_modbus_client_mock():
    """Skip calls to get data from API."""
    mock = Mock()
    with (
        patch(
            "custom_components.sunspec2.pysunspec2.modbus.client.SunSpecModbusClientDeviceTCP",
            return_value=mock,
        ),
        patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True),
    ):
        yield


# In this fixture, we are forcing calls to async_get_data to raise an Exception. This is useful
# for exception handling.
@pytest.fixture(name="error_on_get_device_info")
def error_get_device_info_fixture():
    """Simulate error when retrieving data from API."""
    with (
        patch(
            "custom_components.sunspec2.SunSpecApiClient.async_get_device_info",
            side_effect=Exception,
        ),
        patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True),
    ):
        yield


@pytest.fixture(name="timeout_on_get_device_info")
def timeout_get_device_info_fixture():
    """Simulate timeout when retrieving data from API."""
    with (
        patch(
            "custom_components.sunspec2.SunSpecApiClient.async_get_device_info",
            side_effect=TransientError,
        ),
        patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True),
    ):
        yield


@pytest.fixture(name="device_info_without_serial")
def device_info_without_serial_fixture():
    """Return device info without an SN point."""
    device_info = Mock()

    def get_value(point_name, model_index=0):
        if point_name == "SN":
            raise KeyError(point_name)
        return None

    device_info.getValue.side_effect = get_value
    type(device_info).num_models = PropertyMock(return_value=1)
    yield device_info


# In this fixture, we are forcing calls to async_get_data to raise an Exception. This is useful
# for exception handling.
@pytest.fixture
def error_on_get_data():
    """Simulate error when retrieving data from API."""
    client = MockFileClientDevice("./tests/test_data/inverter.json")
    client.scan()
    with (
        patch("custom_components.sunspec2.SunSpecApiClient.modbus_connect", return_value=client),
        patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True),
        patch(
            "custom_components.sunspec2.SunSpecApiClient.async_get_data",
            side_effect=TransportError,
        ),
    ):
        yield


# In this fixture, we are forcing calls to async_get_data to raise an Exception. This is useful
# for exception handling.
@pytest.fixture
def timeout_error_on_get_data():
    """Simulate timeout error when retrieving data from API."""
    client = MockFileClientDevice("./tests/test_data/inverter.json")
    client.scan()
    with (
        patch("custom_components.sunspec2.SunSpecApiClient.get_client", return_value=client),
        patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True),
        patch(
            "custom_components.sunspec2.SunSpecApiClient.async_get_data",
            side_effect=TransientError,
        ),
    ):
        yield


# In this fixture, we are forcing calls to async_get_data to raise an Exception. This is useful
# for exception handling.
@pytest.fixture
def connect_error_on_get_data():
    """Simulate connection error when retrieving data from API."""
    client = MockFileClientDevice("./tests/test_data/inverter.json")
    client.scan()
    with (
        patch("custom_components.sunspec2.SunSpecApiClient.modbus_connect", return_value=client),
        patch("custom_components.sunspec2.SunSpecApiClient.check_port", return_value=True),
        patch(
            "custom_components.sunspec2.SunSpecApiClient.async_get_data",
            side_effect=TransportError,
        ),
    ):
        yield


@pytest.fixture
def overflow_error_dca():
    """Simulate overflow error for getValue from API."""

    def my_side_effect(*args, **kwargs):
        if args[0] == "DCA":
            raise OverflowError()
        # Pass-through for the common-block fields the SunSpecEntity
        # device_info reads: without these the device-name would be
        # the integer 1 (from the catch-all return) and HA would
        # build a different entity_id slug, making the assertion in
        # test_sensor_overflow_error miss the entity entirely.
        if args[0] in ("Md", "Mn", "Vr"):
            return "Test-1547-1" if args[0] == "Md" else "TestVendor"
        return 1

    with patch(
        "custom_components.sunspec2.models.SunSpecModelWrapper.getValue",
        side_effect=my_side_effect,
    ):
        yield
