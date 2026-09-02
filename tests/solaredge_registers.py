"""Register images of a SolarEdge Home Hub, for the file-backed test client.

The SunSpec models come from tests/test_data/inverter_solaredge.json;
these are the registers outside them: the grid status, the vendor
status word, the site limit, the storage control block, a battery in
slot 1, a phantom in slot 2 (strings but no rated energy, as firmware
4.18 shows one), and nothing in slot 3.
"""

from custom_components.sunspec2.raw_blocks import encode_value
from custom_components.sunspec2.vendors.solaredge import BATTERY_BASE

LITTLE = "little"


def u16(value):
    return encode_value("uint16", value, LITTLE)


def u32(value):
    return encode_value("uint32", value, LITTLE)


def u32_big(value):
    return encode_value("uint32", value, "big")


def u64(value):
    return encode_value("uint64", value, LITTLE)


def f32(value):
    return encode_value("float32", value, LITTLE)


def string32(text):
    return text.encode("ascii").ljust(32, b"\x00")


def at(address, data):
    """The register image of ``data`` starting at ``address``."""
    return {
        address + index: int.from_bytes(data[index * 2 : index * 2 + 2], "big")
        for index in range(len(data) // 2)
    }


BATTERY_1_DATA = (
    f32(5000.0)  # max charge power
    + f32(5000.0)  # max discharge power
    + f32(7000.0)  # max charge peak power
    + f32(7000.0)  # max discharge peak power
    + bytes(64)  # 32 registers the block does not decode
    + f32(25.5)  # temperature average
    + f32(27.0)  # temperature max
    + f32(51.2)  # voltage
    + f32(-10.0)  # current
    + f32(-512.0)  # power, negative while discharging
    + u64(123456)  # energy exported
    + u64(234567)  # energy imported
    + f32(9600.0)  # energy max
    + f32(4800.0)  # energy available
    + f32(99.0)  # state of health
    + f32(50.0)  # state of energy
    + u32(4)  # status: discharge
    + u32(0)  # status vendor
    + bytes(32)  # 16 event log registers
)


def home_hub_registers():
    """A Home Hub with the battery block switched on and one battery."""
    registers = {}
    registers.update(at(40113, u32(0)))
    registers.update(at(40119, u32_big(0x180000BF)))
    registers.update(at(0xE000, u16(1) + u16(0) + f32(5000.0)))
    registers.update(
        at(
            0xE004,
            u16(1)  # control mode: maximize self-consumption
            + u16(1)  # AC charge policy: always allowed
            + f32(0.0)  # AC charge limit
            + f32(10.0)  # backup reserve
            + u16(7)  # default mode
            + u32(3600)  # command timeout
            + u16(7)  # command mode
            + f32(5000.0)  # charge limit
            + f32(5000.0),  # discharge limit
        )
    )
    registers.update(
        at(
            BATTERY_BASE[1],
            string32("SolarEdge")
            + string32("Home Battery 48V")
            + string32("DCDC 3.3.9")
            + string32("B1234567")
            + u16(112)
            + u16(0)
            + f32(9700.0),
        )
    )
    registers.update(at(BATTERY_BASE[1] + 68, BATTERY_1_DATA))
    # The phantom: firmware 4.18 lists a second battery with strings
    # and no energy. Its data block must never be read.
    registers.update(
        at(
            BATTERY_BASE[2],
            string32("SolarEdge")
            + string32("")
            + string32("")
            + string32("")
            + u16(0)
            + u16(0)
            + f32(0.0),
        )
    )
    return registers


def battery_3_registers():
    """A second real battery, in slot 3, for the "switched on later" case."""
    registers = {}
    registers.update(
        at(
            BATTERY_BASE[3],
            string32("BYD")
            + string32("Battery-Box LV")
            + string32("1.0")
            + string32("BYD0001")
            + u16(113)
            + u16(0)
            + f32(10240.0),
        )
    )
    registers.update(at(BATTERY_BASE[3] + 68, BATTERY_1_DATA))
    return registers
