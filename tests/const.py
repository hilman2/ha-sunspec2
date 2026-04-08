"""Constants for SunSpec tests."""

from custom_components.sunspec2.const import CONF_ENABLED_MODELS
from custom_components.sunspec2.const import CONF_HOST
from custom_components.sunspec2.const import CONF_PORT
from custom_components.sunspec2.const import CONF_PREFIX
from custom_components.sunspec2.const import CONF_SCAN_INTERVAL
from custom_components.sunspec2.const import CONF_TRANSPORT
from custom_components.sunspec2.const import CONF_UNIT_ID
from custom_components.sunspec2.const import TRANSPORT_TCP

MOCK_SETTINGS_PREFIX = {
    CONF_ENABLED_MODELS: [160],
    CONF_PREFIX: "test",
    CONF_SCAN_INTERVAL: 10,
}
MOCK_SETTINGS = {CONF_ENABLED_MODELS: [103, 160], CONF_SCAN_INTERVAL: 10}
MOCK_SETTINGS_MM = {CONF_ENABLED_MODELS: [701], CONF_SCAN_INTERVAL: 10}
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
