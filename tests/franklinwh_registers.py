"""Selected readbacks from the reference aGate's V12 snapshot, with synthetic identity.

Source: david2069/franklinwh-modbus at 9afbb9f, MIT license, copyright
2026 david2069. See LICENSE and docs/SUNSPEC_SNAPSHOT_2026-10-20_V12R02B60D00.md
in that repository. Unlisted SunSpec points are left unimplemented in
inverter_franklinwh.json; this is not a complete capture of its model chain.
"""

from .fake_unit import register_image


def agate_registers(base=0):
    image = register_image("./tests/test_data/inverter_franklinwh.json", base)
    # 15500..15513: PV flags, source powers, load, mode, reserves, then
    # the two uint32 Wh counters. 16000 is the finer home-load reading.
    energy = [15882870 >> 16, 15882870 & 0xFFFF]
    words = [1, 0, 300, 300, 0, 0, 185, 2, 8, 15, *energy, *energy]
    image.update({15500 + index: word for index, word in enumerate(words)})
    image.update({16000: 185, 16001: 8})
    return image
