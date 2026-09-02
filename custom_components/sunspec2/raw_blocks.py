"""Registers outside the SunSpec models: decoding them and reading them each cycle.

pysunspec2 walks the SunSpec model chain and knows nothing beyond it.
Some vendors keep their battery, their storage control or their power
control in registers of their own, at fixed addresses, in their own
word order. A vendor profile declares those as ``RawBlock``s, the
coordinator reads them after the models, and the decoded values land
in ``coordinator.raw_blocks`` for the entities.

Two things a block read has to survive. A device without the feature
answers a Modbus exception, which means "not here" and is not worth a
retry every poll. And a SolarEdge on recent firmware answers nothing
at all for a block it does not serve, which costs the socket timeout
each time it is tried. Both mark the block absent for a while; both
are tried again later, because a feature can be switched on by the
vendor's support without a reconnect.
"""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from .errors import DeviceError
from .errors import SunSpecError
from .errors import TransientError
from .vendors.profile import RawBlock
from .vendors.profile import RawField

if TYPE_CHECKING:
    from .api import SunSpecApiClient

_LOGGER: logging.Logger = logging.getLogger(__package__)

#: Cycles an absent or silent block waits before it is tried again:
#: an hour at the default scan interval.
REPROBE_CYCLES = 120

_NOT_IMPLEMENTED: dict[str, int] = {
    "uint16": 0xFFFF,
    "int16": 0x8000,
    "uint32": 0xFFFFFFFF,
    "int32": 0x80000000,
    "uint64": 0xFFFFFFFFFFFFFFFF,
}

#: A float this large is a sentinel, not a reading: NaN for "not
#: implemented", the float extremes for "no limit set".
_FLOAT_LIMIT = 3.0e38


def _register_words(data: bytes, offset: int, count: int) -> tuple[int, ...]:
    start = offset * 2
    words: tuple[int, ...] = struct.unpack(f">{count}H", data[start : start + count * 2])
    return words


def _join(words: tuple[int, ...], word_order: str) -> int:
    ordered = tuple(reversed(words)) if word_order == "little" else words
    value = 0
    for word in ordered:
        value = (value << 16) | word
    return value


def decode_field(field: RawField, data: bytes, word_order: str) -> Any:
    """The value of ``field`` in ``data``, or None for a not-implemented sentinel."""
    kind = field.kind
    if kind == "string":
        raw = data[field.offset * 2 : (field.offset + field.length) * 2]
        text = raw.decode("ascii", errors="ignore")
        return "".join(ch for ch in text if ch.isprintable()).strip()
    if kind in ("uint16", "int16"):
        (value,) = _register_words(data, field.offset, 1)
        if value == _NOT_IMPLEMENTED[kind]:
            return None
        return value - 0x10000 if kind == "int16" and value >= 0x8000 else value
    if kind in ("uint32", "int32", "float32"):
        value = _join(_register_words(data, field.offset, 2), word_order)
        if kind == "float32":
            number: float = struct.unpack(">f", struct.pack(">I", value))[0]
            if math.isnan(number) or abs(number) > _FLOAT_LIMIT:
                return None
            return number
        if value == _NOT_IMPLEMENTED[kind]:
            return None
        return value - 0x100000000 if kind == "int32" and value >= 0x80000000 else value
    if kind == "uint64":
        value = _join(_register_words(data, field.offset, 4), word_order)
        return None if value == _NOT_IMPLEMENTED[kind] else value
    raise ValueError(f"unknown raw field kind {kind!r}")


def decode_block(block: RawBlock, data: bytes) -> dict[str, Any]:
    """Every field of ``block`` from the bytes of its registers.

    Raises:
        DeviceError: The device answered fewer registers than the block has.
    """
    if len(data) < block.count * 2:
        raise DeviceError(
            f"Block {block.key}: {len(data) // 2} of {block.count} registers answered"
        )
    return {field.name: decode_field(field, data, block.word_order) for field in block.fields}


def encode_value(kind: str, value: float | int, word_order: str) -> bytes:
    """The register bytes for one value of ``kind``, in the block's word order."""
    if kind in ("uint16", "int16"):
        number = int(value)
        return struct.pack(">H", number & 0xFFFF)
    words: tuple[int, ...]
    if kind == "float32":
        (as_int,) = struct.unpack(">I", struct.pack(">f", float(value)))
        words = ((as_int >> 16) & 0xFFFF, as_int & 0xFFFF)
    elif kind in ("uint32", "int32"):
        number = int(value) & 0xFFFFFFFF
        words = ((number >> 16) & 0xFFFF, number & 0xFFFF)
    elif kind == "uint64":
        number = int(value) & 0xFFFFFFFFFFFFFFFF
        words = tuple((number >> shift) & 0xFFFF for shift in (48, 32, 16, 0))
    else:
        raise ValueError(f"cannot encode raw field kind {kind!r}")
    ordered = tuple(reversed(words)) if word_order == "little" else words
    return struct.pack(f">{len(ordered)}H", *ordered)


@dataclass
class _Probe:
    """What the reader knows about one block: when to try it again, and what it last held."""

    skip_until: int | None = None
    last: dict[str, Any] | None = None
    reported: bool = False


class RawBlockReader:
    """Reads a profile's raw blocks once per cycle, absent ones only now and then."""

    def __init__(self, api: SunSpecApiClient, log: logging.LoggerAdapter[logging.Logger]) -> None:
        self._api = api
        self._log = log
        self._cycle = 0
        self._probes: dict[str, _Probe] = {}

    async def async_read(self, blocks: tuple[RawBlock, ...]) -> dict[str, dict[str, Any]]:
        """The decoded blocks of this cycle, keyed by block.

        A block that is absent, silent or gated off is not in the
        result. A block that failed for a reason that is not the
        device's, a closed connection say, keeps what it held last.
        """
        self._cycle += 1
        result: dict[str, dict[str, Any]] = {}
        for block in blocks:
            probe = self._probes.setdefault(block.key, _Probe())
            if block.gate is not None:
                gate_block, gate_field = block.gate
                gate_value = result.get(gate_block, {}).get(gate_field)
                if not isinstance(gate_value, (int, float)) or gate_value <= 0:
                    continue
            if probe.skip_until is not None and self._cycle < probe.skip_until:
                continue
            try:
                data = await self._api.async_read_block(block.address, block.count)
                decoded = decode_block(block, data)
            except DeviceError as exc:
                # A Modbus exception: the device does not serve these
                # registers. SolarEdge switches whole feature sets on per
                # inverter, so ask again in a while.
                self._mark_absent(probe, block, f"not on this device ({exc})")
            except TransientError as exc:
                # Silence. SolarEdge firmware from 4.23 hangs instead of
                # refusing a block it does not have, and every attempt
                # costs the socket timeout, so this one waits as long.
                self._mark_absent(probe, block, f"no answer ({exc})")
            except SunSpecError as exc:
                self._log.debug("Raw block %s: %s, keeping the last reading", block.key, exc)
                if probe.last is not None:
                    result[block.key] = probe.last
            else:
                if probe.reported:
                    self._log.info("Raw block %s answers again", block.key)
                    probe.reported = False
                probe.skip_until = None
                probe.last = decoded
                result[block.key] = decoded
        return result

    def _mark_absent(self, probe: _Probe, block: RawBlock, why: str) -> None:
        probe.skip_until = self._cycle + REPROBE_CYCLES
        probe.last = None
        if not probe.reported:
            self._log.info(
                "Raw block %s at %d: %s. Trying again in %d cycles",
                block.key,
                block.address,
                why,
                REPROBE_CYCLES,
            )
            probe.reported = True
