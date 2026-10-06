"""APsystems: the ECU-R and ECU-C gateways in front of DS3, QS1, QT2 and YC micro inverters.

The ECU serves one SunSpec device per micro inverter, on the unit ids
set in EMA Manager (Workspace, Modbus Configuration), over Modbus TCP
on port 502 or over RS485. The models are 1, 101 or 103, 111 to 113
and 123 for the controls. What the SunSpec models do not carry is the
DC side of each inverter, and APsystems puts it in a block of its
own: model id 114, 48 registers of float32, DC voltage, current and
power per input (DCV1 to 8 at 40214, DCA1 to 8 at 40230, DCW1 to 8 at
40246, two registers each, 8 inputs whatever the inverter has).

That block is not a SunSpec model: it is not in sunspec/models, and in
APsystems' own allocation table it sits behind the 0xFFFF end marker,
so a scan that stops at the marker never sees it. This module reads it
from its fixed addresses, which is all it takes. Inputs an inverter
does not have read 0.0, so inputs 3 to 8 are off by default.

Sources: APsystems "SunSpec Modbus" rev 3.3, section 3, the allocation
tables (https://global.apsystems.com/wp-content/uploads/2025/01/SunSpec-Modbus.pdf),
and the register dump of an ECU-R 2160 on firmware 1.3.23 with eight
DS3(L) in issue #109.

What the dump shows and the document does not. On that firmware the
header at 40184, where the document puts model 123 with 24 registers,
reads model 114 with 48, and the header at 40212, where the document
puts 114, reads zero; the registers behind both are as documented
(the controls body behind 40184, the floats from 40214 on). The chain
therefore ends in blank memory after 40233 instead of at the marker at
40210, which the scan handles (it stops at a header with model id 0),
and the controls model is not found under its number. That is not
corrected here: it is one dump, and a guess at model 123 would be a
guess about a write.
"""

from __future__ import annotations

from .profile import RawBlock
from .profile import RawField
from .profile import RawSensor
from .profile import VendorProfile

#: Inputs the block has room for. A DS3 uses 2, a QS1 or QT2 4, a YC1000 more.
INPUTS = 8
#: Where the block's data starts: the registers behind the 40212 header.
DC_ADDRESS = 40214
#: Eight float32 voltages, eight currents, eight powers.
DC_COUNT = 3 * INPUTS * 2
#: Inputs a micro inverter always has; the rest only some of them do.
ALWAYS_PRESENT = 2

_QUANTITIES: tuple[tuple[str, str, str, str], ...] = (
    ("dcv", "voltage", "V", "voltage"),
    ("dca", "current", "A", "current"),
    ("dcw", "power", "W", "power"),
)

RAW_BLOCKS: tuple[RawBlock, ...] = (
    RawBlock(
        key="apsystems_dc",
        address=DC_ADDRESS,
        count=DC_COUNT,
        fields=tuple(
            RawField(f"{prefix}{input_no}", (group * INPUTS + input_no - 1) * 2, "float32")
            for group, (prefix, _key, _unit, _cls) in enumerate(_QUANTITIES)
            for input_no in range(1, INPUTS + 1)
        ),
    ),
)

RAW_SENSORS: tuple[RawSensor, ...] = tuple(
    RawSensor(
        block="apsystems_dc",
        field=f"{prefix}{input_no}",
        key=f"apsystems_dc_{key}_{input_no}",
        unit=unit,
        device_class=device_class,
        state_class="measurement",
        enabled_by_default=input_no <= ALWAYS_PRESENT,
    )
    for prefix, key, unit, device_class in _QUANTITIES
    for input_no in range(1, INPUTS + 1)
)

APSYSTEMS = VendorProfile(
    slug="apsystems",
    manufacturer_prefixes=("APsystems",),
    raw_blocks=RAW_BLOCKS,
    raw_sensors=RAW_SENSORS,
)
