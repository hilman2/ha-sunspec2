"""Constants for SunSpec 2."""

from __future__ import annotations

# Base component constants
NAME = "SunSpec 2"
DOMAIN = "sunspec2"
DOMAIN_DATA = f"{DOMAIN}_data"
VERSION = "2026.9.1"

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
SELECT = "select"
# v0.12.0: write controls (Number, Switch) are kept off by default
# and only forwarded when the user explicitly opts in via the
# CONF_WRITE_BETA_ENABLED option. The sensor platform is always on.
PLATFORMS = [SENSOR, NUMBER, SWITCH, SELECT]
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
# Every model the write platforms can build controls for. The
# coordinator polls whichever of these a device exposes while the beta
# flag is on, so users do not have to find and tick them in the model
# list first, which is the bug #30 fixed for model 123 alone.
WRITE_CAPABLE_MODEL_IDS: frozenset[int] = frozenset({123, 124, 704})

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
# the phase 0 baseline, never chosen deliberately. Until v0.22.0 it was
# the single largest cost of a poll cycle: the coordinator closed its
# client at the end of every cycle (single-slot inverters were assumed
# to need their Modbus slot back), so every cycle rescanned the whole
# model tree from scratch. On an inverter exposing 20+ models that was
# 10+ seconds of pure sleep per cycle, independent of the network.
# Reported by @haraldg in #17 as "HA often needs quite a bit longer to
# update the values than expected" at a 30 s interval. The session is
# held open now, so a steady-state poll pays none of it.
#
# The delay is not pointless: it paces the request stream for slow
# devices (KACO Powador on 100 Mbit was the original reason the setup
# timeout had to grow), which is why it stays configurable rather than
# being removed.
#
# The default stays at the inherited 0.5 rather than being tuned down,
# because the persisted model layout took the scan out of the steady
# state entirely: it now runs on the first connect with nothing usable
# stored, after a failed cycle (reconnect_next() drops the cache on
# purpose, because a layout read at addresses that just stopped
# answering is the one thing not to reuse), and when a firmware change
# moves the model chain. Not twice a minute, so there is no longer a
# trade to make between "fast polling" and "gentle on slow hardware".
# Lowering it only affects that rare walk.


# Give the inverter's Modbus slot back between polls instead of
# holding one session open.
#
# Off by default, which is the opposite of what this integration did
# until v0.22.0. Measured against a KACO Powador 7.8 TL3, polling every
# 30 s: reconnecting per poll failed 5 of 6 cycles, while a single
# session held open served 20 of 20 polls in a steady 1.6 s each. The
# device is not flaky, it simply cannot build a fresh Modbus session
# every 30 seconds, and Modbus TCP was never meant to be used that way.
#
# Turning this on is for one situation only: something else on the
# network has to read the same inverter, and it cannot go through a
# Modbus proxy. Sharing a single slot by taking turns is unreliable by
# construction, so a proxy is the better answer wherever it is possible.
# Multiple config entries behind one gateway do not need this option;
# that case is detected and handled on its own.
CONF_RELEASE_SLOT = "release_slot"

# Issue #52: many PV inverters power their communication board down
# when there is no DC input, which on a domestic roof means every
# night. The TCP session dies with it, so the integration sees a plain
# connect timeout and, three cycles later, raises a red "Cannot reach
# SunSpec inverter" repair. Nothing is broken, and there is nothing for
# the user to fix.
#
# The coordinator detects the common case on its own by reading the
# inverter's own operating state (see OPERATING_STATE_MODEL_IDS below),
# so this option only exists for the devices that cannot be detected:
# ones that drop the link without ever reporting SLEEPING in a poll we
# still got an answer to, and ones whose model set has no operating
# state point at all (an SMA Tripower X speaking model 701 rather than
# 103, for instance).
#
# It suppresses the transport repair only. A device that answers with
# a Modbus exception, or answers with something that is not SunSpec,
# is awake and still escalates as before.
CONF_STANDBY_WHEN_IDLE = "standby_when_idle"

# Lower bound for the poll interval, enforced in the config flow and
# again in the coordinator.
#
# Not a taste call, and not about being gentle on the device. Both
# schemas took a bare ``int`` with no range, and both ends of that are
# broken in Home Assistant's own coordinator:
#
#   0   ``DataUpdateCoordinator.update_interval`` stores
#       ``value.total_seconds() if value else None``, and
#       ``timedelta(seconds=0)`` is falsy. ``_schedule_refresh()`` then
#       returns early on ``None`` and polling stops silently and
#       permanently. Nothing looks broken: no cycle runs, so no cycle
#       fails, so ``consecutive_failed_cycles`` never moves and the
#       entities do not even go unavailable. They just quietly hold
#       their last value forever.
#   <0  truthy, so the interval survives and ``next_refresh`` lands in
#       the past. ``loop.call_at`` fires immediately, every time, for
#       as long as Home Assistant is up.
#
# 5 s rather than 1 s because a single model read already costs the
# MODEL_READ_PACING_SECONDS pause in api.py plus a round trip, and
# INTERVAL_RETRY_DELAY_SECONDS is itself 5: below that the in-cycle
# retry, not the configured interval, sets the real pace.
MIN_SCAN_INTERVAL_SECONDS = 5

CONF_SCAN_DELAY = "scan_delay"
DEFAULT_SCAN_DELAY_SECONDS = 0.5
MIN_SCAN_DELAY_SECONDS = 0.0
MAX_SCAN_DELAY_SECONDS = 2.0

# Storage key and version for the persisted SunSpec model layout.
#
# The coordinator used to close its client at the end of every cycle so
# single-slot inverters got their Modbus slot back, and a fresh
# pysunspec2 client has an empty ``client.models``. That is the only
# reason the scan ever ran on every poll: it was never about detecting
# change, it was about rebuilding state we had just thrown away. On an
# inverter exposing 20 models, at 30 s intervals, that was 41 modbus
# round trips and 20 pacing sleeps every 30 seconds to rediscover a
# layout that changes on firmware updates and never otherwise. v0.22.0
# holds the session open, so a poll no longer pays any of it. The cache
# still earns its keep on the paths that do build a client again:
# CONF_RELEASE_SLOT and the shared-gateway case, where ``close()``
# leaves the layout intact and the next connect rebuilds from it. A
# failed cycle is the one path that drops the layout on purpose; the
# only other way back to a full scan is a connect whose cached layout
# no longer validates.
#
# Caching the layout (base address plus model id / address / length per
# block) lets a reconnect rebuild the same model objects from three
# short validating reads. Persisting it means a Home Assistant restart
# does not have to scan either, which is where the scan actually hurt:
# it runs inside the setup timeout, on the slowest devices, at the
# worst possible moment.
#
# There is deliberately no expiry. A rescan cannot confirm a layout, it
# can only replace it, and it is the one operation that can silently
# corrupt it (see the comment on the cache in api.py). Freshness is
# established by re-reading both ends of the chain on every connect,
# not by a clock.
STRUCTURE_STORAGE_VERSION = 1
STRUCTURE_STORAGE_KEY = f"{DOMAIN}.model_structure"
# The model 1 points that say WHICH device the stored layout belongs to.
# Deliberately without "Vr": a firmware update changes the version
# string and nothing else about the identity, and treating it as a
# device swap failed setup in a loop after every update (#49). The
# version is still stored next to these, so the change can be noticed
# and answered with a one-off rescan of the model tree.
DEVICE_IDENTITY_POINTS = ("Mn", "Md", "SN")

# Points the sensor platform must not build an entity for.
#
# Derived from the write-control specs rather than hand-maintained: a
# point that is a Number, Switch or Select must not also be a read-only
# sensor, because the two disagree during a write and the user has no
# way to tell which one is lying. Keeping a second list in sync with
# write_controls.py by hand is exactly the kind of bookkeeping that
# quietly rots.
#
# The unit problem that motivated half of the old hand-written set is
# fixed properly in sensor.py as of v0.19.0: an unmapped SunSpec unit
# resolves to no unit rather than to the raw string, so it can no longer
# start a statistics series the recorder cannot convert, and "% WMax"
# and its relatives map to PERCENTAGE anyway.


def is_excluded_sensor_point(model_id: int, key: str) -> bool:
    """True if the sensor platform must not build an entity for this point.

    ``key`` is the flattened models.py key, so a repeating-group point
    arrives as ``group:idx:point``. Only the trailing point name is
    matched, because the exclusion is a property of the point, not of
    the group instance it happens to live in.

    Imported lazily: write_controls imports nothing from const, but
    const is imported by everything, and a module-level import here
    would make that a cycle waiting for someone to add one line.
    """
    from .write_controls import specs_for_model

    specs = specs_for_model(model_id)
    if not specs:
        return False
    point_name = key.rsplit(":", 1)[-1]
    return any(spec.point_name == point_name for spec in specs)


# Service action names. The service handler reads the entry by
# entry_id from the service-call data so multi-inverter installs
# pick the right device.
SERVICE_SET_EXPORT_LIMIT = "set_export_limit"
# Phase 2 debugging-first: when True, every read on the Modbus session
# also stores its raw bytes in api._captured_reads so users can attach
# a reproducible fixture to bug reports via the diagnostics dump. The
# wrap goes on when the client is built, so it covers a scan, the
# cached-layout validation reads and every model read alike. Saving the
# option reloads the entry, and the connect that follows is where it
# takes effect.
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
# transients near the inverter's nameplate. The delta is measured over the
# time since the counter last moved (floored at the scan interval), not
# over one scan interval: an inverter that updates its lifetime counter
# every few minutes reports the same value for several polls and then
# jumps by the whole accumulated amount, and that jump is correct (#45).
ENERGY_DELTA_SAFETY_FACTOR = 2.0
# Headroom applied to the AUTO-DETECTED nameplate when it stands in for
# a peak the user never configured.
#
# Both plausibility filters used to be off entirely unless the user
# found and filled in "peak AC power" in the options, which is exactly
# the user who does not know they need it. The nameplate we already read
# from model 120 (WRtg) is a better default than no filter at all, but
# it is not a hard ceiling: inverters legitimately overshoot their
# continuous rating in cold, bright weather, and an installer-set WMax
# from model 121 can sit below what the hardware really does. 1.2 leaves
# room for that while still catching the order-of-magnitude garbage the
# filter exists for. A user-configured value is used as-is, without this
# factor, because that number is a deliberate statement about the site.
NAMEPLATE_FILTER_HEADROOM = 1.2


def effective_peak_power_kw(configured_kw: float | None, detected_kw: float | None) -> float | None:
    """Peak AC power the plausibility filters work from, in kW.

    Shared by the sensor platform (which applies it) and diagnostics
    (which reports it), so a bug report can never disagree with what the
    filter actually did. ``None`` means no ceiling and no filtering.
    """
    if configured_kw:
        return float(configured_kw)
    if detected_kw:
        return float(detected_kw) * NAMEPLATE_FILTER_HEADROOM
    return None


# Which SunSpec points the power plausibility filter is allowed to touch,
# and how far above the peak AC power each of them may legitimately go.
#
# Until #45 the filter keyed on the UNIT alone, which is wrong twice over.
#
# Wrong once, because the ceiling is an ACTIVE power number and three of
# the four quantities carrying a power unit are bounded by something else:
#
#   apparent  VA = W / cos phi, and grid codes require operation down to
#             cos phi 0.80 (VDE-AR-N 4105, IEEE 1547 category B), so
#             apparent power legitimately runs above active power. It is
#             also >= W by definition, so a ceiling at the AC rating
#             takes the VA sensor out before it takes the W sensor out.
#   reactive  bounded by apparent power, so it gets the same allowance.
#             A watt number carries no information about a var limit.
#   dc        DCW is measured BEFORE conversion losses, so DCW = W / eta
#             and it always reads above AC output. On a hybrid with a
#             DC-coupled battery the DC side carries AC output and charge
#             power at the same time, which is why this one is widest.
#
# Wrong twice, because a unit-only rule also swept up every static rating
# and setpoint that happens to be measured in watts - WRtg, WMax, VARtg,
# VAMax, VarSet, PMaxLim, LifeTimeMaxOut, the model 706 / 712 curve
# points. Gating a nameplate against a limit derived from that same
# nameplate is circular, and on a device where VARtg exceeds the watt
# rating the rated-apparent-power sensor simply read "unknown".
#
# So this is an allowlist of LIVE MEASUREMENTS, built from the point
# names pysunspec2 actually ships. Anything not named here is never
# filtered, which is the right failure mode: the filter exists to catch
# order-of-magnitude garbage (MW spikes at dawn), not to enforce tight
# physical bounds, so leaving an unknown vendor point alone costs
# nothing while wrongly clipping it costs the user a sensor.
MEASURED_POWER_POINT_HEADROOM: dict[str, float] = {
    # AC active power. The ceiling is expressed in exactly this quantity.
    "W": 1.0,
    "WphA": 1.0,
    "WphB": 1.0,
    "WphC": 1.0,
    "WL1": 1.0,
    "WL2": 1.0,
    "WL3": 1.0,
    # Apparent power (inverter models 10x / 11x, meter models 20x, 701).
    "VA": 1.25,
    "VAphA": 1.25,
    "VAphB": 1.25,
    "VAphC": 1.25,
    "VAL1": 1.25,
    "VAL2": 1.25,
    "VAL3": 1.25,
    # Reactive power. SunSpec spells this four ways across its models.
    "VAr": 1.25,
    "VAR": 1.25,
    "VARphA": 1.25,
    "VARphB": 1.25,
    "VARphC": 1.25,
    "Var": 1.25,
    "VarL1": 1.25,
    "VarL2": 1.25,
    "VarL3": 1.25,
    # DC power: inverter DC input (10x / 11x), per-MPPT module:N:DCW
    # (160), DER port DCW (714), string combiner (402 / 404).
    #
    # The physical bound on the DC side is the sum of the MPPT input
    # limits, plus the battery charge power on a DC-coupled hybrid, and
    # neither number is in any register this integration reads (model
    # 160 carries no ratings). So a factor on the AC rating can only be
    # a garbage ceiling here, never a physical one, and it has to sit
    # above every real layout: two MPPTs each rated at the full AC power
    # is 2.0x on its own and is sold today, a DC-coupled battery charges
    # on top of that, and a small AC stage in front of a large DC side
    # (AC rating near the export limit) pushes it further still (#45).
    # 1.5 was inside that range. 3.0 is above it and still catches what
    # the filter exists for, a misread scale factor (10x and up) or a
    # shifted register.
    "DCW": 3.0,
    "InDCW": 3.0,
}


def measured_power_headroom(key: str) -> float | None:
    """Headroom factor for a point key, or ``None`` if it must not be gated.

    ``key`` is the flattened models.py key, so a repeating-group point
    arrives as ``group:idx:point`` and only the trailing name decides -
    ``module:0:DCW`` is the same physical quantity as ``DCW``.
    """
    return MEASURED_POWER_POINT_HEADROOM.get(key.rsplit(":", 1)[-1])


# The power plausibility filter emits one WARNING per rejected read. When
# the ceiling is set too low that is one line per affected sensor per
# poll, for as long as the sun shines, which is how #45 buried its own
# evidence in the reporter's log. Log the first rejection of a run, then
# every Nth, then a single line when the sensor comes back.
IMPLAUSIBLE_LOG_EVERY = 20
# After this many consecutive rejected reads the energy plausibility filter
# accepts the new value as the new baseline. Without this escape hatch a
# legitimate large jump the time window above cannot explain (a restored
# baseline whose timestamp is off, a freshly-started integration whose
# first read was an outlier below the truth) would freeze the sensor on
# its initial value forever, since lastKnown is never updated while the
# filter rejects. Counters that move in coarse steps used to need this
# hatch on every step; the time window handles them without a rejection.
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
# poll. One hold is dominated by pysunspec2's own sleeps - read_model
# sleeps 0.6s per model instance, so a hold that polls 18 model
# instances spends nearly 11s in pacing alone before the last register
# is read. A hold that has to build a client first pays scan() on top,
# which sleeps `delay` (0.5s) per discovered model: that was every hold
# until v0.22.0, and it is now a first connect with no cached layout,
# a reconnect after a failure, and any connect whose cached layout no
# longer validates. Two config entries behind one Modbus gateway can
# therefore legitimately queue past 30s without anything being wrong,
# and a timeout that fires on healthy hardware is worse than no timeout
# at all.
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

# Issue #52: SunSpec models that carry the inverter operating state
# point, and the values of that point which mean "I am powering
# myself down".
#
# Deliberately only the inverter models 101/102/103 (integer + scale
# factor) and 111/112/113 (float). They share one enum: 1 OFF,
# 2 SLEEPING, 3 STARTING, 4 MPPT, 5 THROTTLED, 6 SHUTTING_DOWN,
# 7 FAULT, 8 STANDBY.
#
# Model 701 (DER AC Measurement) also has a point called "St" and it
# must stay out of this list. Its enum is 0 OFF / 1 ON, so a perfectly
# healthy inverter reporting ON would read as OFF here and silence a
# genuine outage.
OPERATING_STATE_MODEL_IDS = (101, 102, 103, 111, 112, 113)
OPERATING_STATE_POINT = "St"
# Only the states the device reaches on its way out. FAULT is not one
# of them: a faulted inverter that then stops answering is exactly the
# case the repair issue exists for.
OPERATING_STATE_LABELS = {
    1: "OFF",
    2: "SLEEPING",
    6: "SHUTTING_DOWN",
    8: "STANDBY",
}
STANDBY_OPERATING_STATES = frozenset(OPERATING_STATE_LABELS)

# How long a previously-detected SunSpec model has to stay missing
# from successful scans before we raise a Repairs issue suggesting the
# user remove the related device. Generous on purpose: SMA Tripower X12
# (cjne issue #202) sometimes stops exposing model 714 for hours during
# low-light conditions, and a one-time hiccup should not escalate.
#
# Measured in seconds, not cycles. It used to be 20 cycles, chosen
# because "with the default 30s scan interval that is roughly ten
# minutes". That equivalence broke the moment the model tree stopped
# being scanned on every cycle: a counter that only advances on a real
# scan now never advances at all on a healthy device, because a healthy
# device never needs a rescan. Wall-clock time is what the threshold
# always meant, so it is now what it measures.
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
