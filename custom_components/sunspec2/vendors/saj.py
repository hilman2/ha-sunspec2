"""SAJ: the R6 three-phase inverter behind an eSolar AIO3 communication module.

The AIO3 sits on the inverter's USB port and serves Modbus TCP on port
502, unit id 1, over Wi-Fi or Ethernet. It answers a SunSpec chain at
40000 with models 1, 103, 120 to 123, 127, 128, 132 and 160. Model 103
carries the AC side, the energy counter and the DC totals. What it does
not carry is the DC side of each MPPT input, and there is no daily
energy point. SAJ declares model 160 for the inputs but leaves every
point of its one module at zero, DCV included while model 103 reads
620 V on the DC side.

Both sit in SAJ's own registers on the same unit id, at fixed addresses,
big-endian like SunSpec:

* 0x6004 to 0x600B: four uint32 energy counters in 0.01 kWh, the total
  (the same value as ``WH`` of model 103), the year, the month and the
  day. The month read 330.72 kWh where SAJ's cloud showed 330.72 kWh,
  the day 0.40 kWh where the cloud showed 0.39 kWh.
* 0x5194 to 0x519C: the DC power of both inputs together, then voltage
  (0.1 V), current (0.01 A) and power (W) of PV1 and of PV2, with an
  unused register after each current. PV1 + PV2 equals the total.

The 0x5194 block is a snapshot the module refreshes every few minutes,
probably the record it uploads to SAJ's cloud, so the input values lag
model 103 by that much. Read at 09:24 it held 186.2 V, 2.32 A, 433 W on
PV1 and 695.2 V, 0.29 A, 214 W on PV2, where SAJ's app showed 198.5 V,
2.09 A, 418 W and 698.1 V, 0.31 A, 223 W from its 09:20 upload.

Two quirks of the module. A register it does not serve answers a Modbus
exception whose code is the high byte of the address (0x40 for 0x40xx)
rather than 02; the raw block reader takes any exception as "not here".
And it stops answering for a few minutes after a few hundred requests
in quick succession, so the blocks are kept to two short reads.

``WH`` of model 103 is not a lifetime counter on the R6 either: it
starts again at zero on 1 January (12 894 kWh in November 2025, 19.7 kWh
in December), together with the year counter here. The lifetime energy
sensor holds the first low reading of the year and takes it after its
consecutive rejections, which costs a poll or two of the new year.

Sources: register reads of an R6-15K-T2-32 on firmware 6.199 behind an
AIO3, checked against model 103, SAJ's eSolar cloud and the Elekeeper
app on 2026-10-17 and 18. The profile applies to the R6-T2 family,
whose two MPPT inputs SAJ documents across its power ratings:
https://www.saj-electric.com/hubfs/Product%20Url/R6/R6%203-15K%20Three%20Phase%20On-Grid%20Inverter.pdf
https://img.saj-electric.com/file/R6-T2-20210812105442329.pdf
Only the R6-15K-T2-32 register image has been checked directly. R6-T3
and R6-T4 variants have more inputs and stay generic. Other SAJ models
use different maps: the R5 and the H1/H2 hybrids put their real-time
data at 0x0100 or 0x4000 over RS485.
"""

from __future__ import annotations

import re

from .profile import RawBlock
from .profile import RawField
from .profile import RawSensor
from .profile import VendorProfile

#: The energy counters, four uint32 in 0.01 kWh.
ENERGY_ADDRESS = 0x6004
ENERGY_COUNT = 8
#: The DC block: total, then PV1 and PV2.
PV_ADDRESS = 0x5194
PV_COUNT = 9
#: MPPT inputs of the R6.
INPUTS = 2

RAW_BLOCKS: tuple[RawBlock, ...] = (
    RawBlock(
        key="saj_energy",
        address=ENERGY_ADDRESS,
        count=ENERGY_COUNT,
        fields=(
            RawField("total", 0, "uint32", scale=0.01),
            RawField("year", 2, "uint32", scale=0.01),
            RawField("month", 4, "uint32", scale=0.01),
            RawField("today", 6, "uint32", scale=0.01),
        ),
    ),
    RawBlock(
        key="saj_pv",
        address=PV_ADDRESS,
        count=PV_COUNT,
        fields=(
            RawField("dcw", 0, "uint16"),
            *(
                field
                for input_no in range(1, INPUTS + 1)
                for field in (
                    RawField(f"dcv{input_no}", 1 + (input_no - 1) * 4, "uint16", scale=0.1),
                    RawField(f"dca{input_no}", 2 + (input_no - 1) * 4, "uint16", scale=0.01),
                    RawField(f"dcw{input_no}", 4 + (input_no - 1) * 4, "uint16"),
                )
            ),
        ),
    ),
)

_QUANTITIES: tuple[tuple[str, str, str, str], ...] = (
    ("dcv", "voltage", "V", "voltage"),
    ("dca", "current", "A", "current"),
    ("dcw", "power", "W", "power"),
)

RAW_SENSORS: tuple[RawSensor, ...] = (
    RawSensor(
        block="saj_energy",
        field="today",
        key="saj_energy_today",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
    ),
    RawSensor(
        block="saj_energy",
        field="month",
        key="saj_energy_month",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
    ),
    *(
        RawSensor(
            block="saj_pv",
            field=f"{prefix}{input_no}",
            key=f"saj_dc_{key}_{input_no}",
            unit=unit,
            device_class=device_class,
            state_class="measurement",
        )
        for prefix, key, unit, device_class in _QUANTITIES
        for input_no in range(1, INPUTS + 1)
    ),
)


def is_r6(model: str, option: str, version: str) -> bool:
    """Whether common model 1 names an R6 with two MPPT inputs."""
    # T2 selects the two-input family, independently of its power rating
    # and regional suffixes. Opt only says "R6" and cannot select it.
    return re.fullmatch(r"R6-[1-9][0-9]*K-T2(?:-[A-Z0-9]+)*", model.strip().upper()) is not None


SAJ = VendorProfile(
    slug="saj",
    manufacturer_prefixes=("SAJ",),
    identifies=is_r6,
    raw_blocks=RAW_BLOCKS,
    raw_sensors=RAW_SENSORS,
)
