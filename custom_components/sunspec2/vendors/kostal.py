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

KOSTAL = VendorProfile(
    slug="kostal",
    # The inverters report "KOSTAL"; the prefix match covers whatever
    # a firmware appends to it.
    manufacturer_prefixes=("KOSTAL",),
    raw_blocks=RAW_BLOCKS,
    raw_devices=RAW_DEVICES,
    raw_sensors=RAW_SENSORS,
)
