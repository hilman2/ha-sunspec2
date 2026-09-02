"""Kostal: PIKO IQ, PLENTICORE plus, PLENTICORE BI, PLENTICORE G2, G3 and MP G3.

Kostal serves SunSpec and a register set of its own on one port, both
under the same unit id. SunSpec carries models 1, 103, 113, 120, 123,
160, 203 and 802: the inverter both as integers with a scale factor
and as float, the nameplate, the immediate controls, the strings, the
meter and the battery base model. What it leaves out is what this
module reads: the house consumption split by source, the battery
temperature, the battery's energy counters, and which battery is
fitted.

Two settings decide whether any of it answers. Modbus is off from the
factory and is switched on per inverter, and the port is 1502 rather
than 502, with unit id 71. The other is the byte order: Kostal serves
its own 32-bit registers with the low word first (CDAB, the factory
setting) and offers big-endian (ABCD) as an alternative, which no
SunSpec device would use and which this module does not follow. A
device switched to ABCD reads its own registers as nonsense while its
SunSpec models stay correct, because SunSpec fixes the order itself.

The PIKO CI, Kostal's commercial line, is not this profile: it has an
interface description of its own, and matching it here would read its
registers as though they were a Plenticore's. See
``identifies_plenticore``.

Sources: KOSTAL's "Interface description MODBUS (TCP) & SunSpec, PIKO
IQ / PLENTICORE with control information", revision 2.9 of 17.07.2026,
valid from PIKO/PLENTICORE G1 UI 01.30.12092, PLENTICORE G2 SW
02.15, PLENTICORE G3 and MP G3 SW 3.07.00. Register numbers there are
the addresses on the wire, so they are used as they are printed.
"""

from __future__ import annotations

from typing import Any

from .profile import RawBlock
from .profile import RawDevice
from .profile import RawField
from .profile import RawKeepAlive
from .profile import RawNumber
from .profile import RawSensor
from .profile import VendorProfile

#: Kostal's own registers carry the low word first. This is the byte
#: order the inverter ships with; see the module docstring for what
#: happens when someone changes it.
LITTLE = "little"

#: What register 588 says is fitted. A battery Kostal has not got a
#: number for reads as no model on the battery device rather than as a
#: wrong one, and 0 gates the battery blocks away entirely.
BATTERY_TYPES: dict[int, str] = {
    0x0002: "PIKO Battery Li",
    0x0004: "BYD",
    0x0008: "BMZ",
    0x0010: "AXIstorage Li SH",
    0x0040: "LG",
    0x0200: "Pylontech",
    0x0400: "AXIstorage Li SV",
    0x1000: "Dyness",
    0x2000: "VARTA",
    0x4000: "ZYC",
    0x8000: "HELIVOR",
    0x10000: "M-TEC",
}

#: Register 104, the energy manager's own view of where the energy is
#: going. The gaps, 0x01 and 0x04, are listed as "n/a" by Kostal.
ENERGY_MANAGER_STATES: dict[int, str] = {
    0x00: "idle",
    0x02: "emergency_battery_charge",
    0x08: "winter_mode_step_1",
    0x10: "winter_mode_step_2",
}

#: Register 202. The PSSB is the string fuse board.
PSSB_STATES: dict[int, str] = {0: "fuse_fail", 1: "fuse_ok", 0xFF: "unchecked"}

#: Register 208.
BATTERY_READY: dict[int, str] = {0: "not_ready", 1: "ready"}

#: Register 1080. Read-only since revision 2.9: which way the battery
#: takes external commands is set in the inverter's own menu, not over
#: Modbus. Reading it is how a user finds out whether the setpoints of
#: the control registers would do anything at all.
BATTERY_MANAGEMENT_MODES: dict[int, str] = {
    0x00: "none",
    0x01: "digital_io",
    0x02: "modbus",
}

#: Ceiling of the watt entities, well above any inverter Kostal builds
#: for a house. The battery reports what it actually takes in 1076 and
#: 1078, which the two sensors of that name show, but a RawNumber's
#: range is fixed at import and cannot follow it.
MAX_BATTERY_POWER_W = 20000

#: How often the G3 battery limitation is written again while its
#: switch is on. Under the 30 seconds that is the smallest fallback
#: time the inverter accepts in 1288, so the limit holds whatever the
#: user set there.
LIMITATION_REASSERT_SECONDS = 20.0

#: Register 1082, the energy meter the inverter is paired with.
SENSOR_TYPES: dict[int, str] = {
    0x00: "sdm630",
    0x01: "b_control_em300lr",
    0x03: "ksem",
    0x04: "kem_c",
    0x05: "kem_p",
    0x06: "kem_mp_p",
    0xFF: "none",
}


def identifies_plenticore(model: str, option: str, version: str) -> bool:
    """Whether ``Md`` names a device the interface description in the docstring covers.

    Kostal documents its commercial line, the PIKO CI, in a register
    map of its own, and a PIKO CI 50 is a device this tracker has seen
    (#52). Matching on the manufacturer alone would hand it this
    profile and read its registers as though they were a Plenticore's,
    which is how a sensor comes to show a confident wrong number. Any
    Kostal that does not name itself here is left to the generic
    SunSpec entities until someone checks its document.
    """
    name = model.strip().upper()
    return name.startswith("PLENTICORE") or name.startswith("PIKO IQ")


def as_option(value: Any) -> Any:
    """The integer an option map is keyed by, for an enum Kostal puts in a float register.

    The fuse state and the ready flag are declared Float although they
    only ever hold 0, 1 or 255. ``options`` looks its names up by int,
    so the value has to arrive as one.
    """
    return int(value) if isinstance(value, float) else value


RAW_BLOCKS: tuple[RawBlock, ...] = (
    # 98 to 123. The house consumption split by source is the reason
    # this block exists: no SunSpec model carries it, and it is what
    # the inverter's own portal shows.
    RawBlock(
        key="inverter",
        address=98,
        count=26,
        word_order=LITTLE,
        fields=(
            RawField("controller_temperature", 0, "float32"),
            RawField("total_dc_power", 2, "float32"),
            RawField("energy_manager_state", 6, "uint32"),
            RawField("home_consumption_battery", 8, "float32"),
            RawField("home_consumption_grid", 10, "float32"),
            RawField("home_consumption_battery_total", 12, "float32"),
            RawField("home_consumption_grid_total", 14, "float32"),
            RawField("home_consumption_pv_total", 16, "float32"),
            RawField("home_consumption_pv", 18, "float32"),
            RawField("home_consumption_total", 20, "float32"),
            RawField("isolation_resistance", 22, "float32"),
            RawField("power_limit_evu", 24, "float32"),
        ),
    ),
    # 512 to 589. Wide because the battery's identity is spread across
    # it with the inverter's strings in between; one request is still
    # cheaper than three. ``battery_type`` is what says a battery is
    # there at all, so this block is never gated.
    RawBlock(
        key="battery_info",
        address=512,
        count=78,
        word_order=LITTLE,
        fields=(
            RawField("gross_capacity", 0, "uint32"),
            RawField("manufacturer", 5, "string", 8),
            RawField("serial", 15, "uint32"),
            RawField("firmware", 74, "uint32"),
            RawField("battery_type", 76, "uint16"),
        ),
    ),
    # 202 to 215. The temperature is the point of it: SunSpec's battery
    # base model 802 has no temperature field at all.
    RawBlock(
        key="battery_state",
        address=202,
        count=14,
        word_order=LITTLE,
        gate=("battery_info", "battery_type"),
        fields=(
            RawField("pssb_fuse_state", 0, "float32"),
            RawField("ready_flag", 6, "float32"),
            RawField("temperature", 12, "float32"),
        ),
    ),
    # 1046 to 1065. Five counters that separate where the battery's
    # energy came from and went to, which model 802 does not do, plus
    # the meter's own export total.
    RawBlock(
        key="battery_energy",
        address=1046,
        count=20,
        word_order=LITTLE,
        gate=("battery_info", "battery_type"),
        fields=(
            RawField("dc_charge_energy", 0, "float32"),
            RawField("dc_discharge_energy", 2, "float32"),
            RawField("ac_charge_energy", 4, "float32"),
            RawField("ac_discharge_energy", 6, "float32"),
            RawField("ac_charge_energy_grid", 8, "float32"),
            RawField("energy_to_grid", 18, "float32"),
        ),
    ),
    # 1034 to 1045, the writable end of the external battery
    # management. The inverter only acts on the setpoint while its own
    # menu has external battery management set to Modbus, which is
    # what the management_mode sensor below reports; the two SoC
    # bounds and the two power limits apply either way.
    RawBlock(
        key="battery_control",
        address=1034,
        count=12,
        word_order=LITTLE,
        gate=("battery_info", "battery_type"),
        fields=(
            RawField("dc_power_setpoint", 0, "float32"),
            RawField("max_charge_power_limit", 4, "float32"),
            RawField("max_discharge_power_limit", 6, "float32"),
            RawField("min_soc", 8, "float32"),
            RawField("max_soc", 10, "float32"),
        ),
    ),
    # 1280 to 1289, PLENTICORE G3 from SW 03.05 only. Older inverters
    # answer a Modbus exception here and the block is marked absent,
    # which is why the limits above and these are not one block.
    RawBlock(
        key="battery_limitation",
        address=1280,
        count=10,
        word_order=LITTLE,
        gate=("battery_info", "battery_type"),
        fields=(
            RawField("max_charge_power", 0, "float32"),
            RawField("max_discharge_power", 2, "float32"),
            RawField("fallback_charge_power", 4, "float32"),
            RawField("fallback_discharge_power", 6, "float32"),
            RawField("fallback_seconds", 8, "uint32"),
        ),
    ),
    # 1068 to 1083, the read-only end of the external battery
    # management. The limits here are what the battery reports it can
    # take, not what anyone set.
    RawBlock(
        key="battery_limits",
        address=1068,
        count=16,
        word_order=LITTLE,
        gate=("battery_info", "battery_type"),
        fields=(
            RawField("work_capacity", 0, "float32"),
            RawField("max_charge_power", 8, "float32"),
            RawField("max_discharge_power", 10, "float32"),
            RawField("management_mode", 12, "uint16"),
            RawField("sensor_type", 14, "uint16"),
        ),
    ),
)

RAW_DEVICES: tuple[RawDevice, ...] = (
    RawDevice(
        key="battery",
        info_block="battery_info",
        name="Battery",
        manufacturer="manufacturer",
        model="battery_type",
        model_options=BATTERY_TYPES,
        serial="serial",
        # battery_limits is gated on the battery type, so it answers
        # only where a battery is fitted. Without it the identity block
        # alone would build a battery device on every PV-only inverter.
        requires="battery_limits",
    ),
)


def _battery(field: str, key: str, **kwargs: Any) -> RawSensor:
    return RawSensor(block="battery_state", field=field, key=key, device="battery", **kwargs)


RAW_SENSORS: tuple[RawSensor, ...] = (
    RawSensor(
        block="inverter",
        field="home_consumption_pv",
        key="home_consumption_pv",
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:home-lightning-bolt",
    ),
    RawSensor(
        block="inverter",
        field="home_consumption_battery",
        key="home_consumption_battery",
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:home-battery",
    ),
    RawSensor(
        block="inverter",
        field="home_consumption_grid",
        key="home_consumption_grid",
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:home-import-outline",
    ),
    RawSensor(
        block="inverter",
        field="home_consumption_pv_total",
        key="home_consumption_pv_total",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    RawSensor(
        block="inverter",
        field="home_consumption_battery_total",
        key="home_consumption_battery_total",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    RawSensor(
        block="inverter",
        field="home_consumption_grid_total",
        key="home_consumption_grid_total",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    RawSensor(
        block="inverter",
        field="home_consumption_total",
        key="home_consumption_total",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    RawSensor(
        block="inverter",
        field="total_dc_power",
        key="total_dc_power",
        unit="W",
        device_class="power",
        state_class="measurement",
        icon="mdi:solar-power-variant",
    ),
    RawSensor(
        block="inverter",
        field="energy_manager_state",
        key="energy_manager_state",
        device_class="enum",
        options=ENERGY_MANAGER_STATES,
        diagnostic=True,
        icon="mdi:state-machine",
    ),
    RawSensor(
        block="inverter",
        field="controller_temperature",
        key="controller_temperature",
        unit="°C",
        device_class="temperature",
        state_class="measurement",
        diagnostic=True,
    ),
    RawSensor(
        block="inverter",
        field="isolation_resistance",
        key="isolation_resistance",
        unit="Ω",
        state_class="measurement",
        diagnostic=True,
        icon="mdi:omega",
    ),
    # What the grid operator currently allows, in percent of nameplate.
    # A PV system under a feed-in limit spends its sunny hours here.
    RawSensor(
        block="inverter",
        field="power_limit_evu",
        key="power_limit_evu",
        unit="%",
        state_class="measurement",
        diagnostic=True,
        icon="mdi:transmission-tower",
    ),
    RawSensor(
        block="battery_limits",
        field="sensor_type",
        key="energy_meter_type",
        device_class="enum",
        options=SENSOR_TYPES,
        diagnostic=True,
        icon="mdi:meter-electric",
    ),
    RawSensor(
        block="battery_energy",
        field="energy_to_grid",
        key="energy_to_grid",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _battery(
        "temperature",
        "battery_temperature",
        unit="°C",
        device_class="temperature",
        state_class="measurement",
    ),
    _battery(
        "pssb_fuse_state",
        "pssb_fuse_state",
        device_class="enum",
        options=PSSB_STATES,
        transform=as_option,
        diagnostic=True,
        icon="mdi:fuse",
    ),
    _battery(
        "ready_flag",
        "battery_ready",
        device_class="enum",
        options=BATTERY_READY,
        transform=as_option,
        diagnostic=True,
        icon="mdi:battery-check",
    ),
    RawSensor(
        block="battery_energy",
        field="dc_charge_energy",
        key="battery_dc_charge_energy",
        device="battery",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    RawSensor(
        block="battery_energy",
        field="dc_discharge_energy",
        key="battery_dc_discharge_energy",
        device="battery",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    RawSensor(
        block="battery_energy",
        field="ac_charge_energy",
        key="battery_ac_charge_energy",
        device="battery",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    RawSensor(
        block="battery_energy",
        field="ac_discharge_energy",
        key="battery_ac_discharge_energy",
        device="battery",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    # Grid to battery, the one counter that says whether the battery
    # was charged from the grid rather than from the roof.
    RawSensor(
        block="battery_energy",
        field="ac_charge_energy_grid",
        key="battery_ac_charge_energy_grid",
        device="battery",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    RawSensor(
        block="battery_limits",
        field="work_capacity",
        key="battery_work_capacity",
        device="battery",
        unit="Wh",
        device_class="energy_storage",
        diagnostic=True,
    ),
    RawSensor(
        block="battery_limits",
        field="max_charge_power",
        key="battery_max_charge_power",
        device="battery",
        unit="W",
        device_class="power",
        diagnostic=True,
    ),
    RawSensor(
        block="battery_limits",
        field="max_discharge_power",
        key="battery_max_discharge_power",
        device="battery",
        unit="W",
        device_class="power",
        diagnostic=True,
    ),
    RawSensor(
        block="battery_limits",
        field="management_mode",
        key="battery_management_mode",
        device="battery",
        device_class="enum",
        options=BATTERY_MANAGEMENT_MODES,
        diagnostic=True,
        icon="mdi:battery-sync",
    ),
)


def _control(field: str, key: str, **kwargs: Any) -> RawNumber:
    return RawNumber(block="battery_control", field=field, key=key, device="battery", **kwargs)


def _limitation(field: str, key: str, **kwargs: Any) -> RawNumber:
    return RawNumber(block="battery_limitation", field=field, key=key, device="battery", **kwargs)


RAW_NUMBERS: tuple[RawNumber, ...] = (
    # The pair a user reaches for first: how much of the battery to
    # keep back, and how full to let it get.
    _control("min_soc", "battery_min_soc", min=0, max=100, unit="%", icon="mdi:battery-low"),
    _control("max_soc", "battery_max_soc", min=0, max=100, unit="%", icon="mdi:battery-high"),
    _control(
        "max_charge_power_limit",
        "battery_max_charge_power_limit",
        min=0,
        max=MAX_BATTERY_POWER_W,
        step=100,
        unit="W",
        device_class="power",
        icon="mdi:battery-arrow-up",
    ),
    _control(
        "max_discharge_power_limit",
        "battery_max_discharge_power_limit",
        min=0,
        max=MAX_BATTERY_POWER_W,
        step=100,
        unit="W",
        device_class="power",
        icon="mdi:battery-arrow-down",
    ),
    # Behind the beta, and the only entity here that drives the battery
    # rather than bounding it. Kostal names no timeout for this
    # register, so a value written here is one the inverter keeps: an
    # automation that sets it and stops leaves the battery where it
    # put it. Negative charges, positive discharges, which is Kostal's
    # sign convention and the opposite of model 124's.
    _control(
        "dc_power_setpoint",
        "battery_dc_power_setpoint",
        min=-MAX_BATTERY_POWER_W,
        max=MAX_BATTERY_POWER_W,
        step=100,
        unit="W",
        device_class="power",
        beta=True,
        icon="mdi:battery-charging-medium",
    ),
    _limitation(
        "max_charge_power",
        "battery_limitation_charge_power",
        min=0,
        max=MAX_BATTERY_POWER_W,
        step=100,
        unit="W",
        device_class="power",
        icon="mdi:battery-arrow-up-outline",
    ),
    _limitation(
        "max_discharge_power",
        "battery_limitation_discharge_power",
        min=0,
        max=MAX_BATTERY_POWER_W,
        step=100,
        unit="W",
        device_class="power",
        icon="mdi:battery-arrow-down-outline",
    ),
    _limitation(
        "fallback_charge_power",
        "battery_fallback_charge_power",
        min=0,
        max=MAX_BATTERY_POWER_W,
        step=100,
        unit="W",
        device_class="power",
        icon="mdi:battery-alert-variant-outline",
    ),
    _limitation(
        "fallback_discharge_power",
        "battery_fallback_discharge_power",
        min=0,
        max=MAX_BATTERY_POWER_W,
        step=100,
        unit="W",
        device_class="power",
        icon="mdi:battery-alert-variant-outline",
    ),
    # The inverter's own range. Below 30 it refuses the write.
    _limitation(
        "fallback_seconds",
        "battery_fallback_time",
        min=30,
        max=10800,
        step=10,
        unit="s",
        icon="mdi:timer-sand",
    ),
)

RAW_KEEPALIVES: tuple[RawKeepAlive, ...] = (
    # Kostal requires the two limitation registers to be written again
    # and again once they have been written at all: stop, and after
    # the fallback time the fallback pair takes over. That makes the
    # switch part of the feature rather than a convenience, and it is
    # also what makes the feature safe. A Home Assistant that goes
    # down hands the battery back to the inverter on its own.
    RawKeepAlive(
        key="battery_limitation_hold",
        block="battery_limitation",
        fields=("max_charge_power", "max_discharge_power"),
        interval_seconds=LIMITATION_REASSERT_SECONDS,
        device="battery",
        icon="mdi:battery-sync-outline",
    ),
)

KOSTAL = VendorProfile(
    slug="kostal",
    # The inverters report "KOSTAL"; the prefix match covers whatever
    # a firmware appends to it. Which of them this profile is for is
    # then decided on the model name, see identifies_plenticore.
    manufacturer_prefixes=("KOSTAL",),
    identifies=identifies_plenticore,
    raw_blocks=RAW_BLOCKS,
    raw_devices=RAW_DEVICES,
    raw_sensors=RAW_SENSORS,
    raw_numbers=RAW_NUMBERS,
    raw_keepalives=RAW_KEEPALIVES,
    # 1280 to 1289 are the registers Kostal asks to be written on a
    # timer, so they cannot be the flash-backed kind; nothing would
    # survive a limit rewritten every twenty seconds. The rest of the
    # profile's writes are counted as flash writes, which is the
    # cautious reading where the document says nothing.
    volatile_registers=frozenset(range(1280, 1290)),
)
