"""Register image of an APsystems ECU-R unit, for the file-backed test client.

The SunSpec models come from tests/test_data/inverter_apsystems.json;
these are the floats behind the end marker, from the dump in issue
#109 (a DS3(L): two inputs, the rest zero).
"""

from custom_components.sunspec2.raw_blocks import encode_value

DC_ADDRESS = 40214


def ds3_registers(inputs=((33.5, 0.9, 30.2), (34.1, 0.85, 29.0))):
    """The DC block of a micro inverter with the given ``(volts, amps, watts)`` inputs."""
    values = [0.0] * 24
    for index, (volts, amps, watts) in enumerate(inputs):
        values[index] = volts
        values[8 + index] = amps
        values[16 + index] = watts
    registers = {}
    for index, value in enumerate(values):
        data = encode_value("float32", value, "big")
        registers[DC_ADDRESS + index * 2] = int.from_bytes(data[0:2], "big")
        registers[DC_ADDRESS + index * 2 + 1] = int.from_bytes(data[2:4], "big")
    return registers
