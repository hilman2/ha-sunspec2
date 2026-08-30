"""Test SunSpec config flow."""

from unittest.mock import patch

import pytest
import voluptuous_serialize
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2.const import CONF_BAUDRATE
from custom_components.sunspec2.const import CONF_ENABLED_MODELS
from custom_components.sunspec2.const import CONF_HOST
from custom_components.sunspec2.const import CONF_MAX_AC_POWER_KW
from custom_components.sunspec2.const import CONF_PARITY
from custom_components.sunspec2.const import CONF_PORT
from custom_components.sunspec2.const import CONF_PREFIX
from custom_components.sunspec2.const import CONF_SCAN_INTERVAL
from custom_components.sunspec2.const import CONF_SERIAL_PORT
from custom_components.sunspec2.const import CONF_STANDBY_WHEN_IDLE
from custom_components.sunspec2.const import CONF_TRANSPORT
from custom_components.sunspec2.const import CONF_UNIT_ID
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.const import MIN_SCAN_INTERVAL_SECONDS
from custom_components.sunspec2.const import PARITY_NONE
from custom_components.sunspec2.const import TRANSPORT_RTU

from . import MockSunSpecDataUpdateCoordinator
from .const import MOCK_CONFIG
from .const import MOCK_CONFIG_RTU
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
    serialised_names = {field["name"] for field in serialised}  # type: ignore[index]
    assert CONF_MAX_AC_POWER_KW in serialised_names

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_ENABLED_MODELS: ["1"], CONF_SCAN_INTERVAL: 10}
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


async def test_user_step_offers_manual_scan_and_serial_menu(hass):
    """Adding the integration must show a Manual / Scan / Serial menu first.

    The user-step menu was introduced in v0.8.1 to make the network
    scan reachable as an explicit user choice without forcing a scan
    on every install. v0.11.0 added a third "serial" option for
    Modbus RTU. All three options must be present.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "user"
    assert set(result["menu_options"]) == {"manual", "scan", "serial"}


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


async def test_setup_settings_step_pre_selects_default_models(
    hass, bypass_get_data, sunspec_client_mock
):
    """The settings-step model multi-select must arrive with DEFAULT_MODELS pre-ticked.

    Regression for v0.9.0 and earlier where the default was a Python
    set: HA's frontend serialises ``vol.Optional`` defaults straight
    to JSON and a set does not survive that round-trip in a way the
    multi-select can match against its option keys, so the field
    rendered with no boxes ticked. The user could then click submit
    without noticing the empty selection and end up with a config
    entry that polled zero models.
    """
    result = await _open_manual_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "settings"

    # Walk the schema to find the models_enabled marker and inspect
    # its default. Voluptuous stores the default as a callable that
    # returns the actual value.
    schema_markers = result["data_schema"].schema
    models_default = None
    for marker in schema_markers:
        if str(marker) == "models_enabled":
            models_default = marker.default()
            break

    assert models_default is not None, "models_enabled marker not in schema"
    # Must be a list (HA frontend serialises this to JSON), and must
    # contain at least one model that the test fixture exposes.
    assert isinstance(models_default, list)
    assert len(models_default) > 0
    # The test inverter exposes model 103 (inverter three phase) and
    # model 160 (multi-MPPT extension), both of which are in
    # DEFAULT_MODELS, so both must be pre-selected.
    assert "103" in models_default
    assert "160" in models_default


async def test_setup_settings_step_rejects_empty_model_selection(
    hass, bypass_get_data, sunspec_client_mock
):
    """Submitting the settings step with no models picked must show an inline error.

    The OPTIONS flow has had this guard since v0.7.6, but the
    initial SETUP flow was missing it. Without it the user could
    click through the form without ticking anything, persist
    ``models_enabled: []``, and the next coordinator reload would
    poll zero models and kill every sensor on the integration before
    they ever existed.
    """
    result = await _open_manual_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )
    assert result["step_id"] == "settings"

    # Submit with an explicitly empty model list. Must come back with
    # the form again and an inline base error.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "prefix": "",
            "scan_interval": 30,
            "models_enabled": [],
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "settings"
    assert result["errors"] == {"base": "no_models_selected"}


# ---------------------------------------------------------------------------
# Gold rule: reconfiguration-flow
# ---------------------------------------------------------------------------


async def test_reconfigure_flow_updates_host_when_serial_matches(
    hass, bypass_get_data, sunspec_client_mock
):
    """The Gold reconfiguration flow lets the user move an entry to a
    new IP without losing its history.

    The probe must succeed AND the inverter behind the new IP must
    report the same serial number as the existing entry's
    unique_id - otherwise we are looking at a different inverter
    and refuse to swap the history onto it.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        entry_id="test_reconfigure",
        unique_id="sn-123456789",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    # Submit a new host. The probe via sunspec_client_mock will return
    # the same serial number, so the entry is updated and the flow
    # aborts cleanly.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"host": "test_host_new", "port": 502, "unit_id": 1},
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "test_host_new"
    assert entry.data["port"] == 502


async def test_reconfigure_flow_refuses_serial_mismatch(
    hass, bypass_get_data, sunspec_client_mock, device_info_without_serial
):
    """If the new IP serves a different inverter, refuse the swap."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        entry_id="test_reconfigure_mismatch",
        unique_id="some-other-serial",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"host": "test_host_new", "port": 502, "unit_id": 1},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "unique_id_mismatch"}
    # Entry data was NOT touched.
    assert entry.data["host"] == "test_host"


# ---------------------------------------------------------------------------
# v0.11.0: Modbus RTU (serial) support
# ---------------------------------------------------------------------------


async def test_user_step_serial_branch_renders_serial_form(hass):
    """Picking 'serial' from the user-step menu must show the serial form
    with the four RTU fields (port, baudrate, parity, unit_id).
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.MENU

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "serial"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "serial"
    schema_keys = {str(marker) for marker in result["data_schema"].schema}
    assert {"serial_port", "baudrate", "parity", "unit_id"} <= schema_keys


async def test_serial_setup_creates_rtu_entry(hass, sunspec_client_mock):
    """Walking the serial setup happy path must create an RTU config entry.

    The sunspec_client_mock fixture patches modbus_connect at the
    SunSpecApiClient level, so the test never opens an actual serial
    port. The mock returns the same FileClientDevice as for TCP, so
    the unique-id helper reads the mock inverter's serial number and
    the entry lands in CREATE_ENTRY state.
    """
    from custom_components.sunspec2.const import CONF_BAUDRATE
    from custom_components.sunspec2.const import CONF_PARITY
    from custom_components.sunspec2.const import CONF_SERIAL_PORT
    from custom_components.sunspec2.const import CONF_TRANSPORT
    from custom_components.sunspec2.const import DEFAULT_BAUDRATE
    from custom_components.sunspec2.const import PARITY_NONE
    from custom_components.sunspec2.const import TRANSPORT_RTU

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "serial"}
    )
    assert result["step_id"] == "serial"

    # Submit the serial form
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SERIAL_PORT: "/dev/ttyUSB0",
            CONF_BAUDRATE: DEFAULT_BAUDRATE,
            CONF_PARITY: PARITY_NONE,
            CONF_UNIT_ID: 1,
        },
    )
    # Should land on the settings step (probe succeeded via the mock)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "settings"

    # Submit the settings step
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PREFIX: "",
            CONF_SCAN_INTERVAL: 30,
            CONF_ENABLED_MODELS: ["103"],
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TRANSPORT] == TRANSPORT_RTU
    assert result["data"][CONF_SERIAL_PORT] == "/dev/ttyUSB0"
    assert result["data"][CONF_BAUDRATE] == DEFAULT_BAUDRATE
    assert result["data"][CONF_PARITY] == PARITY_NONE
    assert result["data"][CONF_UNIT_ID] == 1


def _suggested_value(data_schema, field_name):
    """Read the suggested_value the form will pre-fill a field with.

    HA carries it in the voluptuous marker's ``description`` dict, which
    survives voluptuous_serialize but is easier to read straight off the
    schema.
    """
    for key in data_schema.schema:
        if str(key) == field_name:
            return (key.description or {}).get("suggested_value")
    raise AssertionError(f"{field_name} is not in the form schema")


async def _open_model_options(hass, entry):
    """Helper: walk an options flow up to the model_options form."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "host_options"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )
    assert result["step_id"] == "model_options"
    return result


def _schema_default(data_schema, field_name):
    """Read the default a vol.Optional field renders with."""
    for key in data_schema.schema:
        if str(key) == field_name:
            return key.default()
    raise AssertionError(f"{field_name} is not in the form schema")


@pytest.mark.parametrize("stored", [True, False])
async def test_options_flow_offers_standby_when_idle(hass, sunspec_client_mock, stored):
    """#52: the standby opt-out is on the form and round-trips its value."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        options={CONF_STANDBY_WHEN_IDLE: stored},
        entry_id="test",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = MockSunSpecDataUpdateCoordinator(hass, [1, 2])

    result = await _open_model_options(hass, entry)

    assert _schema_default(result["data_schema"], CONF_STANDBY_WHEN_IDLE) is stored


async def test_options_flow_suggests_nameplate_with_headroom(hass, sunspec_client_mock):
    """The pre-filled peak power must carry NAMEPLATE_FILTER_HEADROOM.

    Regression for #45. The filter grants 1.2x to an auto-detected
    nameplate and nothing to a configured value, on purpose. Pre-filling
    the field with the RAW nameplate therefore downgraded the ceiling to
    1.0x the moment anyone opened the form and pressed Submit, because
    the frontend echoes a suggested_value back on submit. At exactly the
    nameplate the DCW and VA sensors read "unknown" for most of a sunny
    day: DCW is measured before conversion losses and VA is by
    definition >= W.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    entry.add_to_hass(hass)
    coordinator = MockSunSpecDataUpdateCoordinator(hass, [1, 2])
    coordinator.detected_max_ac_power_kw = 5.0
    entry.runtime_data = coordinator

    result = await _open_model_options(hass, entry)

    assert _suggested_value(result["data_schema"], CONF_MAX_AC_POWER_KW) == 6.0


async def test_options_flow_shows_configured_peak_unchanged(hass, sunspec_client_mock):
    """A value the user typed is shown back as-is, with no headroom.

    The headroom belongs to the auto-detected nameplate only. Inflating
    a number the user deliberately entered would make the field lie
    about what the filter actually uses, and re-saving the form would
    ratchet the ceiling up by 20 % every time.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        options={CONF_MAX_AC_POWER_KW: 4.2},
        entry_id="test",
    )
    entry.add_to_hass(hass)
    coordinator = MockSunSpecDataUpdateCoordinator(hass, [1, 2])
    coordinator.detected_max_ac_power_kw = 5.0
    entry.runtime_data = coordinator

    result = await _open_model_options(hass, entry)

    assert _suggested_value(result["data_schema"], CONF_MAX_AC_POWER_KW) == 4.2


async def test_options_flow_clearing_peak_power_disables_filter(hass, sunspec_client_mock):
    """Clearing the peak-power box must actually delete the option.

    Regression for #45. ``self.options`` is seeded from the stored
    options and then merged with ``dict.update``, which cannot express a
    deletion, and an emptied vol.Optional field arrives as a MISSING key
    rather than None. So the UI's "leave it empty to disable filtering"
    was a no-op and the old ceiling survived every save - the one escape
    hatch from a filter eating real readings did not work.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        options={CONF_MAX_AC_POWER_KW: 5.0},
        entry_id="test",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = MockSunSpecDataUpdateCoordinator(hass, [1, 2])

    result = await _open_model_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_ENABLED_MODELS: ["1"], CONF_SCAN_INTERVAL: 10},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_MAX_AC_POWER_KW not in result["data"]


async def test_options_flow_keeps_peak_power_when_submitted(hass, sunspec_client_mock):
    """The other half of the contract: a submitted value is persisted."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        options={CONF_MAX_AC_POWER_KW: 5.0},
        entry_id="test",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = MockSunSpecDataUpdateCoordinator(hass, [1, 2])

    result = await _open_model_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_ENABLED_MODELS: ["1"],
            CONF_SCAN_INTERVAL: 10,
            CONF_MAX_AC_POWER_KW: 7.5,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MAX_AC_POWER_KW] == 7.5


async def test_setup_flow_suggests_nameplate_with_headroom(hass, sunspec_client_mock):
    """Same headroom contract on the initial setup form.

    The setup form has carried the raw-nameplate pre-fill since v0.8.1,
    which is why the too-tight ceiling was the normal state of an
    install rather than an edge case.
    """
    with patch(
        "custom_components.sunspec2.config_flow.SunSpecFlowHandler._probe_nameplate",
        return_value=5.0,
    ):
        result = await _open_manual_step(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=MOCK_CONFIG_STEP_1
        )

    assert result["step_id"] == "settings"
    assert _suggested_value(result["data_schema"], CONF_MAX_AC_POWER_KW) == 6.0


@pytest.mark.parametrize("bad_interval", [0, -5])
async def test_options_flow_rejects_an_unusable_scan_interval(
    hass, sunspec_client_mock, bad_interval
):
    """0 and negative intervals are not slow or fast, they are broken.

    Both schemas took a bare ``int``. Home Assistant stores the interval
    as ``value.total_seconds() if value else None``, so a saved 0 is
    falsy, ``_schedule_refresh`` returns early on None, and polling stops
    silently and permanently - nothing ever fails, so the entities do not
    even go unavailable. A negative value is truthy and puts the next
    refresh in the past, which hot-loops the coordinator.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    entry.add_to_hass(hass)
    entry.runtime_data = MockSunSpecDataUpdateCoordinator(hass, [1, 2])

    result = await _open_model_options(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_ENABLED_MODELS: ["1"], CONF_SCAN_INTERVAL: bad_interval},
        )


async def test_options_flow_accepts_the_minimum_scan_interval(hass, sunspec_client_mock):
    """The floor itself must still be reachable.

    Polling model 103 alone at 5 s is a legitimate setup: since v0.22.0
    the Modbus session is held open, so a single-model cycle is one
    register read plus pysunspec2's 0.6 s pause.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    entry.add_to_hass(hass)
    entry.runtime_data = MockSunSpecDataUpdateCoordinator(hass, [1, 2])

    result = await _open_model_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_ENABLED_MODELS: ["1"],
            CONF_SCAN_INTERVAL: MIN_SCAN_INTERVAL_SECONDS,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SCAN_INTERVAL] == MIN_SCAN_INTERVAL_SECONDS


async def test_setup_flow_rejects_an_unusable_scan_interval(hass, sunspec_client_mock):
    """The setup form carries the same floor as the options form."""
    result = await _open_manual_step(hass)
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id, user_input=MOCK_CONFIG_STEP_1)
    assert result["step_id"] == "settings"

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            flow_id,
            user_input={
                CONF_PREFIX: "",
                CONF_SCAN_INTERVAL: 0,
                CONF_ENABLED_MODELS: ["103"],
            },
        )


async def test_options_flow_reaches_the_model_form_without_a_coordinator(hass):
    """An entry that never loaded must still reach the second options step.

    An unreachable inverter leaves its entry in SETUP_RETRY, and Home
    Assistant has dropped runtime_data by then, so the options flow
    finds no coordinator. That is the state a user is in when the
    inverter moved to a new IP and they open the options to correct it.

    Reading coordinator.api here raised an AttributeError into the
    catch-all around this step, which showed the host form again with a
    "device_error" the user could not act on.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "host_options"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )

    assert result["step_id"] == "model_options"
    assert result["errors"] is None


async def test_options_flow_saves_a_new_host_without_a_coordinator(hass):
    """Reaching the form is not enough, the correction has to save.

    Nothing has polled the device, so the model multi-select has no
    live list to offer. An empty one cannot be submitted at all: the
    no_models_selected check rejects every submission, and the user is
    stuck one step further along than before. The form therefore falls
    back to the selection the entry already carries.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_HOST: "192.168.1.99", CONF_PORT: 502, CONF_UNIT_ID: 1},
    )
    assert result["step_id"] == "model_options"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_SCAN_INTERVAL: 30}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_HOST] == "192.168.1.99"


async def test_options_flow_keeps_the_serial_configuration(hass):
    """Saving options must not turn a serial entry into a TCP one.

    ``self.settings`` only ever holds what the host form asks for: host,
    port and unit id. Writing that as the whole of ``entry.data`` dropped
    transport, serial_port, baudrate and parity, which live nowhere else.
    The entry then read as a TCP entry pointed at a host that does not
    exist, and the only way back for the user was deleting it and adding
    it again.

    The user here does the ordinary thing: confirms the prefilled host
    form and changes the scan interval.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_RTU, entry_id="rtu")
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_HOST: "/dev/ttyUSB0", CONF_PORT: 19200, CONF_UNIT_ID: 1},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_SCAN_INTERVAL: 30}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_RTU
    assert entry.data[CONF_SERIAL_PORT] == "/dev/ttyUSB0"
    assert entry.data[CONF_BAUDRATE] == 19200
    assert entry.data[CONF_PARITY] == PARITY_NONE
