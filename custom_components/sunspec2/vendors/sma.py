"""SMA: Sunny Boy, Sunny Tripower, Sunny Boy Storage, Sunny Island, the ennexOS generation.

SMA serves two Modbus profiles on one port. SunSpec answers on the
SMA unit id plus 123, so 126 with the factory unit id of 3, and that
is where the integration reads the models: 1, 101 to 103, 120 to 123
and 160 on the Speedwire generation (Sunny Boy -41, Tripower -40,
Core1, Highpower), 1, 123 and 701 to 714 on the ennexOS generation
(Tripower X, Smart Energy, Sunny Island X, Core2), plus 124 on Sunny
Boy Storage and Sunny Island. Everything else SMA keeps in its own
profile, 123 unit ids below: the battery, the device identity, the
operating status, and the external active power setpoint. This
module says where.

Sources: SMA's technical information "SunSpec Modbus" (v1.1) and
"SMA Modbus" (v1.0, 2023) with the SMA_Modbus-TI-en-14 register map,
the SMA per-device register lists, and the register knowledge the
evcc templates, the Optic00/ha-modbus-akku-adapter reference and the
photovoltaikforum threads carry. Register numbers in the SMA
documents are one-based; the addresses here are one less.

What the documents say and the users confirm: RW parameters wear the
flash when written on a timer, and the only registers SMA allows to
write cyclically are the setpoints, which read back nothing. The
external setpoint (40151 active, 40149 watts, negative charges) works
on Sunny Island, Sunny Boy Storage and Tripower Smart Energy once the
operating mode "external setpoint" (1079) has been set in the
inverter, lapses after the device's own timeout (five seconds to nine
hours, ten to thirty minutes from the factory), and wants a second
or more between the flag and the value; 500 ms cycles have hung an
inverter until a power cycle. Sunny Boy Smart Energy accepts the
writes and ignores them on the firmware seen so far.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .profile import RawBlock
from .profile import RawField
from .profile import RawKeepAlive
from .profile import RawNumber
from .profile import RawSelect
from .profile import RawSensor
from .profile import VendorProfile

#: SunSpec answers 123 unit ids above SMA's own profile.
SUNSPEC_UNIT_ID_DISTANCE = 123
#: What SunSpec sits on with SMA's factory unit id of 3.
SUNSPEC_UNIT_ID = 126
#: The blocks below live on SMA's own profile.
SMA_PROFILE = -SUNSPEC_UNIT_ID_DISTANCE

#: The value SMA puts in a status register for "information not
#: available", in the low 24 bits it uses for tags.
TAG_NOT_AVAILABLE = 0xFFFFFD

DEVICE_CLASSES: dict[int, str] = {
    8001: "pv_inverter",
    8002: "wind_inverter",
    8007: "battery_inverter",
    8033: "load",
    8064: "sensor",
    8065: "energy_meter",
    8128: "communication",
}

OPERATING_STATUS: dict[int, str] = {35: "fault", 303: "off", 307: "ok", 455: "warning"}

#: "Operating mode active power": what steers the inverter's power.
ACTIVE_POWER_MODES: dict[int, str] = {
    303: "off",
    1077: "watts",
    1078: "percent",
    1079: "external_setpoint",
}

#: The external setpoint's on switch.
POWER_CONTROL: dict[int, str] = {803: "inactive", 802: "active"}
POWER_CONTROL_ACTIVE = 802

#: How often the setpoint is written again while its switch is on.
#: Well inside the shortest factory timeout; users run 15 to 60 s.
SETPOINT_KEEPALIVE_SECONDS = 30.0


def address(register: int) -> int:
    """The Modbus address of an SMA register number."""
    return register - 1


def sma_tag(value: Any) -> Any:
    """A status tag, or None for SMA's "not available" marker."""
    if not isinstance(value, int):
        return None
    tag = value & 0xFFFFFF
    return None if tag == TAG_NOT_AVAILABLE else tag


def _sma_block(key: str, register: int, count: int, *fields: RawField, **kwargs: Any) -> RawBlock:
    return RawBlock(
        key=key,
        address=address(register),
        count=count,
        fields=fields,
        unit_id_offset=SMA_PROFILE,
        **kwargs,
    )


RAW_BLOCKS: tuple[RawBlock, ...] = (
    _sma_block(
        "sma_device",
        30051,
        8,
        RawField("device_class", 0, "uint32"),
        RawField("device_type", 2, "uint32"),
        RawField("vendor", 4, "uint32"),
        RawField("serial", 6, "uint32"),
    ),
    _sma_block("sma_status", 30201, 2, RawField("status", 0, "uint32")),
    _sma_block("sma_power_mode", 30835, 2, RawField("mode", 0, "uint32")),
    _sma_block(
        "sma_battery",
        30843,
        10,
        RawField("current", 0, "int32", scale=0.001),
        RawField("state_of_charge", 2, "uint32"),
        RawField("capacity", 4, "uint32"),
        RawField("temperature", 6, "int32", scale=0.1),
        RawField("voltage", 8, "uint32", scale=0.01),
    ),
    _sma_block("sma_battery_status", 30955, 2, RawField("status", 0, "uint32")),
    _sma_block(
        "sma_battery_power",
        31393,
        12,
        RawField("charge_power", 0, "uint32"),
        RawField("discharge_power", 2, "uint32"),
        RawField("energy_charged", 4, "uint64"),
        RawField("energy_discharged", 8, "uint64"),
        gate=("sma_battery", "voltage"),
    ),
    _sma_block(
        "sma_battery_nameplate",
        40187,
        2,
        RawField("rated_energy", 0, "uint32"),
        gate=("sma_battery", "voltage"),
    ),
    _sma_block("sma_setpoint_timeout", 41195, 2, RawField("timeout", 0, "uint32")),
    # The external setpoint: written, never read. What the entities
    # show is what Home Assistant last wrote.
    _sma_block(
        "sma_setpoint",
        40149,
        4,
        RawField("setpoint", 0, "int32"),
        RawField("control", 2, "uint32"),
        readable=False,
    ),
)


def _sensor(block: str, field: str, key: str, **kwargs: Any) -> RawSensor:
    return RawSensor(block=block, field=field, key=key, **kwargs)


RAW_SENSORS: tuple[RawSensor, ...] = (
    _sensor(
        "sma_device",
        "device_class",
        "sma_device_class",
        device_class="enum",
        options=DEVICE_CLASSES,
        diagnostic=True,
        icon="mdi:shape-outline",
    ),
    _sensor("sma_device", "device_type", "sma_device_type", diagnostic=True, icon="mdi:barcode"),
    _sensor(
        "sma_status",
        "status",
        "sma_operating_status",
        device_class="enum",
        options=OPERATING_STATUS,
        transform=sma_tag,
        icon="mdi:state-machine",
    ),
    _sensor(
        "sma_power_mode",
        "mode",
        "sma_active_power_mode",
        device_class="enum",
        options=ACTIVE_POWER_MODES,
        transform=sma_tag,
        diagnostic=True,
        icon="mdi:speedometer",
    ),
    _sensor(
        "sma_battery",
        "state_of_charge",
        "battery_state_of_charge",
        unit="%",
        device_class="battery",
        state_class="measurement",
    ),
    _sensor(
        "sma_battery",
        "capacity",
        "battery_capacity_pct",
        unit="%",
        state_class="measurement",
        diagnostic=True,
        icon="mdi:battery-heart-variant",
    ),
    _sensor(
        "sma_battery",
        "temperature",
        "battery_temperature",
        unit="°C",
        device_class="temperature",
        state_class="measurement",
    ),
    _sensor(
        "sma_battery",
        "voltage",
        "battery_voltage",
        unit="V",
        device_class="voltage",
        state_class="measurement",
    ),
    _sensor(
        "sma_battery",
        "current",
        "battery_current",
        unit="A",
        device_class="current",
        state_class="measurement",
    ),
    _sensor(
        "sma_battery_status",
        "status",
        "battery_status_code",
        transform=sma_tag,
        diagnostic=True,
        icon="mdi:battery-unknown",
    ),
    _sensor(
        "sma_battery_power",
        "charge_power",
        "battery_charge_power",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    _sensor(
        "sma_battery_power",
        "discharge_power",
        "battery_discharge_power",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    _sensor(
        "sma_battery_power",
        "energy_charged",
        "battery_energy_charged",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _sensor(
        "sma_battery_power",
        "energy_discharged",
        "battery_energy_discharged",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _sensor(
        "sma_battery_nameplate",
        "rated_energy",
        "battery_rated_energy",
        unit="Wh",
        device_class="energy_storage",
        diagnostic=True,
    ),
    _sensor(
        "sma_setpoint_timeout",
        "timeout",
        "sma_setpoint_timeout",
        unit="s",
        device_class="duration",
        diagnostic=True,
        icon="mdi:timer-sand",
    ),
)

RAW_SELECTS: tuple[RawSelect, ...] = (
    RawSelect(
        block="sma_setpoint",
        field="control",
        key="sma_power_control",
        options=POWER_CONTROL,
        icon="mdi:remote",
    ),
)

RAW_NUMBERS: tuple[RawNumber, ...] = (
    RawNumber(
        block="sma_setpoint",
        field="setpoint",
        key="sma_power_setpoint",
        min=-100000,
        max=100000,
        step=10,
        unit="W",
        device_class="power",
        icon="mdi:battery-arrow-up-outline",
    ),
)


def _control_active(data: Mapping[str, Any]) -> bool:
    return data.get("control") == POWER_CONTROL_ACTIVE


RAW_KEEPALIVES: tuple[RawKeepAlive, ...] = (
    RawKeepAlive(
        key="sma_power_setpoint_keepalive",
        block="sma_setpoint",
        fields=("control", "setpoint"),
        interval_seconds=SETPOINT_KEEPALIVE_SECONDS,
        only_while=_control_active,
        # The flag a moment before the value; SMA asks for a second
        # between transfers.
        settle_seconds=1.0,
        icon="mdi:refresh-auto",
    ),
)

SMA = VendorProfile(
    slug="sma",
    manufacturer_prefixes=("SMA",),
    raw_blocks=RAW_BLOCKS,
    raw_sensors=RAW_SENSORS,
    raw_selects=RAW_SELECTS,
    raw_numbers=RAW_NUMBERS,
    raw_keepalives=RAW_KEEPALIVES,
    # The setpoints are what SMA lets a controller write cyclically;
    # nothing else this profile writes.
    volatile_registers=frozenset({address(40149), address(40151)}),
)
