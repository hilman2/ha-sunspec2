"""Test SunSpec config flow."""

from unittest.mock import patch

import pytest
import voluptuous_serialize
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2.const import CONF_ENABLED_MODELS
from custom_components.sunspec2.const import CONF_MAX_AC_POWER_KW
from custom_components.sunspec2.const import CONF_SCAN_INTERVAL
from custom_components.sunspec2.const import DOMAIN

from . import MockSunSpecDataUpdateCoordinator
from .const import MOCK_CONFIG
from .const import MOCK_CONFIG_STEP_1
from .const import MOCK_SETTINGS


# This fixture bypasses the actual setup of the integration
# since we only want to test the config flow. We test the
# actual functionality of the integration in other test modules.
@pytest.fixture(autouse=True)
def bypass_setup_fixture():
    """Prevent setup."""
    with (
        patch(
            "custom_components.sunspec2.async_setup",
            return_value=True,
        ),
        patch(
            "custom_components.sunspec2.async_setup_entry",
            return_value=True,
        ),
    ):
        yield


# Here we simiulate a successful config flow from the backend.
# Note that we use the `bypass_get_data` fixture here because
# we want the config flow validation to succeed during the test.
async def test_successful_config_flow(
    hass, bypass_get_data, enable_custom_integrations, sunspec_client_mock
):
    """Test a successful config flow."""
    # Initialize a config flow
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # Check that the config flow shows the user form as the first step
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    flow_id = result["flow_id"]
    # If a user were to enter `test_username` for username and `test_password`
    # for password, it would result in this function call
    result = await hass.config_entries.flow.async_configure(flow_id, user_input=MOCK_CONFIG_STEP_1)

    # Check that the config flow is complete and a new entry is created with
    # the input data
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(flow_id, user_input=MOCK_SETTINGS)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "test_host:123:1"
    assert result["data"] == MOCK_CONFIG
    assert result["result"]


# In this case, we want to simulate a failure during the config flow.
# We use the `error_on_get_data` mock instead of `bypass_get_data`
# (note the function parameters) to raise an Exception during
# validation of the input config.
async def test_failed_config_flow(
    hass, error_on_get_data, error_on_get_device_info, sunspec_client_mock
):
    """Test a failed config flow due to credential validation failure."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "device_error"}


async def test_timeout_config_flow(hass, timeout_on_get_device_info, sunspec_client_mock):
    """Test a failed config flow due to a timeout during validation."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "timeout"}


async def test_config_flow_without_serial_number(
    hass, device_info_without_serial, sunspec_client_mock
):
    """Test config flow falls back when the device does not expose SN."""
    with patch(
        "custom_components.sunspec2.SunSpecApiClient.async_get_device_info",
        return_value=device_info_without_serial,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"

        flow_id = result["flow_id"]
        result = await hass.config_entries.flow.async_configure(
            flow_id, user_input=MOCK_CONFIG_STEP_1
        )

        assert result["type"] == FlowResultType.FORM

        result = await hass.config_entries.flow.async_configure(flow_id, user_input=MOCK_SETTINGS)

        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == "test_host:123:1"


# Our config flow also has an options flow, so we must test it as well.
async def test_options_flow(hass, sunspec_client_mock):
    """Test an options flow."""
    # Create a new MockConfigEntry and add to HASS (we're bypassing config
    # flow entirely)
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    entry.add_to_hass(hass)

    coordinator = MockSunSpecDataUpdateCoordinator(hass, [1, 2])
    # api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    hass.data[DOMAIN] = {entry.entry_id: coordinator}

    # Initialize an options flow
    # await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    # Verify that the first options step is a user form
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "host_options"

    # Enter some fake data into the form
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )

    # Verify that the second options step is a user form
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "model_options"

    # Regression guard for the voluptuous_serialize crash: when the
    # frontend requests the options form, HA calls
    # voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)
    # to turn the schema into JSON. Plain callables (like the old
    # _optional_positive_float validator) blow up that call. A NumberSelector
    # serialises cleanly, so every field - including max_ac_power_kw -
    # must appear in the serialised output.
    serialised = voluptuous_serialize.convert(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    serialised_names = {field["name"] for field in serialised}
    assert CONF_MAX_AC_POWER_KW in serialised_names

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_ENABLED_MODELS: [1], CONF_SCAN_INTERVAL: 10}
    )

    # Verify that the flow finishes
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == ""

    # Verify that the options were updated
    # assert entry.options == {BINARY_SENSOR: True, SENSOR: False, SWITCH: True}


async def test_options_flow_rejects_empty_model_selection(hass, sunspec_client_mock):
    """An empty models_enabled save must be refused with an inline error.

    Regression for the v0.7.3 -> v0.7.5 bug where saving the options
    form with no models ticked silently persisted ``models_enabled: []``
    to disk and the next coordinator reload polled zero models, killing
    every sensor on the integration.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test_empty_models")
    entry.add_to_hass(hass)

    coordinator = MockSunSpecDataUpdateCoordinator(hass, [1, 2])
    hass.data[DOMAIN] = {entry.entry_id: coordinator}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "host_options"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )
    assert result["step_id"] == "model_options"

    # Submit with an explicitly empty models list. The form must come
    # back with an inline base error instead of creating an entry.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_ENABLED_MODELS: [], CONF_SCAN_INTERVAL: 10}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "model_options"
    assert result["errors"] == {"base": "no_models_selected"}


# Test faild connection in options flow
async def test_options_flow_connect_error(hass, sunspec_client_mock_connect_error):
    """Test the options flow when the coordinator is currently failing.

    Phase 4 changed the error-surface mechanism: the options form no
    longer probes the inverter (which used to race the coordinator's
    own TCP slot on KACO Powador). Instead it inspects
    coordinator.last_update_success and shows the connection warning
    on the host_options step if the coordinator is currently broken.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    entry.add_to_hass(hass)

    coordinator = MockSunSpecDataUpdateCoordinator(hass, [1, 2])
    # Simulate a broken coordinator: this is what triggers the error
    # surface in the new options-flow path.
    coordinator.last_update_success = False
    hass.data[DOMAIN] = {entry.entry_id: coordinator}

    # Initialize an options flow
    result = await hass.config_entries.options.async_init(entry.entry_id)

    # Verify that the first options step is a user form
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "host_options"

    # Enter some fake data into the form
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )

    # Verify that we return to host_options with the connection warning
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "host_options"
    assert result["errors"] == {"base": "connection"}
