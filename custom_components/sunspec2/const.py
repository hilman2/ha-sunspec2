"""Constants for SunSpec 2."""

# Base component constants
NAME = "SunSpec 2"
DOMAIN = "sunspec2"
DOMAIN_DATA = f"{DOMAIN}_data"
VERSION = "0.18.0"

ATTRIBUTION = "Data provided by SunSpec alliance - https://sunspec.org"
ISSUE_URL = "https://github.com/hilman2/ha-sunspec2/issues"

# Icons
ICON = "mdi:format-quote-close"

# Device classes
BINARY_SENSOR_DEVICE_CLASS = "connectivity"

# Platforms
SENSOR = "sensor"
NUMBER = "number"
SWITCH = "switch"
# v0.12.0: write controls (Number, Switch) are kept off by default
# and only forwarded when the user explicitly opts in via the
# CONF_WRITE_BETA_ENABLED option. The sensor platform is always on.
PLATFORMS = [SENSOR, NUMBER, SWITCH]
PLATFORMS_READ_ONLY = [SENSOR]


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

# v0.12.0: experimental write support / inverter controls.
#
# Disabled by default. The user has to explicitly tick "Enable
# experimental write controls (BETA)" in the options flow before any
# Number / Switch entity from the write platforms shows up. Reason:
# writing to a Modbus register on a real inverter is genuinely risky
# (vendor-specific deviations from the SunSpec spec, persistence
# semantics that vary between firmware revisions, the possibility
# of locking yourself out of the inverter if the wrong combination
# of registers is sent), and the integration owner has no test
# hardware that exposes model 123 to validate the write path against.
# Until at least one community tester has confirmed the path on real
# hardware, every write entity is gated behind this flag and the
# README carries a clear "EXPERIMENTAL" disclaimer.
CONF_WRITE_BETA_ENABLED = "write_beta_enabled"

# Standard SunSpec model that exposes the immediate-control points
# we expose as writable Number / Switch entities. The integration
# only registers write entities when this model is part of
# coordinator.detected_models.
WRITE_CONTROLS_MODEL_ID = 123

# Upper bound for the model 123 WMaxLimPct Number entity, in percent of
# WMax. The SunSpec definition describes WMaxLimPct as a percentage of
# WMax and the obvious reading is 0..100, but real firmware uses values
# above 100 as a "no limit" sentinel: a KACO in #17 shipped with 110 and
# the user could not restore it, because HA's number component rejects
# an out-of-range service call and clamps the native value on top.
#
# Flat headroom on purpose, NOT derived from the current value. A
# ceiling that tracks what the device currently reports collapses back
# to 100 the moment the user writes 50, which puts 110 out of reach
# again - the exact bug, one write later.
EXPORT_LIMIT_HARD_MAX_PCT = 200

# UI step bounds for the same entity. The real step comes from the
# device's own WMaxLimPct_SF scale factor (10 ** SF): at SF -1 the
# inverter reports and accepts 99.5, and a hardcoded step of 1 makes
# the device's own value unenterable in the frontend box. Clamped so we
# are never coarser than the old behaviour and never finer than 0.01 %.
EXPORT_LIMIT_DEFAULT_STEP_PCT = 1.0
EXPORT_LIMIT_MIN_STEP_PCT = 0.01

# Seconds pysunspec2 sleeps after every model it walks during
# ``client.scan()``. Inherited verbatim at 0.5 from cjne/ha-sunspec in
# the phase 0 baseline, never chosen deliberately, and it is the single
# largest cost of a poll cycle: the coordinator closes its client at the
# end of every cycle (single-slot inverters need their Modbus slot back),
# so every cycle rescans the whole model tree from scratch. On an
# inverter exposing 20+ models that is 10+ seconds of pure sleep per
# cycle, independent of the network. Reported by @haraldg in #17 as
# "HA often needs quite a bit longer to update the values than expected"
# at a 30 s interval.
#
# The delay is not pointless: it paces the request stream for slow
# devices (KACO Powador on 100 Mbit was the original reason the setup
# timeout had to grow), which is why it stays configurable rather than
# being removed.
#
# The default stays at the inherited 0.5 rather than being tuned down,
# because MODEL_STRUCTURE_TTL_SECONDS took the scan out of the steady
# state: it now runs about once every 10 minutes instead of twice a
# minute, so the pacing costs roughly a fiftieth of what it did and
# there is no longer a trade to make between "fast polling" and "gentle
# on slow hardware". Lowering it only matters for the rescan itself.
CONF_SCAN_DELAY = "scan_delay"
DEFAULT_SCAN_DELAY_SECONDS = 0.5
MIN_SCAN_DELAY_SECONDS = 0.0
MAX_SCAN_DELAY_SECONDS = 2.0

# How long a cached SunSpec model structure stays valid before the
# next connect walks the model tree again.
#
# The coordinator closes its client at the end of every cycle so
# single-slot inverters get their Modbus slot back, and a fresh
# pysunspec2 client has an empty ``client.models``. That is the only
# reason the scan ran on every poll: it was never about detecting
# change, it was about rebuilding state we had already thrown away. On
# an inverter exposing 20 models, at 30 s intervals, that is 41 modbus
# round trips and 20 pacing sleeps every 30 seconds to rediscover a
# layout that changes on firmware updates and never otherwise.
#
# Caching the layout (base address plus model id / address / length per
# block) lets a reconnect rebuild the same model objects with a single
# validating read. The scan still happens, just on this interval rather
# than on every cycle, which is also what keeps the pacing question out
# of the steady state: a scan every 10 minutes can afford to be slow.
MODEL_STRUCTURE_TTL_SECONDS = 600

# Points the sensor platform must not build an entity for, per model.
#
# This started life as SENSOR_EXCLUDED_MODELS, which skipped model 123
# wholesale. That was too blunt, and @tisoft found the hole in #17: his
# KACO silently drops the export limit after WMaxLimPct_RvrtTms seconds
# while WMaxLim_Ena and WMaxLimPct keep reporting the old setpoint, so
# the integration shows a limit that is no longer in force. The points
# that expose the timer are exactly the ones a user needs to notice
# that, and they were the collateral damage of the model-wide skip.
#
# Only two kinds of point actually have to go:
#
#   * Points already exposed as a Number or Switch. A sensor there is a
#     read-only duplicate of a control the user can operate, and the
#     two would disagree during a write.
#   * Points whose SunSpec unit has no HA equivalent ("% WMax",
#     "% VArMax", "% VArAval"). sensor.py falls back to the raw SunSpec
#     string as the native unit with state_class MEASUREMENT, which
#     starts a long-term statistics series under a unit the recorder
#     can never convert or merge.
#
# Everything else in model 123 is a "Secs" timer or an enum16, both of
# which HA maps cleanly, so they come back as diagnostic sensors.
SENSOR_EXCLUDED_POINTS: dict[int, frozenset[str]] = {
    WRITE_CONTROLS_MODEL_ID: frozenset(
        {
            # Exposed as Number / Switch entities.
            "Conn",
            "WMaxLimPct",
            "WMaxLim_Ena",
            "OutPFSet",
            "OutPFSet_Ena",
            "WMaxLimPct_RvrtTms",
            # Units HA has no equivalent for.
            "VArWMaxPct",
            "VArMaxPct",
            "VArAvalPct",
        }
    )
}


def is_excluded_sensor_point(model_id: int, key: str) -> bool:
    """True if the sensor platform must not build an entity for this point.

    ``key`` is the flattened models.py key, so a repeating-group point
    arrives as ``group:idx:point``. Only the trailing point name is
    matched, because the exclusion is a property of the point, not of
    the group instance it happens to live in.
    """
    excluded = SENSOR_EXCLUDED_POINTS.get(model_id)
    if not excluded:
        return False
    return key.rsplit(":", 1)[-1] in excluded


# Service action names. The service handler reads the entry by
# entry_id from the service-call data so multi-inverter installs
# pick the right device.
SERVICE_SET_EXPORT_LIMIT = "set_export_limit"
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
# After this many consecutive rejected reads the energy plausibility filter
# accepts the new value as the new baseline. Without this escape hatch a
# legitimate large jump (e.g. an inverter that bumps its lifetime counter
# in coarse 1 kWh steps, or a freshly-started integration whose first read
# was an outlier below the truth) would freeze the sensor on its initial
# value forever, since lastKnown is never updated while the filter rejects.
ENERGY_DELTA_REJECT_RECOVERY_COUNT = 3

# Resilience: when an update cycle fails after the integration is already
# running, wait this many seconds and retry the cycle once before giving
# up. Inverters and Modbus TCP gateways have famously flaky connectivity
# and a single fast retry catches most one-shot blips before HA marks the
# coordinator as failed. The first refresh during setup deliberately does
# NOT use this retry - first-refresh failure raises ConfigEntryNotReady
# and HA's own exponential backoff takes over.
INTERVAL_RETRY_DELAY_SECONDS = 5

# How long a write waits for the per-gateway lock before giving up.
#
# Sizing this off the scan interval is tempting and wrong: the wait is
# bounded by lock HOLD time times queued waiters, not by how often we
# poll. One hold is dominated by pysunspec2's own sleeps - scan()
# sleeps `delay` (0.5s) per discovered model and read_model sleeps
# 0.6s per model instance - so an 18-model inverter spends roughly 9s
# in the rescan before a single sensor register is read, and a full
# hold lands at 15-20s. Two config entries behind one Modbus gateway
# can therefore legitimately queue past 30s without anything being
# wrong, and a timeout that fires on healthy hardware is worse than
# no timeout at all.
#
# 120s is "something is genuinely stuck" territory rather than "the
# gateway is busy". A write that fails with a readable message still
# beats a write that never returns, which is what we had before: HA
# has no service-call timeout of its own (SLOW_UPDATE_WARNING only
# fires inside async_update_ha_state(force_refresh=True), which
# CoordinatorEntity never reaches because should_poll is False).
WRITE_LOCK_TIMEOUT_SECONDS = 120

# Resilience: keep serving the last successfully-read value through the
# entity's `available` property for up to this many consecutive failed
# update cycles before flipping to "unavailable". With the default 30s
# scan interval and the 5s in-cycle retry, this rides out roughly three
# minutes of dropped connectivity without bouncing the long-term
# statistics graphs to "unknown".
STALE_DATA_TOLERANCE_CYCLES = 5

# How long a previously-detected SunSpec model has to stay missing
# from successful scans before we raise a Repairs issue suggesting the
# user remove the related device. Generous on purpose: SMA Tripower X12
# (cjne issue #202) sometimes stops exposing model 714 for hours during
# low-light conditions, and a one-time hiccup should not escalate.
#
# Measured in seconds, not cycles. It used to be 20 cycles, chosen
# because "with the default 30s scan interval that is roughly ten
# minutes". That equivalence broke the moment the model tree stopped
# being scanned on every cycle (MODEL_STRUCTURE_TTL_SECONDS): a counter
# that only advances on a real scan would have stretched the same 20
# steps to over three hours. Wall-clock time is what the threshold
# always meant, so it is now what it measures, and it no longer moves
# when a poll or rescan interval changes.
STALE_MODEL_TOLERANCE_SECONDS = 600

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
