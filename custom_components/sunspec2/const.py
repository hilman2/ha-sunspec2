"""Constants for SunSpec 2."""

# Base component constants
NAME = "SunSpec 2"
DOMAIN = "sunspec2"
DOMAIN_DATA = f"{DOMAIN}_data"
VERSION = "0.11.1"

ATTRIBUTION = "Data provided by SunSpec alliance - https://sunspec.org"
ISSUE_URL = "https://github.com/hilman2/ha-sunspec2/issues"

# Icons
ICON = "mdi:format-quote-close"

# Device classes
BINARY_SENSOR_DEVICE_CLASS = "connectivity"

# Platforms
SENSOR = "sensor"
PLATFORMS = [SENSOR]


# Configuration and options
CONF_ENABLED = "enabled"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_UNIT_ID = "unit_id"
# Legacy constant for backward compatibility
CONF_SLAVE_ID = "slave_id"  # Deprecated, use CONF_UNIT_ID
CONF_PREFIX = "prefix"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_ENABLED_MODELS = "models_enabled"

# v0.11.0: Modbus transport selection. ``tcp`` is the default and the
# only mode that existed before; ``rtu`` is the new serial-line variant
# that talks to the inverter over an RS-485 (typically via a USB-to-
# RS-485 adapter on /dev/ttyUSB0 or similar). Existing config entries
# without CONF_TRANSPORT continue to work because the coordinator
# defaults to TCP when the field is missing - no migration needed.
CONF_TRANSPORT = "transport"
TRANSPORT_TCP = "tcp"
TRANSPORT_RTU = "rtu"

# Modbus RTU specific config keys.
CONF_SERIAL_PORT = "serial_port"
CONF_BAUDRATE = "baudrate"
CONF_PARITY = "parity"

# pysunspec2 only exposes the two ``N`` (none) and ``E`` (even) parity
# settings via its modbus client; we mirror those constants directly so
# the config flow's dropdown values match what the underlying library
# accepts at construction time.
PARITY_NONE = "N"
PARITY_EVEN = "E"

# Sensible default for any inverter we have seen so far. Users with a
# different setting can override during the serial setup step.
DEFAULT_BAUDRATE = 9600
# Phase 2 debugging-first: when True, the next scan also stores raw modbus
# bytes in api._captured_reads so users can attach a reproducible fixture
# to bug reports via the diagnostics dump.
CONF_CAPTURE_RAW = "capture_raw_registers"
# Plausibility limit used to drop unrealistic values reported by inverters
# at dawn / dusk (e.g. MW or TWh spikes that poison long-term statistics).
# Optional - leaving the option empty disables the filter. The value is
# used in two ways:
#   * Power-like sensors (W / VA / VAr) are dropped if they exceed this
#     value (in kW).
#   * Energy sensors are dropped when the delta to the previous value would
#     imply an instantaneous power above this value, with a safety factor.
CONF_MAX_AC_POWER_KW = "max_ac_power_kw"
# Safety factor applied when deriving the maximum plausible energy delta
# from the configured peak power. Generous on purpose - we only want to
# catch the really obvious garbage values (MW / TWh spikes), not legitimate
# transients near the inverter's nameplate.
ENERGY_DELTA_SAFETY_FACTOR = 2.0

# Resilience: when an update cycle fails after the integration is already
# running, wait this many seconds and retry the cycle once before giving
# up. Inverters and Modbus TCP gateways have famously flaky connectivity
# and a single fast retry catches most one-shot blips before HA marks the
# coordinator as failed. The first refresh during setup deliberately does
# NOT use this retry - first-refresh failure raises ConfigEntryNotReady
# and HA's own exponential backoff takes over.
INTERVAL_RETRY_DELAY_SECONDS = 5

# Resilience: keep serving the last successfully-read value through the
# entity's `available` property for up to this many consecutive failed
# update cycles before flipping to "unavailable". With the default 30s
# scan interval and the 5s in-cycle retry, this rides out roughly three
# minutes of dropped connectivity without bouncing the long-term
# statistics graphs to "unknown".
STALE_DATA_TOLERANCE_CYCLES = 5

DEFAULT_MODELS = set(
    [
        101,
        102,
        103,
        160,
        201,
        202,
        203,
        204,
        307,
        308,
        401,
        402,
        403,
        404,
        501,
        502,
        601,
        701,
        801,
        802,
        803,
        804,
        805,
        806,
        808,
        809,
    ]
)
# Defaults
DEFAULT_NAME = DOMAIN

STARTUP_MESSAGE = f"""
-------------------------------------------------------------------
{NAME}
Version: {VERSION}
This is a custom integration!
If you have any issues with this you need to open an issue here:
{ISSUE_URL}
-------------------------------------------------------------------
"""
