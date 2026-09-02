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
        block="site_limit",
        field="mode",
        key="site_limit_mode",
        device_class="enum",
        options=SITE_LIMIT_MODES,
        transform=lambda value: value & 0b111 if isinstance(value, int) else None,
        diagnostic=True,
        icon="mdi:transmission-tower-export",
    ),
    RawSensor(
        block="site_limit",
        field="site_limit",
        key="site_limit",
        unit="W",
        device_class="power",
        diagnostic=True,
        icon="mdi:transmission-tower-export",
    ),
    RawSensor(
        block="storage_control",
        field="control_mode",
        key="storage_control_mode",
        device_class="enum",
        options=STORAGE_CONTROL_MODES,
        diagnostic=True,
        icon="mdi:battery-sync",
    ),
    *(sensor for slot in BATTERY_BASE for sensor in _battery_sensors(slot)),
)

SOLAREDGE = VendorProfile(
    slug="solaredge",
    # The device sends "SolarEdge " with a trailing space; the prefix
    # match does not care.
    manufacturer_prefixes=("SolarEdge",),
    raw_blocks=RAW_BLOCKS,
    raw_devices=RAW_DEVICES,
    raw_sensors=RAW_SENSORS,
)
