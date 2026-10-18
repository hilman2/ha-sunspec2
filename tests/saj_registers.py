"""Register image of a SAJ R6 behind an eSolar AIO3, for the file-backed test client.

The SunSpec models come from tests/test_data/inverter_saj.json; these
are SAJ's own registers, with the values read from an R6-15K-T2-32 on
the morning of 2026-10-18.
"""

ENERGY_ADDRESS = 0x6004
PV_ADDRESS = 0x5194


def _uint32(value):
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


def r6_registers(total=1239113, month=33072, today=40, inputs=((1862, 232, 433), (6952, 29, 214))):
    """SAJ's energy and DC blocks: counters in 0.01 kWh, inputs as raw ``(dV, cA, W)``."""
    registers = {}
    energy = _uint32(total) + _uint32(total) + _uint32(month) + _uint32(today)
    for index, word in enumerate(energy):
        registers[ENERGY_ADDRESS + index] = word
    pv = [sum(watts for _volts, _amps, watts in inputs)]
    for volts, amps, watts in inputs:
        pv += [volts, amps, 0, watts]
    for index, word in enumerate(pv):
        registers[PV_ADDRESS + index] = word
    return registers
