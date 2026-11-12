"""Constants for SunSpec 2."""

from __future__ import annotations

NAME = "SunSpec 2"
DOMAIN = "sunspec2"
VERSION = "2026.11.1"

ISSUE_URL = "https://github.com/hilman2/ha-sunspec2/issues"

# Platforms
SENSOR = "sensor"
NUMBER = "number"
SWITCH = "switch"
SELECT = "select"
TIME = "time"
BUTTON = "button"
# Every platform is forwarded on every entry. What the write platforms
# build depends on the device, see number.build_specs.
PLATFORMS = [SENSOR, NUMBER, SWITCH, SELECT, TIME, BUTTON]


# Configuration and options
CONF_HOST = "host"
CONF_PORT = "port"
CONF_UNIT_ID = "unit_id"
CONF_PREFIX = "prefix"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_ENABLED_MODELS = "models_enabled"

# Modbus transport of an entry: ``tcp``, or ``rtu`` for a serial RS-485
# line (typically a USB adapter on /dev/ttyUSB0). Entries created before
# the serial transport existed have no CONF_TRANSPORT key, so every
# reader of the key defaults to TRANSPORT_TCP.
CONF_TRANSPORT = "transport"
TRANSPORT_TCP = "tcp"
TRANSPORT_RTU = "rtu"

# Modbus RTU specific config keys.
CONF_SERIAL_PORT = "serial_port"
CONF_BAUDRATE = "baudrate"
CONF_PARITY = "parity"

# The parity settings the serial setup step offers: none and even.
# api._params passes them on to modbus-connection.
PARITY_NONE = "N"
PARITY_EVEN = "E"

# 9600 baud fits every inverter seen so far. The serial setup step
# lets the user change it.
DEFAULT_BAUDRATE = 9600

# Opt-in for the off/on cycle a vendor needs before it takes a new
# value for an export limit or power factor (Fronius, see
# vendors/fronius.py). Off by default: the cycle leaves the inverter
# unlimited for a second, which is not something to do behind the
# user's back (#17).
CONF_REARM_ON_CHANGE = "rearm_on_change"

# The Fronius web interface (fronius_web.py). The password is a form
# field only and never stored; what the entry keeps is the Digest
# secret the login minted from it. The forget flag drops that secret.
CONF_FRONIUS_WEB_PASSWORD = "fronius_web_password"
CONF_FRONIUS_WEB_FORGET = "fronius_web_forget"
CONF_FRONIUS_WEB_TOKEN = "fronius_web_token"

# Step bounds of the export limit Number (number.py). The real step
# comes from the device's own WMaxLimPct_SF scale factor (10 ** SF): at
# SF -1 the inverter reports and accepts 99.5, and a fixed step of 1
# makes the device's own value unenterable in the frontend box. The
# step is clamped to the range from 0.01 % to 1 %.
EXPORT_LIMIT_DEFAULT_STEP_PCT = 1.0
EXPORT_LIMIT_MIN_STEP_PCT = 0.01

# Many PV inverters power their communication board down without DC
# input, which on a domestic roof means every night (#52). The
# integration then sees a plain connect timeout, and the transport
# repair issue it raises names nothing the user could fix.
#
# The coordinator recognises the common case itself from the inverter's
# operating state (see OPERATING_STATE_MODEL_IDS). This option covers
# the devices it cannot recognise: one that drops the link without
# reporting SLEEPING in a poll that was still answered, and one whose
# models have no operating state point (an SMA Tripower X speaking
# model 701 instead of 103).
#
# The option suppresses the transport repair only. A device that
# answers with a Modbus exception, or with something that is not
# SunSpec, is awake and still escalates.
CONF_STANDBY_WHEN_IDLE = "standby_when_idle"

# Lower bound for the poll interval, enforced in the config flow and
# again in the coordinator. Home Assistant's DataUpdateCoordinator
# breaks at both ends of an unbounded interval:
#
#   0   ``update_interval`` stores ``value.total_seconds() if value
#       else None``, and ``timedelta(seconds=0)`` is falsy.
#       ``_schedule_refresh()`` then returns early and polling stops
#       silently and for good. No cycle runs, so no cycle fails,
#       ``consecutive_failed_cycles`` never moves and the entities hold
#       their last value without going unavailable.
#   <0  ``next_refresh`` lands in the past, so ``loop.call_at`` fires
#       immediately, every time.
#
# The bound is 5 s and not 1 s because a model read can include a
# pacing pause as well as a round trip, and because
# INTERVAL_RETRY_DELAY_SECONDS is 5: below that the in-cycle retry sets
# the real pace, not the configured interval.
MIN_SCAN_INTERVAL_SECONDS = 5

# Seconds pysunspec2 sleeps after every model it walks during a scan of
# the model tree. The pause paces the request stream for slow devices
# (a KACO Powador on 100 Mbit is the known one), which is why the delay
# is an option and not removed. A steady-state poll pays no scan delay:
# the model layout is cached and persisted, so a scan runs only on a
# first connect with nothing stored, after a failure that is neither a
# transport nor a transient one, after a firmware or device change, and
# when the cached layout no longer validates. Lowering the delay only
# shortens that rare walk.
CONF_SCAN_DELAY = "scan_delay"
DEFAULT_SCAN_DELAY_SECONDS = 0.5
MIN_SCAN_DELAY_SECONDS = 0.0
MAX_SCAN_DELAY_SECONDS = 2.0

# Seconds api._pace_model_read keeps between the end of one model read
# and the start of the next.
CONF_MODEL_READ_DELAY = "model_read_delay"
DEFAULT_MODEL_READ_DELAY_SECONDS = 0.6
MIN_MODEL_READ_DELAY_SECONDS = 0.0
MAX_MODEL_READ_DELAY_SECONDS = 2.0

# Storage key and version for the persisted SunSpec model layout: the
# base address plus model id, address and length of every block. With
# the layout a new client rebuilds its model objects from three short
# validating reads instead of a scan. That matters most after a Home
# Assistant restart, where the scan would run inside the setup timeout
# on the slowest devices.
#
# The layout has no expiry on purpose. A rescan cannot confirm a
# layout, only replace it, and a rescan is the one operation that can
# silently corrupt it (see the comment on the cache in api.py). The
# layout is kept fresh by re-reading both ends of the chain on every
# connect, not by a clock.
STRUCTURE_STORAGE_VERSION = 1
STRUCTURE_STORAGE_KEY = f"{DOMAIN}.model_structure"
# The model 1 points that say which device the stored layout belongs
# to. "Vr" is left out on purpose: a firmware update changes the version
# string and nothing else about the identity, and treating the update
# as a device swap fails setup in a loop (#49). The version is stored
# next to these points, so the coordinator notices the update and
# answers it with a single rescan of the model tree.
DEVICE_IDENTITY_POINTS = ("Mn", "Md", "SN")

# The points the sensor platform must not build an entity for are
# derived from the write-control specs. A point that is a Number, Switch
# or Select must not also be a read-only sensor, because the two
# disagree during a write and the user cannot tell which one is right.
# A spec with retain_sensor keeps a sensor that shipped before its
# control, so the sensor's automations and history are not orphaned.


def is_excluded_sensor_point(model_id: int, key: str) -> bool:
    """Return True if the sensor platform must not build an entity for this point.

    ``key`` is the flattened models.py key, so a repeating-group point
    arrives as ``group:idx:point``. Only the trailing point name is
    matched.
    """
    # Imported here because every module imports const: a module-level
    # import of write_controls would become a cycle as soon as
    # write_controls imports anything from const.
    from .write_controls import specs_for_model

    specs = specs_for_model(model_id)
    if not specs:
        return False
    point_name = key.rsplit(":", 1)[-1]
    return any(spec.point_name == point_name and not spec.retain_sensor for spec in specs)


# With this option on, every read on the Modbus session also stores its
# raw bytes in api._captured_reads, and the diagnostics dump carries
# them as a reproducible fixture for a bug report. The wrap goes on
# when the client is built, so it covers a scan, the cached-layout
# validation reads and every model read alike. Saving the option
# reloads the entry, and the capture starts with the connect that
# follows.
CONF_CAPTURE_RAW = "capture_raw_registers"

# Peak AC power in kW, the limit of the plausibility filters. The
# filters drop the unrealistic values inverters report at dawn and dusk
# (MW or TWh spikes that poison long-term statistics). With the option
# empty the detected nameplate stands in, see effective_peak_power_kw.
# The limit is used in two ways:
#   * A measured power reading is dropped when it is above the limit
#     times the headroom of its point (MEASURED_POWER_POINT_HEADROOM).
#   * An energy reading is dropped when the delta to the previous value
#     implies a power above the limit times ENERGY_DELTA_SAFETY_FACTOR.
CONF_MAX_AC_POWER_KW = "max_ac_power_kw"

# Safety factor on the largest plausible energy delta derived from the
# peak power. The factor is generous on purpose: the filter is there
# for obvious garbage (MW / TWh spikes), not for legitimate transients
# near the nameplate. The delta is measured over the time since the counter last
# moved (floored at the scan interval), not over one scan interval: an
# inverter that updates its lifetime counter every few minutes reports
# the same value for several polls and then jumps by the whole
# accumulated amount, and that jump is correct (#45).
ENERGY_DELTA_SAFETY_FACTOR = 2.0

# Headroom on the auto-detected nameplate (WRtg of model 120, else WMax
# of model 121) when it stands in for a peak the user did not configure.
# The nameplate is not a hard ceiling: inverters overshoot their
# continuous rating in cold, bright weather, and an installer-set WMax
# can sit below what the hardware does. 1.2 leaves room for that and
# still catches order-of-magnitude garbage. A configured peak is used
# without this factor, because it is a deliberate statement about the
# site.
NAMEPLATE_FILTER_HEADROOM = 1.2


def effective_peak_power_kw(configured_kw: float | None, detected_kw: float | None) -> float | None:
    """Return the peak AC power the plausibility filters work from, in kW.

    The sensor platform applies the value and diagnostics reports it.
    ``None`` means no ceiling and no filtering.
    """
    if configured_kw:
        return float(configured_kw)
    if detected_kw:
        return float(detected_kw) * NAMEPLATE_FILTER_HEADROOM
    return None


# The points the power plausibility filter may touch, and how far above
# the peak AC power each of them may legitimately read. The peak is an
# active power number, and three of the four quantities with a power
# unit are bounded by something else:
#
#   apparent  VA = W / cos phi, and grid codes require operation down to
#             cos phi 0.80 (VDE-AR-N 4105, IEEE 1547 category B), so
#             apparent power legitimately runs above active power.
#   reactive  bounded by apparent power, so it gets the same allowance.
#   dc        DCW is measured before conversion losses, so it always
#             reads above AC output. On a hybrid with a DC-coupled
#             battery the DC side carries AC output and charge power at
#             the same time, which is why DC gets the widest factor.
#
# The table is an allowlist of live measurements, built from the point
# names pysunspec2 ships. A point that is not named here is never
# filtered. That keeps the static ratings and setpoints measured in
# watts out (WRtg, WMax, VARtg, VAMax, VarSet, PMaxLim, LifeTimeMaxOut,
# the model 706 / 712 curve points): gating a nameplate against a limit
# derived from the same nameplate is circular. Leaving an unnamed point
# alone is also the right failure mode for an unknown vendor point. The
# filter exists to catch order-of-magnitude garbage (MW spikes at
# dawn), so an unfiltered point costs nothing, while a wrongly clipped
# one costs the user a sensor.
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
    # neither number is in a register this integration reads (model 160
    # carries no ratings). A factor on the AC rating can therefore only
    # be a garbage ceiling, and the factor has to sit above every real
    # layout: two MPPTs each rated at the full AC power are 2.0x on
    # their own, a DC-coupled battery charges on top of that, and a
    # small AC stage in front of a large DC side (AC rating near the
    # export limit) pushes the ratio further still (#45). 3.0 is above
    # that range and still catches a misread scale factor (10x and up)
    # or a shifted register.
    "DCW": 3.0,
    "InDCW": 3.0,
}


def measured_power_headroom(key: str) -> float | None:
    """Return the headroom factor for a point key, or ``None`` for a point that is not gated.

    ``key`` is the flattened models.py key. Only the trailing point name
    decides: ``module:0:DCW`` is the same physical quantity as ``DCW``.
    """
    return MEASURED_POWER_POINT_HEADROOM.get(key.rsplit(":", 1)[-1])


# The power plausibility filter logs the first rejected read of a run,
# then every Nth, then one line when the sensor comes back. With a
# ceiling set too low, a WARNING per rejection is one line per affected
# sensor per poll for as long as the sun shines (#45).
IMPLAUSIBLE_LOG_EVERY = 20

# After this many consecutive rejected reads the energy plausibility
# filter accepts the new value as the new baseline. Without the limit, a
# legitimate large jump that the time window of
# ENERGY_DELTA_SAFETY_FACTOR cannot explain (a restored baseline with a
# wrong timestamp, a first read that was an outlier below the truth)
# freezes the sensor on its initial value, because lastKnown is not
# updated while the filter rejects.
ENERGY_DELTA_REJECT_RECOVERY_COUNT = 3

# When an update cycle fails on a running integration, the coordinator
# waits this many seconds and retries the cycle once. A single fast
# retry catches most one-shot blips of inverters and Modbus TCP gateways
# before Home Assistant marks the coordinator as failed. The first
# refresh during setup does not retry: its failure raises
# ConfigEntryNotReady and Home Assistant's own backoff takes over.
INTERVAL_RETRY_DELAY_SECONDS = 5

# The rest of a gateway whose profile sets collection_round_seconds: a
# gateway that works through one request at a time and goes on working
# on a request left unanswered. After such a request the gateway rests:
# no entry behind it sends anything until the rest is over, and the
# in-cycle retry is skipped, because the retry would only queue behind
# the request the gateway is still busy with. A connect that times out
# counts the same: the gateway is not taking anything. A refused
# connect does not.
#
# The rest lasts at least GATEWAY_REST_MIN_SECONDS, time for the gateway
# to finish or drop that request, and then ends GATEWAY_REST_SETTLE_SECONDS
# after the start of the gateway's next round. A gateway collecting from
# its devices is slow to answer, and the margin keeps the next attempt out
# of the first minute of the round. Polls that fall into a rest count as
# failed cycles, so STALE_DATA_TOLERANCE_CYCLES runs on the same clock
# as without a rest.
GATEWAY_REST_MIN_SECONDS = 60
GATEWAY_REST_SETTLE_SECONDS = 60

# How long a write waits for the per-gateway lock before giving up.
#
# The wait is bounded by lock hold time times queued waiters, not by the
# scan interval. One hold is dominated by pacing: at the default model
# read delay of 0.6 s a read cycle over 18 model instances spends about
# 10 s in pauses, and a hold that has to scan first pays the scan delay
# (0.5 s) per discovered model on top. Two config entries behind one
# Modbus gateway can therefore queue past 30 s with nothing wrong, and
# a timeout that fires on healthy hardware is worse than none.
#
# 120 s means "something is stuck", not "the gateway is busy". The
# timeout has to exist because Home Assistant has no service-call
# timeout of its own: SLOW_UPDATE_WARNING only fires inside
# async_update_ha_state(force_refresh=True), which CoordinatorEntity
# never reaches because should_poll is False. Without the timeout a
# write behind a stuck lock never returns.
WRITE_LOCK_TIMEOUT_SECONDS = 120

# The entity's `available` property keeps serving the last good value
# for this many consecutive failed update cycles before the entity
# turns "unavailable". With the default 30 s scan interval and the 5 s
# in-cycle retry that rides out roughly three minutes of dropped
# connectivity without a gap in the long-term statistics.
STALE_DATA_TOLERANCE_CYCLES = 5

# A model polled only for its controls is skipped after this many
# Modbus exceptions in a row, with no successful read in between and
# never one read by the current coordinator (#172).
CONTROL_REFUSALS_BEFORE_SKIP = 2

# The SunSpec models that carry the inverter operating state point, and
# the values of that point that mean the inverter is powering itself
# down (#52).
#
# Only the inverter models 101/102/103 (integer plus scale factor) and
# 111/112/113 (float) are listed. They share one enum: 1 OFF,
# 2 SLEEPING, 3 STARTING, 4 MPPT, 5 THROTTLED, 6 SHUTTING_DOWN,
# 7 FAULT, 8 STANDBY.
#
# Model 701 (DER AC Measurement) also has a point called "St" and must
# stay out of this list. Its enum is 0 OFF / 1 ON, so a healthy
# inverter reporting ON would read as OFF here and silence a genuine
# outage.
OPERATING_STATE_MODEL_IDS = (101, 102, 103, 111, 112, 113)
OPERATING_STATE_POINT = "St"
# Only the states the device reaches on its way down. FAULT is not one
# of them: a faulted inverter that then stops answering is the case the
# repair issue exists for.
OPERATING_STATE_LABELS = {
    1: "OFF",
    2: "SLEEPING",
    6: "SHUTTING_DOWN",
    8: "STANDBY",
}
STANDBY_OPERATING_STATES = frozenset(OPERATING_STATE_LABELS)

# How long a previously detected SunSpec model has to stay missing
# before a Repairs issue suggests removing the related device. Generous
# on purpose: an SMA Tripower X12 (cjne/ha-sunspec#202) sometimes stops
# exposing model 714 for hours in low light, and a one-time hiccup
# should not escalate.
#
# The threshold is wall-clock time, not a number of scans: a healthy
# device is never rescanned, so a counter that advances per scan would
# never advance.
STALE_MODEL_TOLERANCE_SECONDS = 600

# Every model the config flow may tick, and what an entry with an
# empty selection falls back to. Measurement models only: the
# write-capable models reach the polled set through the coordinator's
# separate write_model_filter, so a tick the user never made is never
# rendered and never saved.
#
# Both halves of every measurement pair are listed. SunSpec defines the
# inverter and the meter blocks twice, once with integer values and a
# scale factor and once as float: 101 to 103 against 111 to 113, 201 to
# 204 against 211 to 214. A device may serve one half or both, and
# default_models_for() decides which half gets ticked.
DEFAULT_MODELS = set(
    [
        101,
        102,
        103,
        111,
        112,
        113,
        160,
        201,
        202,
        203,
        204,
        211,
        212,
        213,
        214,
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

#: A measurement model's float twin sits ten model numbers up: 103 is
#: the three phase inverter as integers with a scale factor, 113 the
#: same block as float.
FLOAT_TWIN_OFFSET = 10

#: The integer half of each measurement pair.
INTEGER_MEASUREMENT_MODELS: frozenset[int] = frozenset({101, 102, 103, 201, 202, 203, 204})


def default_models_for(available: set[int]) -> set[int]:
    """Return the models to tick for a device reporting ``available``.

    Args:
        available: The model ids found on the device.

    Returns:
        Those of DEFAULT_MODELS the device has, minus the float twin of
        every pair the device serves both halves of.
    """
    # A Kostal PLENTICORE serves 103 and 113, and its meter 203 and
    # 213. Ticking both halves would build a second entity for every
    # reading, same value, different model number in its unique id.
    # The integer half wins because installations older than 2026.10
    # have only that half ticked: they keep their entity ids and their
    # recorder history when the user next saves the options form.
    ticked = DEFAULT_MODELS & available
    return ticked - {model + FLOAT_TWIN_OFFSET for model in INTEGER_MEASUREMENT_MODELS & available}


STARTUP_MESSAGE = f"""
-------------------------------------------------------------------
{NAME}
Version: {VERSION}
This is a custom integration!
If you have any issues with this you need to open an issue here:
{ISSUE_URL}
-------------------------------------------------------------------
"""
