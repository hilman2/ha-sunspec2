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


async def _open_manual_step(hass):
    """Helper: walk past the user-step menu into the manual form.

    v0.8.1 turned the user step into a menu (Manual / Scan), so the
    classic config-flow tests now have to traverse one extra level
    before they can submit IP / port / unit_id. Returns the
    flow-result dict for the manual form so the caller can grab
    flow_id and submit MOCK_CONFIG_STEP_1.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "manual"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual"
    return result


# Here we simiulate a successful config flow from the backend.
# Note that we use the `bypass_get_data` fixture here because
# we want the config flow validation to succeed during the test.
async def test_successful_config_flow(
    hass, bypass_get_data, enable_custom_integrations, sunspec_client_mock
):
    """Test a successful config flow."""
    result = await _open_manual_step(hass)
    flow_id = result["flow_id"]
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
    result = await _open_manual_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "device_error"}


async def test_timeout_config_flow(hass, timeout_on_get_device_info, sunspec_client_mock):
    """Test a failed config flow due to a timeout during validation."""
    result = await _open_manual_step(hass)
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
        result = await _open_manual_step(hass)
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
    entry.runtime_data = coordinator

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
    entry.runtime_data = coordinator

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
    entry.runtime_data = coordinator

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


async def test_dhcp_discovery_pre_fills_host_in_manual_step(hass):
    """A DHCP-discovered inverter must land the user in the manual step
    with the discovered IP already filled in.

    The DHCP handler does not probe the device itself - probing would
    race against any other Modbus client on the network and we cannot
    silently steal the inverter's single TCP slot. Instead we trust
    the IEEE OUI list in manifest.json (it gives us "this MAC almost
    certainly belongs to a SunSpec-capable inverter vendor") and let
    the user confirm the rest in the manual step.
    """
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    discovery = DhcpServiceInfo(
        ip="192.168.42.17",
        hostname="solaredge-12345",
        macaddress="0027020a1b2c",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_DHCP},
        data=discovery,
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual"

    # The host field default must equal the discovered IP. Voluptuous
    # stores defaults as a callable on the marker, so we have to pull
    # them by walking the schema's marker dict.
    schema_markers = result["data_schema"].schema
    host_default = next(
        (marker.default() for marker in schema_markers if str(marker) == "host"),
        None,
    )
    assert host_default == "192.168.42.17"


async def test_dhcp_discovery_aborts_when_host_already_configured(hass):
    """A second DHCP discovery for an already-configured host must abort.

    Without this guard, every DHCP lease renewal would re-prompt the
    user with a fresh "discovered integration" tile that they would
    have to dismiss by hand. Once they've already set up the inverter
    we should leave them alone.
    """
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    existing = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG, "host": "192.168.42.17"},
        entry_id="already_configured",
    )
    existing.add_to_hass(hass)

    discovery = DhcpServiceInfo(
        ip="192.168.42.17",
        hostname="solaredge-12345",
        macaddress="0027020a1b2c",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_DHCP},
        data=discovery,
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_step_offers_manual_and_scan_menu(hass):
    """Adding the integration must show a Manual / Scan menu first.

    The user-step menu was introduced in v0.8.1 to make the network
    scan reachable as an explicit user choice without forcing a scan
    on every install. Two menu options must be present: ``manual``
    and ``scan``.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "user"
    assert set(result["menu_options"]) == {"manual", "scan"}


async def test_scan_step_picks_candidate_and_pre_fills_manual_step(hass):
    """The scan flow must end on the manual step with the picked IP filled in.

    This pins the full scan -> scan_results -> manual chain. The
    discovery helper is patched out so the test does not need to
    actually open any sockets - it just verifies that the cached
    candidate list is rendered as a picker, that picking one routes
    to the manual step, and that the host field on the manual step
    carries the chosen IP as its default.
    """
    from custom_components.sunspec2.discovery import SunSpecCandidate

    candidates = [
        SunSpecCandidate(ip="192.168.1.50", mac="00:27:02:aa:bb:cc", vendor_match=True),
        SunSpecCandidate(ip="192.168.1.99", mac=None, vendor_match=False),
    ]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    # Step 1: open the scan branch from the menu.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "scan"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "scan"

    # Step 2: submit the scan form, with the discovery helper patched
    # to return our two-candidate fixture instead of touching the LAN.
    with patch(
        "custom_components.sunspec2.config_flow.async_discover_sunspec_candidates",
        return_value=candidates,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"subnet": "192.168.1.0/24"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "scan_results"

    # Step 3: pick the vendor-matched candidate.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"host": "192.168.1.50"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual"
    schema_markers = result["data_schema"].schema
    host_default = next(
        (marker.default() for marker in schema_markers if str(marker) == "host"),
        None,
    )
    assert host_default == "192.168.1.50"


async def test_scan_step_no_candidates_returns_inline_error(hass):
    """An empty scan must show the form again with a no_candidates error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "scan"}
    )

    with patch(
        "custom_components.sunspec2.config_flow.async_discover_sunspec_candidates",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"subnet": "192.168.1.0/24"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "scan"
    assert result["errors"] == {"base": "no_candidates"}


async def test_scan_step_invalid_subnet_returns_inline_error(hass):
    """A bad subnet (raising ValueError in the helper) must surface inline."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "scan"}
    )

    with patch(
        "custom_components.sunspec2.config_flow.async_discover_sunspec_candidates",
        side_effect=ValueError("not a CIDR"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"subnet": "garbage"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "scan"
    assert result["errors"] == {"base": "invalid_subnet"}
