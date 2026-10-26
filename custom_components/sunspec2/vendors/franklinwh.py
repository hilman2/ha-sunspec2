"""FranklinWH aGate: SunSpec battery control and the manufacturer extension registers.

Adapted from david2069/franklinwh-modbus, commit
9afbb9f2c1b81cdea42784dd879e3fe4f75e892d, under the MIT license.
Copyright (c) 2026 david2069. The license notice is in the repository LICENSE.

The register meanings come from FRANKLINWH_MODBUS_EXTENSIONS.md and
the V10R01B04D00 and V12R02B60D00 device snapshots in that repository.
The watts sequence is covered by tests/unit/test_send_command_watts.py
and the V12 conformance tests. Native mode and reserve writes were
acknowledged without taking effect, so these extensions are read-only.

https://github.com/david2069/franklinwh-modbus/tree/9afbb9f2c1b81cdea42784dd879e3fe4f75e892d
"""

from __future__ import annotations

import math
from typing import Any

from .profile import RawBlock
from .profile import RawField
from .profile import RawSensor
from .profile import VendorProfile

AC_CAPACITY_MODEL = 702
CONTROL_MODEL = 704
STORAGE_MODEL = 713
DC_MODEL = 714

# The extension addresses are absolute PDU addresses, including when
# discovery found the SunSpec mirror at 40000.
EXTENSIONS_ADDRESS = 15500
HIGH_RESOLUTION_ADDRESS = 16000

NATIVE_MODES: dict[int, str] = {1: "emergency_backup", 2: "self_consumption", 3: "time_of_use"}

RAW_BLOCKS: tuple[RawBlock, ...] = (
    RawBlock(
        key="franklinwh_extensions",
        address=EXTENSIONS_ADDRESS,
        count=14,
        fields=(
            RawField("pv_installed", 0, "uint16"),
            RawField("remote_pv_installed", 1, "uint16"),
            # The reference documents the PV powers as uint16. An aGate
            # X without sun read 65000 and more in pv_total, which is a
            # small negative power as int16 (issue #149).
            RawField("pv_total", 2, "int16"),
            RawField("pv_proximal", 3, "int16"),
            RawField("pv_remote_1", 4, "int16"),
            RawField("pv_remote_2", 5, "int16"),
            RawField("home_load", 6, "uint16"),
            RawField("native_mode", 7, "uint16"),
            RawField("self_reserve", 8, "uint16"),
            RawField("tou_reserve", 9, "uint16"),
            RawField("pv_energy", 10, "uint32"),
            RawField("pv_proximal_energy", 12, "uint32"),
        ),
    ),
    RawBlock(
        key="franklinwh_high_resolution",
        address=HIGH_RESOLUTION_ADDRESS,
        count=2,
        fields=(RawField("home_load", 0, "uint16"), RawField("active_reserve", 1, "uint16")),
    ),
)


def percent(value: Any) -> float | None:
    """A reported reserve in percent, or None for an invalid value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 100 else None


RAW_SENSORS: tuple[RawSensor, ...] = (
    *(
        RawSensor(
            block="franklinwh_extensions",
            field=name,
            key=f"franklinwh_{name}",
            unit="W",
            device_class="power",
            state_class="measurement",
            enabled_by_default=name != "pv_total" and name != "home_load",
        )
        for name in ("pv_total", "pv_proximal", "pv_remote_1", "pv_remote_2", "home_load")
    ),
    RawSensor(
        block="franklinwh_high_resolution",
        field="home_load",
        key="franklinwh_home_load_high_resolution",
        unit="W",
        device_class="power",
        state_class="measurement",
        enabled_by_default=False,
    ),
    RawSensor(
        block="franklinwh_extensions",
        field="native_mode",
        key="franklinwh_native_mode",
        device_class="enum",
        options=NATIVE_MODES,
    ),
    *(
        RawSensor(
            block="franklinwh_extensions",
            field=name,
            key=f"franklinwh_{name}",
            unit="%",
            state_class="measurement",
            transform=percent,
            enabled_by_default=False,
        )
        for name in ("self_reserve", "tou_reserve")
    ),
    # 16001 followed the active mode's reserve on V12 and on an aGate X
    # reported as V10R11B40D00. On the latter, 15508 and 15509 both
    # repeated 16001 in every mode (issue #149), so only 16001 starts
    # enabled.
    RawSensor(
        block="franklinwh_high_resolution",
        field="active_reserve",
        key="franklinwh_active_reserve",
        unit="%",
        state_class="measurement",
        transform=percent,
    ),
    *(
        RawSensor(
            block="franklinwh_extensions",
            field=name,
            key=f"franklinwh_{name}",
            unit="Wh",
            device_class="energy",
            state_class="total_increasing",
            enabled_by_default=False,
        )
        for name in ("pv_energy", "pv_proximal_energy")
    ),
)


def is_agate(model: str, option: str, version: str) -> bool:
    """Whether common model 1 identifies an aGate gateway."""
    return model.strip().lower().startswith("agate")


FRANKLINWH = VendorProfile(
    slug="franklinwh",
    manufacturer_prefixes=("FranklinWH",),
    identifies=is_agate,
    raw_blocks=RAW_BLOCKS,
    raw_sensors=RAW_SENSORS,
    required_models=frozenset({AC_CAPACITY_MODEL, STORAGE_MODEL, DC_MODEL}),
)
