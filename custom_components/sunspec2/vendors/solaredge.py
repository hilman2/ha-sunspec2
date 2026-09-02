"""SolarEdge: HD-Wave, StorEdge, Energy Hub, Home Hub and Synergy inverters.

SolarEdge implements the SunSpec models for reading, common model 1,
the inverter models 101 to 103, model 160 on Synergy units and the
meter models 201 to 204, each meter with a common block of its own. It
implements nothing of SunSpec for writing and nothing of SunSpec for
a battery. All of that sits in registers of its own, and this module
says where.

Sources: the SolarEdge technical note "SunSpec Logging" v3.2 (June
2025) for the models and the Modbus TCP rules, the technical note
"Power Control Protocol for SolarEdge Inverters" v1.3 (2017) for the
battery, storage control and power control registers, and the
WillCodeForCats/solaredge-modbus-multi integration (Apache-2.0), whose
users have driven these registers since 2021. Every 32-bit value in
the SolarEdge blocks carries its low word first; the SunSpec models do
not.

What the documents do not say and the users found: the battery,
storage control and site limit blocks are one feature set that
SolarEdge support switches on per inverter, with no pattern to which
inverters have it. An inverter without it answers a Modbus exception
on old firmware and, from firmware 4.23, nothing at all. Batteries are
recognised by a rated energy above zero, because firmware 4.18 stopped
filling the model string and shows a phantom second battery with
strings but no energy. The inverter restarts once a night, refuses TCP
while it does, and accepts one Modbus TCP session at a time, refusing
a second and holding a dropped one for two minutes.
"""

from __future__ import annotations

from typing import Any

from .profile import RawBlock
from .profile import RawDevice
from .profile import RawField
from .profile import RawKeepAlive
from .profile import RawNumber
from .profile import RawSelect
from .profile import RawSensor
from .profile import VendorProfile

LITTLE = "little"

#: The battery blocks: slot to first register of the identity block.
#: The data block follows 68 registers later. The stride is not even:
#: 256 between the first two slots, 512 to the third.
BATTERY_BASE: dict[int, int] = {1: 0xE100, 2: 0xE200, 3: 0xE400}

BATTERY_STATUS: dict[int, str] = {
    0: "off",
    1: "standby",
    2: "init",
    3: "charge",
    4: "discharge",
    5: "fault",
    6: "preserve_charge",
    7: "idle",
    10: "power_saving",
}

GRID_STATUS: dict[int, str] = {0: "on_grid", 1: "off_grid"}

#: Bits 0 to 2 of the site limit mode register, one at a time.
SITE_LIMIT_MODES: dict[int, str] = {
    0: "disabled",
    1: "export_control_export_import_meter",
    2: "export_control_consumption_meter",
    4: "production_control",
}

STORAGE_CONTROL_MODES: dict[int, str] = {
    0: "disabled",
    1: "maximize_self_consumption",
    2: "time_of_use",
    3: "backup_only",
    4: "remote_control",
}

#: The value of the remote control's command and default mode
#: registers. 6 does not exist.
REMOTE_MODES: dict[int, str] = {
    0: "solar_power_only",
    1: "charge_from_clipped_solar",
    2: "charge_from_solar",
    3: "charge_from_solar_and_grid",
    4: "discharge_to_maximize_export",
    5: "discharge_to_minimize_import",
    7: "maximize_self_consumption",
}

AC_CHARGE_POLICIES: dict[int, str] = {
    0: "disabled",
    1: "always_allowed",
    2: "fixed_energy_limit",
    3: "percent_of_production",
}

SITE_LIMIT_SCOPES: dict[int, str] = {0: "total", 1: "per_phase"}

REMOTE_CONTROL = 4

#: How often the remote command is written again while its switch is
#: on. The community's users settled on 15 minutes with an hour of
#: timeout; more often "chokes the modbus".
REMOTE_COMMAND_REARM_SECONDS = 900.0

#: How often the active power limit is written again while its switch
#: is on. Firmware has been seen to revert it after ten seconds.
ACTIVE_POWER_LIMIT_REASSERT_SECONDS = 10.0


def _keep_site_limit_bits(current: Any, chosen: int) -> int:
    """The chosen limit mode in bits 0 to 2, the other bits as they are."""
    other = (current & ~0b111) if isinstance(current, int) else 0
    return other | chosen


def _remote_control_active(data: Any) -> bool:
    return isinstance(data, dict) and data.get("control_mode") == REMOTE_CONTROL


def site_limit_mode(value: Any) -> str | None:
    """The one of the three limit modes set in the bitfield, or None."""
    if not isinstance(value, int):
        return None
    return SITE_LIMIT_MODES.get(value & 0b111)


def status_vendor4(value: Any) -> str | None:
    """The vendor status the way SetApp shows it: controller and error code in hex.

    Firmware 3.20 and later report a 32-bit word at ``EvtVnd4`` of the
    inverter model: bits 31 to 24 name the controller, bits 15 to 0 the
    error code. SetApp prints them as ``18xBF``; so does this.
    """
    if not isinstance(value, int):
        return None
    controller = (value >> 24) & 0xFF
    code = value & 0xFFFF
    return f"{controller:X}x{code:X}"


def _battery_blocks(slot: int) -> tuple[RawBlock, RawBlock]:
    base = BATTERY_BASE[slot]
    info = RawBlock(
        key=f"battery_{slot}_info",
        address=base,
        count=68,
        word_order=LITTLE,
        fields=(
            RawField("manufacturer", 0, "string", 16),
            RawField("model", 16, "string", 16),
            RawField("version", 32, "string", 16),
            RawField("serial", 48, "string", 16),
            RawField("device_address", 64, "uint16"),
            RawField("rated_energy", 66, "float32"),
        ),
    )
    data = RawBlock(
        key=f"battery_{slot}",
        address=base + 68,
        count=86,
        word_order=LITTLE,
        gate=(info.key, "rated_energy"),
        fields=(
            RawField("max_charge_power", 0, "float32"),
            RawField("max_discharge_power", 2, "float32"),
            RawField("max_charge_peak_power", 4, "float32"),
            RawField("max_discharge_peak_power", 6, "float32"),
            RawField("temperature_average", 40, "float32"),
            RawField("temperature_max", 42, "float32"),
            RawField("voltage", 44, "float32"),
            RawField("current", 46, "float32"),
            RawField("power", 48, "float32"),
            RawField("energy_exported", 50, "uint64"),
            RawField("energy_imported", 54, "uint64"),
            RawField("energy_max", 58, "float32"),
            RawField("energy_available", 60, "float32"),
            RawField("state_of_health", 62, "float32"),
            RawField("state_of_energy", 64, "float32"),
            RawField("status", 66, "uint32"),
            RawField("status_vendor", 68, "uint32"),
        ),
    )
    return info, data


def _battery_sensors(slot: int) -> tuple[RawSensor, ...]:
    device = f"battery_{slot}"
    info = f"battery_{slot}_info"

    def sensor(field: str, key: str, **kwargs: Any) -> RawSensor:
        return RawSensor(block=device, field=field, key=key, device=device, **kwargs)

    return (
        sensor(
            "state_of_energy",
            "battery_state_of_energy",
            unit="%",
            device_class="battery",
            state_class="measurement",
        ),
        sensor(
            "state_of_health",
            "battery_state_of_health",
            unit="%",
            state_class="measurement",
            diagnostic=True,
            icon="mdi:battery-heart-variant",
        ),
        sensor(
            "temperature_average",
            "battery_temperature",
            unit="°C",
            device_class="temperature",
            state_class="measurement",
        ),
        sensor(
            "temperature_max",
            "battery_temperature_max",
            unit="°C",
            device_class="temperature",
            state_class="measurement",
            diagnostic=True,
        ),
        sensor(
            "voltage",
            "battery_voltage",
            unit="V",
            device_class="voltage",
            state_class="measurement",
        ),
        sensor(
            "current",
            "battery_current",
            unit="A",
            device_class="current",
            state_class="measurement",
        ),
        sensor("power", "battery_power", unit="W", device_class="power", state_class="measurement"),
        sensor(
            "energy_exported",
            "battery_energy_discharged",
            unit="Wh",
            device_class="energy",
            state_class="total_increasing",
        ),
        sensor(
            "energy_imported",
            "battery_energy_charged",
            unit="Wh",
            device_class="energy",
            state_class="total_increasing",
        ),
        sensor(
            "energy_available",
            "battery_energy_available",
            unit="Wh",
            device_class="energy_storage",
            state_class="measurement",
        ),
        sensor(
            "energy_max",
            "battery_energy_max",
            unit="Wh",
            device_class="energy_storage",
            state_class="measurement",
            diagnostic=True,
        ),
        RawSensor(
            block=info,
            field="rated_energy",
            key="battery_rated_energy",
            device=device,
            unit="Wh",
            device_class="energy_storage",
            diagnostic=True,
        ),
        sensor(
            "max_charge_power",
            "battery_max_charge_power",
            unit="W",
            device_class="power",
            diagnostic=True,
        ),
        sensor(
            "max_discharge_power",
            "battery_max_discharge_power",
            unit="W",
            device_class="power",
            diagnostic=True,
        ),
        sensor("status", "battery_status", device_class="enum", options=BATTERY_STATUS),
    )


RAW_BLOCKS: tuple[RawBlock, ...] = (
    # The inverter's grid status: not in any document, from a SolarEdge
    # manager's mail to the community integration (its discussion 618).
    # These are the EvtVnd1 registers of the inverter model, read here
    # in SolarEdge's word order.
    RawBlock(
        key="grid_status",
        address=40113,
        count=2,
        word_order=LITTLE,
        fields=(RawField("grid_status", 0, "uint32"),),
    ),
    # EvtVnd4 of the inverter model, which firmware 3.20 and later fill
    # with the controller and error code SetApp shows.
    RawBlock(
        key="status_vendor4",
        address=40119,
        count=2,
        fields=(RawField("status_vendor4", 0, "uint32"),),
    ),
    RawBlock(
        key="site_limit",
        address=0xE000,
        count=4,
        word_order=LITTLE,
        fields=(
            RawField("mode", 0, "uint16"),
            RawField("limit_mode", 1, "uint16"),
            RawField("site_limit", 2, "float32"),
        ),
    ),
    RawBlock(
        key="storage_control",
        address=0xE004,
        count=14,
        word_order=LITTLE,
        fields=(
            RawField("control_mode", 0, "uint16"),
            RawField("ac_charge_policy", 1, "uint16"),
            RawField("ac_charge_limit", 2, "float32"),
            RawField("backup_reserve", 4, "float32"),
            RawField("default_mode", 6, "uint16"),
            RawField("command_timeout", 7, "uint32"),
            RawField("command_mode", 9, "uint16"),
            RawField("charge_limit", 10, "float32"),
            RawField("discharge_limit", 12, "float32"),
        ),
    ),
    # The global dynamic power control: the RRCR input state and the
    # two setpoints the inverter keeps in RAM and applies at once.
    RawBlock(
        key="power_control",
        address=0xF000,
        count=4,
        word_order=LITTLE,
        fields=(
            RawField("rrcr_state", 0, "uint16"),
            RawField("active_power_limit", 1, "uint16"),
            RawField("cos_phi", 2, "float32"),
        ),
    ),
    *(block for slot in BATTERY_BASE for block in _battery_blocks(slot)),
)

RAW_DEVICES: tuple[RawDevice, ...] = tuple(
    RawDevice(
        key=f"battery_{slot}",
        info_block=f"battery_{slot}_info",
        name=f"Battery {slot}",
        manufacturer="manufacturer",
        model="model",
        serial="serial",
        version="version",
        requires=f"battery_{slot}",
    )
    for slot in BATTERY_BASE
)

RAW_SENSORS: tuple[RawSensor, ...] = (
    RawSensor(
        block="grid_status",
        field="grid_status",
        key="grid_status",
        device_class="enum",
        options=GRID_STATUS,
        icon="mdi:transmission-tower",
    ),
    RawSensor(
        block="status_vendor4",
        field="status_vendor4",
        key="status_vendor4",
        diagnostic=True,
        transform=status_vendor4,
        icon="mdi:alert-circle-outline",
    ),
    RawSensor(
        block="power_control",
        field="rrcr_state",
        key="rrcr_state",
        diagnostic=True,
        icon="mdi:electric-switch",
    ),
    *(sensor for slot in BATTERY_BASE for sensor in _battery_sensors(slot)),
)

# The storage control block, as the Power Control Protocol note lays
# it out. The remote control's default mode, command, timeout and
# limits only mean anything while the control mode is "remote
# control"; the entities stay, the inverter ignores them otherwise.
# The community's users found the order that works: control mode
# first, then the timeout, the command, then the limits, and that
# the inverter falls back to the default mode when the timeout runs
# out or when it restarts at night, which is what the rearm switch
# is for.
RAW_SELECTS: tuple[RawSelect, ...] = (
    RawSelect(
        block="storage_control",
        field="control_mode",
        key="storage_control_mode",
        options=STORAGE_CONTROL_MODES,
        icon="mdi:battery-sync",
    ),
    RawSelect(
        block="storage_control",
        field="ac_charge_policy",
        key="storage_ac_charge_policy",
        options=AC_CHARGE_POLICIES,
        icon="mdi:transmission-tower-import",
    ),
    RawSelect(
        block="storage_control",
        field="default_mode",
        key="storage_default_mode",
        options=REMOTE_MODES,
        icon="mdi:battery-clock",
    ),
    RawSelect(
        block="storage_control",
        field="command_mode",
        key="storage_command_mode",
        options=REMOTE_MODES,
        icon="mdi:battery-arrow-up-outline",
    ),
    # The site limit: which meter the limit is measured against, and
    # whether it applies to the total or to each phase. Bits 10 and
    # 11 of the mode register (external production, negative limit)
    # are left as they are.
    RawSelect(
        block="site_limit",
        field="mode",
        key="site_limit_mode",
        options=SITE_LIMIT_MODES,
        read=lambda value: value & 0b111 if isinstance(value, int) else None,
        write=_keep_site_limit_bits,
        beta=True,
        icon="mdi:transmission-tower-export",
    ),
    RawSelect(
        block="site_limit",
        field="limit_mode",
        key="site_limit_scope",
        options=SITE_LIMIT_SCOPES,
        beta=True,
        icon="mdi:transmission-tower-export",
    ),
)

RAW_NUMBERS: tuple[RawNumber, ...] = (
    RawNumber(
        block="storage_control",
        field="ac_charge_limit",
        key="storage_ac_charge_limit",
        min=0,
        max=100000,
        step=0.1,
        icon="mdi:transmission-tower-import",
    ),
    RawNumber(
        block="storage_control",
        field="backup_reserve",
        key="storage_backup_reserve",
        min=0,
        max=100,
        step=1,
        unit="%",
        icon="mdi:battery-lock",
    ),
    RawNumber(
        block="storage_control",
        field="command_timeout",
        key="storage_command_timeout",
        min=0,
        max=86400,
        step=60,
        unit="s",
        device_class="duration",
        icon="mdi:timer-sand",
    ),
    RawNumber(
        block="storage_control",
        field="charge_limit",
        key="storage_charge_limit",
        min=0,
        max=100000,
        step=100,
        unit="W",
        device_class="power",
        icon="mdi:battery-charging-medium",
    ),
    RawNumber(
        block="storage_control",
        field="discharge_limit",
        key="storage_discharge_limit",
        min=0,
        max=100000,
        step=100,
        unit="W",
        device_class="power",
        icon="mdi:battery-arrow-down",
    ),
    RawNumber(
        block="site_limit",
        field="site_limit",
        key="site_limit",
        min=0,
        max=1000000,
        step=10,
        unit="W",
        device_class="power",
        beta=True,
        icon="mdi:transmission-tower-export",
    ),
    RawNumber(
        block="power_control",
        field="active_power_limit",
        key="active_power_limit",
        min=0,
        max=100,
        step=1,
        unit="%",
        beta=True,
        icon="mdi:speedometer-slow",
    ),
)

RAW_KEEPALIVES: tuple[RawKeepAlive, ...] = (
    RawKeepAlive(
        key="storage_command_rearm",
        block="storage_control",
        fields=("command_timeout", "command_mode"),
        interval_seconds=REMOTE_COMMAND_REARM_SECONDS,
        only_while=_remote_control_active,
        icon="mdi:battery-sync-outline",
    ),
    RawKeepAlive(
        key="active_power_limit_reassert",
        block="power_control",
        fields=("active_power_limit",),
        interval_seconds=ACTIVE_POWER_LIMIT_REASSERT_SECONDS,
        beta=True,
        icon="mdi:refresh-auto",
    ),
)

SOLAREDGE = VendorProfile(
    slug="solaredge",
    # The device sends "SolarEdge " with a trailing space; the prefix
    # match does not care.
    manufacturer_prefixes=("SolarEdge",),
    raw_blocks=RAW_BLOCKS,
    raw_devices=RAW_DEVICES,
    raw_sensors=RAW_SENSORS,
    raw_numbers=RAW_NUMBERS,
    raw_selects=RAW_SELECTS,
    raw_keepalives=RAW_KEEPALIVES,
    # The two dynamic setpoints live in RAM; everything else the
    # profile writes is presumed to land in flash.
    volatile_registers=frozenset({0xF001, 0xF002}),
)
