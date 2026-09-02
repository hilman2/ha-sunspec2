"""A modbus-connection unit and connection over a register image, for the transport tests.

``register_image`` turns one of the pysunspec2 device JSON files under
tests/test_data into the holding registers a real inverter answers
with: the SunS marker at the base address, then every model as id,
length and body, then the end marker. ``FakeConnection`` stands in for
``modbus_connection.tmodbus.ModbusConnection`` and hands out one
``FakeUnit`` per unit id.
"""

from __future__ import annotations

from modbus_connection import IllegalDataAddressError
from modbus_connection import ModbusConnectionError

from custom_components.sunspec2.pysunspec2 import mb
from custom_components.sunspec2.pysunspec2.file.client import FileClientDevice


class FakeUnit:
    """The ``ModbusUnit`` side: the registers of one unit id."""

    def __init__(self, holding: dict[int, int] | None = None, unit_id: int = 1) -> None:
        self.holding: dict[int, int] = holding if holding is not None else {}
        self.unit_id = unit_id
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, list[int]]] = []
        #: Raised by the next request instead of answering, then cleared.
        self.fail_next: Exception | None = None
        self.disconnects = 0

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.reads.append((address, count))
        self._maybe_fail()
        words = []
        for register in range(address, address + count):
            if register not in self.holding:
                raise IllegalDataAddressError()
            words.append(self.holding[register])
        return words

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        return await self.read_holding_registers(address, count)

    async def write_registers(self, address: int, values: list[int]) -> None:
        self._maybe_fail()
        self.writes.append((address, list(values)))
        for index, value in enumerate(values):
            self.holding[address + index] = value

    async def write_register(self, address: int, value: int) -> None:
        await self.write_registers(address, [value])

    async def disconnect(self) -> None:
        self.disconnects += 1

    @property
    def connected(self) -> bool:
        return True

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc


class FakeConnection:
    """The ``ModbusConnection`` side: units by id, and the state of the link."""

    def __init__(self, units: dict[int, FakeUnit] | None = None, refuse: bool = False) -> None:
        self.units: dict[int, FakeUnit] = units if units is not None else {}
        #: Refuse every connect with a connection error.
        self.refuse = refuse
        self.connected = False
        self.connects = 0
        self.disconnects = 0
        self.closed = False

    async def connect(self) -> None:
        self.connects += 1
        if self.refuse:
            raise ModbusConnectionError("connection refused")
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.connected = False

    async def close(self) -> None:
        self.closed = True
        self.connected = False

    def for_unit(self, unit_id: int) -> FakeUnit:
        return self.units.setdefault(unit_id, FakeUnit(unit_id=unit_id))


def register_image(path: str, base: int = 40000) -> dict[int, int]:
    """The holding registers of the device described by a pysunspec2 device JSON file."""
    device = FileClientDevice(path)
    device.scan()
    image = {base: 0x5375, base + 1: 0x6E53}
    addr = base + 2
    for model in device.model_list:
        # The whole model, id and length words first. The header is
        # what the chain walk goes by, so it is written from what the
        # body measures, whatever the JSON says under "L".
        body = model.get_mb()
        words = [int.from_bytes(body[i : i + 2], "big") for i in range(0, len(body), 2)]
        for index, word in enumerate(words):
            image[addr + index] = word
        image[addr] = model.model_id
        image[addr + 1] = len(words) - 2
        addr += len(words)
    image[addr] = mb.SUNS_END_MODEL_ID
    image[addr + 1] = 0
    return image
