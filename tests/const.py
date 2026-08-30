"""Constants for SunSpec tests."""

from custom_components.sunspec2.const import CONF_BAUDRATE
from custom_components.sunspec2.const import CONF_ENABLED_MODELS
from custom_components.sunspec2.const import CONF_HOST
from custom_components.sunspec2.const import CONF_PARITY
from custom_components.sunspec2.const import CONF_PORT
from custom_components.sunspec2.const import CONF_PREFIX
from custom_components.sunspec2.const import CONF_SCAN_INTERVAL
from custom_components.sunspec2.const import CONF_SERIAL_PORT
from custom_components.sunspec2.const import CONF_TRANSPORT
from custom_components.sunspec2.const import CONF_UNIT_ID
from custom_components.sunspec2.const import PARITY_NONE
from custom_components.sunspec2.const import TRANSPORT_RTU
from custom_components.sunspec2.const import TRANSPORT_TCP

MOCK_SETTINGS_PREFIX = {
    CONF_ENABLED_MODELS: ["160"],
    CONF_PREFIX: "test",
    CONF_SCAN_INTERVAL: 10,
}
MOCK_SETTINGS = {CONF_ENABLED_MODELS: ["103", "160"], CONF_SCAN_INTERVAL: 10}
MOCK_SETTINGS_MM = {CONF_ENABLED_MODELS: ["701"], CONF_SCAN_INTERVAL: 10}
MOCK_CONFIG_STEP_1 = {CONF_HOST: "test_host", CONF_PORT: 123, CONF_UNIT_ID: 1}
# v0.11.0 added the explicit ``transport`` field. Old config entries
# without it default to TCP via ``entry.data.get(CONF_TRANSPORT,
# TRANSPORT_TCP)`` in __init__.py, so existing installs migrate
# transparently. Fresh setups (and our tests) get the field set
# explicitly so the config-entry shape matches what the user flow
# actually persists.
MOCK_CONFIG = {
    CONF_TRANSPORT: TRANSPORT_TCP,
    CONF_HOST: "test_host",
    CONF_PORT: 123,
    CONF_UNIT_ID: 1,
    CONF_PREFIX: "",
    CONF_SCAN_INTERVAL: 10,
    CONF_ENABLED_MODELS: MOCK_SETTINGS[CONF_ENABLED_MODELS],
}
# A serial entry as the RTU branch of the config flow persists it. The
# host and port keys carry the port name and the baud rate as synthetic
# coordinates, so the logger prefix and the gateway lock have a stable
# identifier; the serial line itself is read from the four fields above
# them and from nowhere else.
MOCK_CONFIG_RTU = {
    CONF_TRANSPORT: TRANSPORT_RTU,
    CONF_SERIAL_PORT: "/dev/ttyUSB0",
    CONF_BAUDRATE: 19200,
    CONF_PARITY: PARITY_NONE,
    CONF_UNIT_ID: 1,
    CONF_HOST: "/dev/ttyUSB0",
    CONF_PORT: 19200,
    CONF_PREFIX: "",
    CONF_SCAN_INTERVAL: 10,
    CONF_ENABLED_MODELS: MOCK_SETTINGS[CONF_ENABLED_MODELS],
}
MOCK_CONFIG_MM = {
    CONF_HOST: "test_host",
    CONF_PORT: 123,
    CONF_UNIT_ID: 1,
    CONF_PREFIX: "",
    CONF_ENABLED_MODELS: MOCK_SETTINGS_MM[CONF_ENABLED_MODELS],
}
MOCK_CONFIG_PREFIX = {
    CONF_HOST: "test_host",
    CONF_PORT: 123,
    CONF_UNIT_ID: 1,
    CONF_PREFIX: "test",
    CONF_ENABLED_MODELS: MOCK_SETTINGS_PREFIX[CONF_ENABLED_MODELS],
}
# Config for the experimental write platforms. Pairs with the
# ``sunspec_write_client_mock`` fixture, whose device file exposes
# models 1, 160 and 123.
#
# Note what is NOT in CONF_ENABLED_MODELS: model 123. That is the
# realistic case and the one #17 tripped over. 123 is not in
# DEFAULT_MODELS, so nobody has it ticked unless they went looking,
# and before v0.14.0 the write entities silently never appeared. The
# coordinator now adds it to the polled set from the beta flag alone.
MOCK_CONFIG_WRITE = {
    CONF_TRANSPORT: TRANSPORT_TCP,
    CONF_HOST: "test_host",
    CONF_PORT: 123,
    CONF_UNIT_ID: 1,
    CONF_PREFIX: "",
    CONF_SCAN_INTERVAL: 10,
    CONF_ENABLED_MODELS: ["160"],
}
# Entity ids the write platforms produce for the fixture device.
# Since v0.15.0 the device slug carries the model suffix (issue #33):
# Md "Test-1547-1" plus model 123's label "Immediate Controls" gives
# ``test_1547_1_immediate_controls``.
TEST_NUMBER_EXPORT_LIMIT = "number.test_1547_1_immediate_controls_export_limit"
TEST_NUMBER_POWER_FACTOR = "number.test_1547_1_immediate_controls_power_factor_setpoint"
TEST_SWITCH_EXPORT_LIMIT_ENA = "switch.test_1547_1_immediate_controls_export_limit_enabled"
TEST_SWITCH_POWER_FACTOR_ENA = "switch.test_1547_1_immediate_controls_power_factor_enabled"
TEST_SWITCH_CONN = "switch.test_1547_1_immediate_controls_inverter_grid_connection"
