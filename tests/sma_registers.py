"""Register images of an SMA Sunny Tripower Smart Energy, for the file-backed test client.

The SunSpec models come from tests/test_data/inverter_sma.json under
unit id 126; these are SMA's own registers under unit id 3. SMA's
register numbers are one-based, so register 30845 is address 30844.
"""

from custom_components.sunspec2.raw_blocks import encode_value

BIG = "big"


def u32(value):
    return encode_value("uint32", value, BIG)


def s32(value):
    return encode_value("int32", value, BIG)


def u64(value):
    return encode_value("uint64", value, BIG)


def at(register, data):
    """The register image of ``data`` starting at SMA register number ``register``."""
    return {
        register - 1 + index: int.from_bytes(data[index * 2 : index * 2 + 2], "big")
        for index in range(len(data) // 2)
    }


def smart_energy_registers():
    """An STP 10.0-3SE-40 with a battery, external setpoint mode set, 30 min timeout."""
    registers = {}
    registers.update(at(30051, u32(8001) + u32(9337) + u32(461) + u32(3012345678)))
    registers.update(at(30201, u32(307)))
    registers.update(at(30835, u32(1079)))
    registers.update(
        at(
            30843,
            s32(-2500)  # current, 0.001 A: discharging with 2.5 A
            + u32(55)  # state of charge
            + u32(98)  # capacity
            + s32(250)  # temperature, 0.1 C
            + u32(5120),  # voltage, 0.01 V
        )
    )
    registers.update(at(30955, u32(2293)))
    registers.update(at(31393, u32(0) + u32(1200) + u64(123456) + u64(234567)))
    registers.update(at(40187, u32(10240)))
    registers.update(at(41195, u32(1800)))
    return registers
