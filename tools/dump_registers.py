#!/usr/bin/env python3
"""Read-only Modbus TCP register dump of an APsystems ECU.

Use it when setup fails and there is no entry to download diagnostics
from. It reads holding registers 40000 to 40261 from the unit ids you
name, plus one unit id that should have no micro inverter, and writes
the answers to a JSON file to attach to an issue. It never writes.

    python3 tools/dump_registers.py --host 192.0.2.10 --units 1 2 --probe-unit 9

Before you run it:

* Disable the integration first. A second Modbus connection can make the
  ECU stop answering.
* The file holds the serial numbers of your micro inverters. Replace them
  before you share it.
* It sends about 15 requests, one at a time, with a pause in between.

The upper limit of 40261 is deliberate. On an ECU-R registers 40262 to
40269 read 0xDEADBEEF and the ECU stops answering at 40270. That finding
comes from christianvaes/homeassistant-apsystems_modbus, which has a
script of the same purpose.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path

FIRST_REGISTER = 40000
LAST_REGISTER = 40261
"""Last register to read, inclusive."""
BLOCK_SIZES = (125, 60, 30)
"""Registers per request, tried in this order on the first unit."""
PAUSE = 1.0
"""Seconds between two requests."""
TIMEOUT = 15.0
"""Seconds to wait for one reply; the ECU can take several seconds."""

EXCEPTION_TEXT = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "server device failure",
    6: "server device busy",
    10: "gateway path unavailable",
    11: "gateway target device failed to respond",
}


class ExceptionReply(Exception):
    """The ECU answered with a Modbus exception code."""

    def __init__(self, code: int) -> None:
        """Keep the exception code for the report."""
        super().__init__(f"exception {code} ({EXCEPTION_TEXT.get(code, 'unknown')})")
        self.code = code


class Session:
    """One TCP connection that sends function code 3 requests only."""

    def __init__(self, host: str, port: int) -> None:
        """Open the connection."""
        self._sock = socket.create_connection((host, port), timeout=TIMEOUT)
        self._transaction = 0

    def read(self, unit: int, address: int, count: int) -> list[int]:
        """Read ``count`` holding registers starting at ``address``."""
        self._transaction = (self._transaction + 1) % 0x10000
        request = struct.pack(">HHHBBHH", self._transaction, 0, 6, unit, 3, address, count)
        self._sock.sendall(request)
        header = self._recv(7)
        _, _, length, _ = struct.unpack(">HHHB", header)
        body = self._recv(length - 1)
        if body[0] & 0x80:
            raise ExceptionReply(body[1])
        if body[1] != count * 2:
            raise ValueError(f"expected {count * 2} bytes, got {body[1]}")
        return list(struct.unpack(f">{count}H", body[2:]))

    def _recv(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self._sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("the ECU closed the connection")
            data += chunk
        return data

    def close(self) -> None:
        """Close the connection."""
        self._sock.close()


def dump_unit(session: Session, unit: int, block_size: int) -> dict[str, object]:
    """Read the whole range of one unit, or record why it stopped."""
    registers: dict[str, int] = {}
    address = FIRST_REGISTER
    while address <= LAST_REGISTER:
        count = min(block_size, LAST_REGISTER - address + 1)
        try:
            values = session.read(unit, address, count)
        except ExceptionReply as err:
            return {"registers": registers, "stopped_at": address, "error": str(err)}
        except (OSError, ValueError) as err:
            return {"registers": registers, "stopped_at": address, "error": repr(err)}
        registers.update({str(address + i): value for i, value in enumerate(values)})
        address += count
        time.sleep(PAUSE)
    return {"registers": registers}


def pick_block_size(session: Session, unit: int) -> int:
    """Return the largest block size the ECU accepts on the first unit."""
    for size in BLOCK_SIZES:
        try:
            session.read(unit, FIRST_REGISTER, size)
        except ExceptionReply:
            time.sleep(PAUSE)
            continue
        time.sleep(PAUSE)
        return size
    return BLOCK_SIZES[-1]


def main() -> int:
    """Run the dump and write the JSON file."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", required=True, help="IP address of the ECU")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument(
        "--units", type=int, nargs="+", required=True, help="unit ids with a micro inverter"
    )
    parser.add_argument("--probe-unit", type=int, help="a unit id without a micro inverter")
    parser.add_argument("--out", type=Path, default=Path("."), help="folder for the JSON file")
    args = parser.parse_args()

    session = Session(args.host, args.port)
    try:
        block_size = pick_block_size(session, args.units[0])
        result: dict[str, object] = {
            "taken": datetime.now(UTC).isoformat(timespec="seconds"),
            "block_size": block_size,
            "units": {str(u): dump_unit(session, u, block_size) for u in args.units},
        }
        if args.probe_unit is not None:
            result["probe_unit"] = {
                str(args.probe_unit): dump_unit(session, args.probe_unit, block_size)
            }
    finally:
        session.close()

    target = args.out / f"apsystems-dump-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    target.write_text(json.dumps(result, indent=2))
    sys.stdout.write(f"Wrote {target}. Replace the serial numbers before you share it.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
