"""Register images of a Kostal PLENTICORE plus, for the file-backed test client.

The SunSpec models come from tests/test_data/inverter_kostal.json;
these are the registers outside them. A BYD battery is fitted, the
house is drawing from all three sources at once, and every 32-bit
value carries its low word first, the byte order the inverter ships
with.
"""

from custom_components.sunspec2.raw_blocks import encode_value

LITTLE = "little"


def u16(value):
    return encode_value("uint16", value, LITTLE)


def u32(value):
    return encode_value("uint32", value, LITTLE)


def f32(value):
    return encode_value("float32", value, LITTLE)


def string(text, registers):
    return text.encode("ascii").ljust(registers * 2, b"\x00")


def at(address, data):
    """The register image of ``data`` starting at ``address``."""
    return {
        address + index: int.from_bytes(data[index * 2 : index * 2 + 2], "big")
        for index in range(len(data) // 2)
    }


#: 98 to 125: the block the house consumption sensors read.
INVERTER = (
    f32(41.5)  # 98 controller board temperature
    + f32(3300.0)  # 100 total DC power
    + bytes(4)  # 102, not decoded
    + u32(0x00)  # 104 energy manager idle
    + f32(2900.0)  # 106 house consumption from battery
    + f32(150.0)  # 108 house consumption from grid
    + f32(1500000.0)  # 110 house consumption from battery, total
    + f32(2400000.0)  # 112 house consumption from grid, total
    + f32(5100000.0)  # 114 house consumption from PV, total
    + f32(450.0)  # 116 house consumption from PV
    + f32(9000000.0)  # 118 house consumption, total
    + f32(15000.0)  # 120 insulation resistance
    + f32(70.0)  # 122 grid operator power limit
)

#: 202 to 215. 204, 206 and 212 are registers the block does not
#: decode; 210 is the state of charge, which model 802 already has.
BATTERY_STATE = (
    f32(1.0)  # 202 PSSB fuse ok
    + bytes(8)  # 204, 206
    + f32(1.0)  # 208 battery ready
    + f32(64.0)  # 210 state of charge
    + bytes(4)  # 212
    + f32(21.5)  # 214 battery temperature
)

#: 512 to 589, the identity block. Wide, with the inverter's own
#: strings in the middle that nothing here decodes.
BATTERY_INFO = (
    u32(190)  # 512 gross capacity, Ah
    + u16(64)  # 514 state of charge
    + bytes(4)  # 515 firmware maincontroller
    + string("BYD", 8)  # 517 battery manufacturer
    + bytes(4)  # 525 battery model id
    + u32(30412)  # 527 battery serial number
    + u32(10240)  # 529 work capacity, Wh
    + bytes(110)  # 531 to 585, inverter registers and strings
    + u32(0x0107)  # 586 battery firmware
    + u16(0x0004)  # 588 battery type, BYD
    + bytes(2)  # 589, pads the block to its full count
)

#: 1046 to 1065, the energy counters.
BATTERY_ENERGY = (
    f32(3100000.0)  # 1046 DC charge energy
    + f32(2800000.0)  # 1048 DC discharge energy
    + f32(2900000.0)  # 1050 AC charge energy
    + f32(41000.0)  # 1052 AC discharge energy, battery to grid
    + f32(120000.0)  # 1054 AC charge energy, grid to battery
    + bytes(16)  # 1056 to 1062, the PV energy counters
    + f32(6400000.0)  # 1064 total energy AC-side to grid
)

#: 1034 to 1045, the writable end of the external battery management.
#: 1036 is the relative setpoint, which the block does not decode.
BATTERY_CONTROL = (
    f32(0.0)  # 1034 DC power setpoint, nothing asked for
    + bytes(4)  # 1036
    + f32(7000.0)  # 1038 charge power limit
    + f32(7000.0)  # 1040 discharge power limit
    + f32(5.0)  # 1042 minimum SoC
    + f32(100.0)  # 1044 maximum SoC
)

#: 1280 to 1289, the G3 battery limitation with its fallback watchdog.
BATTERY_LIMITATION = (
    f32(4000.0)  # 1280 held charge power limit
    + f32(4000.0)  # 1282 held discharge power limit
    + f32(7000.0)  # 1284 charge power after fallback
    + f32(7000.0)  # 1286 discharge power after fallback
    + u32(30)  # 1288 time until fallback
)

#: 1068 to 1083, the read-only end of the external battery management.
BATTERY_LIMITS = (
    f32(10240.0)  # 1068 work capacity
    + bytes(12)  # 1070 serial, 1072 and 1074 reserved
    + f32(7000.0)  # 1076 max charge power, read out from the battery
    + f32(7000.0)  # 1078 max discharge power
    + u16(0x02)  # 1080 external battery management via Modbus
    + bytes(2)  # 1081 reserved
    + u16(0x03)  # 1082 KOSTAL Smart Energy Meter
    + bytes(2)  # 1083, pads the block to its full count
)


def plenticore_registers():
    """Every Kostal register the profile reads, with a BYD battery fitted.

    A G3, so the battery limitation block at 1280 answers. On an older
    PLENTICORE it does not, which is what plenticore_g1_registers()
    stands for.
    """
    registers = plenticore_g1_registers()
    registers.update(at(1280, BATTERY_LIMITATION))
    return registers


def plenticore_g1_registers():
    """The same inverter below SW 03.05: everything but the battery limitation."""
    registers = {}
    registers.update(at(98, INVERTER))
    registers.update(at(202, BATTERY_STATE))
    registers.update(at(512, BATTERY_INFO))
    registers.update(at(1034, BATTERY_CONTROL))
    registers.update(at(1046, BATTERY_ENERGY))
    registers.update(at(1068, BATTERY_LIMITS))
    return registers


def no_battery_registers():
    """The same inverter with no battery: type 0, and the gated blocks absent."""
    registers = {}
    registers.update(at(98, INVERTER))
    info = BATTERY_INFO[:-4] + u16(0x0000) + bytes(2)
    registers.update(at(512, info))
    return registers


def registers_written(client, address, count):
    """What the test client holds at ``address``, as the profile would decode it."""
    return b"".join(int(client.registers[address + i]).to_bytes(2, "big") for i in range(count))
