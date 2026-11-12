"""SunSpec client for one device over Home Assistant's shared Modbus connection."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any
from typing import Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from modbus_connection import ModbusSerialParams
from modbus_connection import ModbusTcpParams

from .connection import SharedConnection
from .const import DEFAULT_BAUDRATE
from .const import DEFAULT_MODEL_READ_DELAY_SECONDS
from .const import DEFAULT_SCAN_DELAY_SECONDS
from .const import MAX_MODEL_READ_DELAY_SECONDS
from .const import MAX_SCAN_DELAY_SECONDS
from .const import MIN_MODEL_READ_DELAY_SECONDS
from .const import MIN_SCAN_DELAY_SECONDS
from .const import PARITY_EVEN
from .const import PARITY_NONE
from .const import TRANSPORT_RTU
from .const import TRANSPORT_TCP
from .errors import DeviceError
from .errors import ProtocolError
from .errors import TransientError
from .errors import TransportError
from .logger import SunSpecLoggerAdapter
from .logger import get_adapter
from .models import SunSpecModelWrapper
from .models import point_text
from .pysunspec2 import mb
from .pysunspec2 import mdef
from .pysunspec2.device import ModelError
from .pysunspec2.device import preload_model_defs
from .pysunspec2.modbus.client import SunSpecModbusClientError
from .pysunspec2.modbus.client import SunSpecModbusClientException
from .pysunspec2.modbus.client import SunSpecModbusClientTimeout
from .pysunspec2.modbus.client import SunSpecModbusValueError
from .pysunspec2.modbus.modbus import ModbusClientConnectionClosed
from .pysunspec2.modbus.modbus import ModbusClientError
from .pysunspec2.modbus.modbus import ModbusClientException
from .pysunspec2.modbus.modbus import ModbusClientTimeout
from .pysunspec2.modbus.unit_device import SunSpecModbusClientDeviceUnit
from .vendors import PROFILES
from .vendors import profile_for
from .vendors.profile import ModelRelabel

# The embedded pysunspec2 is untyped, so every object it hands back reaches
# mypy as Any. The alias names which Any a signature means: a connected
# SunSpecModbusClientDeviceUnit. The alias adds no checking.
type SunSpecClient = Any

# Seconds a whole config-flow scan may take. SETUP_TIMEOUT is a budget for
# the scan, not a per-request timeout: a probe must not alter the timeout
# of Home Assistant's shared link.
SETUP_TIMEOUT = 60

_LOGGER: logging.Logger = logging.getLogger(__package__)

# Exceptions that mean the link failed while the cached layout was being
# validated or rebuilt, not that the layout is wrong. A wrong layout is
# rescanned. A failed link is not: a scan of a device that does not answer
# takes minutes and holds the gateway for all of them.
_LINK_FAILURES = (SunSpecModbusClientTimeout, ModbusClientTimeout, ModbusClientConnectionClosed)


def progress(msg: str) -> bool:
    """Log the scan progress. pysunspec2 stops the scan when the callback returns False."""
    _LOGGER.debug(msg)
    return True


def is_unanswered(exc: BaseException) -> bool:
    """Whether ``exc`` comes from a gateway that did not answer in time.

    That is a request without an answer, or a connect the gateway did not
    take within the timeout. A refused connection is an answer and does
    not count.
    """
    # The timeout reaches the coordinator wrapped in whichever category the
    # layer that caught it chose: transient from a model read, transport
    # from a connect, protocol from a scan. The cause chain still holds it.
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, (SunSpecModbusClientTimeout, ModbusClientTimeout)):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _discovered_model_count(client: SunSpecClient) -> int:
    """How many models a (possibly failed) scan managed to register.

    Counts the integer keys only: pysunspec2 indexes ``client.models``
    by model id and by group name, so len() would double-count every
    model whose definition it could resolve.
    """
    try:
        return sum(1 for key in client.models if isinstance(key, int))
    except (AttributeError, TypeError):
        return 0


class SunSpecApiClient:
    """Client for one SunSpec device: one pysunspec2 client on one ``SharedConnection``.

    The client, the connection and the layout cache belong to the
    instance. Nothing is kept at class level, so a reloaded config entry
    starts with a new client and never picks up one that another
    instance built.
    """

    def __init__(
        self,
        host: str,
        port: int,
        unit_id: int,
        hass: HomeAssistant,
        capture_enabled: bool = False,
        transport: str = TRANSPORT_TCP,
        serial_port: str | None = None,
        baudrate: int = DEFAULT_BAUDRATE,
        parity: str = PARITY_NONE,
        scan_delay: float = DEFAULT_SCAN_DELAY_SECONDS,
        entry: ConfigEntry | None = None,
        model_read_delay: float = DEFAULT_MODEL_READ_DELAY_SECONDS,
    ) -> None:
        """Set up the client. Does no I/O.

        ``transport="tcp"`` connects to ``host:port`` and ignores the
        serial parameters. ``transport="rtu"`` connects to
        ``serial_port`` with ``baudrate`` and ``parity``; ``host`` and
        ``port`` then only label the log lines (``[host:port#unit_id]``),
        and the caller passes the serial port name and the baud rate
        for them.

        ``entry`` is the config entry whose unload lets go of the shared
        link. The config flow's probe passes None, and its hold on the
        link ends with ``async_shutdown()``.
        """

        self._host = host
        self._port = port
        self._hass = hass
        self._unit_id = unit_id
        # Seconds pysunspec2 sleeps between models during a scan
        # (CONF_SCAN_DELAY). A scan only runs when there is no usable
        # cached layout: a first setup, or a layout that no longer
        # validates. The value is clamped here, so a corrupted options
        # entry cannot reach the scan as a negative or unbounded pause.
        self._scan_delay = min(
            MAX_SCAN_DELAY_SECONDS, max(MIN_SCAN_DELAY_SECONDS, float(scan_delay))
        )
        # Seconds between two model reads (CONF_MODEL_READ_DELAY), see
        # _pace_model_read. It is an option of its own, so a fast device
        # can drop it without changing the scan pacing.
        self._model_read_delay = min(
            MAX_MODEL_READ_DELAY_SECONDS,
            max(MIN_MODEL_READ_DELAY_SECONDS, float(model_read_delay)),
        )
        self._transport = transport
        self._serial_port = serial_port
        self._baudrate = baudrate
        self._parity = parity
        self._reconnect = False
        self._client: SunSpecClient | None = None
        # The vendor profile's corrections, set when model 1 is read
        # (_async_read_model_once). They are kept here and not on the
        # client, so a reconnect that rebuilds the model objects keeps them.
        self._point_transforms: Mapping[tuple[int, str], Callable[[Any], Any]] = {}
        self._signed_nan_points: frozenset[tuple[int, str]] = frozenset()
        # When the last model read finished, for _pace_model_read.
        self._last_model_read_end: float | None = None
        # The connection outlives the client. async_close() drops the link
        # and the model objects; the next client connects on the same
        # connection, which modbus-connection reconnects on demand.
        self._connection: SharedConnection | None = None
        # The request timeout the coordinator asked the link for, from the
        # vendor profile. None for the transport's default.
        self._request_timeout: float | None = None
        self._shutdown = False
        self._entry = entry
        # Cached SunSpec layout: base address plus (model_id, addr, len)
        # per block. The cache survives async_close() and transport
        # failures. A new client validates it before use
        # (_async_validate_model_structure), and
        # reconnect_next(rescan=True) discards it.
        #
        # The cache has no expiry on purpose. A SunSpec model tree changes
        # on a firmware update and never otherwise, so a timer could only
        # rescan a layout that was still correct, and a rescan is the one
        # operation that can corrupt the layout silently: pysunspec2 walks
        # the chain with ``addr += model_len + 2``, so one misread length
        # shifts every block behind it and the scan still returns without
        # raising.
        self._base_addr: int | None = None
        self._model_structure: list[tuple[int, int, int]] | None = None
        # Bumped on every layout change, so the coordinator can tell that
        # the layout it persisted is stale without comparing the lists.
        self.structure_revision: int = 0
        # True while the model list in the live client is known to be
        # incomplete, see last_scan_was_partial.
        self._partial_scan = False
        self._log = get_adapter(host, port, unit_id)
        self._capture_enabled = capture_enabled
        self._captured_reads: list[dict[str, Any]] = []
        self._log.debug(
            "New SunspecApi Client (transport=%s, capture=%s, scan_delay=%ss, model_read_delay=%ss)",
            transport,
            capture_enabled,
            self._scan_delay,
            self._model_read_delay,
        )

    async def async_get_client(self) -> SunSpecClient:
        """Return the live pysunspec2 client, building it on first use.

        One client serves every read of a cycle and every cycle after
        it, until a failure or an unload drops it. After
        ``reconnect_next()`` the old client's link is dropped first and
        a new client is built.

        Raises:
            TransportError: The api is shut down, or the link failed or
                timed out while the device was scanned or its stored
                layout checked.
            ProtocolError: No SunSpec map was found on the device.
        """
        if self._shutdown:
            raise TransportError("SunSpec client is shut down")
        # The flag is cleared whether or not there is a client to drop.
        # After a failed cycle the coordinator closes the client before
        # it sets the flag, so _client is already None here. A flag left
        # standing would tear down the client built below on the next
        # call, and every cycle after a failure would cost two connects
        # and two scans, on hardware that grants one Modbus session at a
        # time (#42).
        if self._reconnect:
            if self._client is not None:
                await self._async_disconnect(self._client)
                self._client = None
            self._reconnect = False
        if self._client is None:
            self._client = await self.modbus_connect()
        return self._client

    def known_models(self) -> list[int]:
        """Return the model ids the live client holds, or an empty list without a client.

        Does no I/O. The options flow reads this for its model form and
        must not connect or scan there while the coordinator polls.
        """
        if self._client is None:
            return []
        return sorted(m for m in self._client.models if isinstance(m, int))

    async def async_get_data(self, model_id: int) -> SunSpecModelWrapper:
        """Read ``model_id`` and translate pysunspec2's exceptions into the typed errors.

        Raises:
            TransientError: No answer within the request timeout.
            TransportError: The link failed, or the device closed it twice in a row.
            DeviceError: The device answered with a Modbus exception.
        """
        with_model = SunSpecLoggerAdapter(
            self._log.logger, {**self._log.bound, "model_id": model_id}
        )
        # The model layer raises the fork's SunSpec* classes for what it
        # notices itself and lets the transport's ModbusClient* classes
        # through as they are; both classes of each pair land in the same
        # category. Subclasses come before their bases: the timeout and
        # the closed connection both derive from ModbusClientError.
        try:
            with_model.debug("Get data")
            return await self.read(model_id)
        except (SunSpecModbusClientTimeout, ModbusClientTimeout) as exc:
            with_model.warning("Modbus read timeout")
            raise TransientError(f"Modbus read timeout for model {model_id}") from exc
        except ModbusClientConnectionClosed as exc:
            # async_read_model already connected again once; a device
            # that hangs up twice in a row is not answering this cycle.
            with_model.warning("Device closed the Modbus connection twice in a row")
            raise TransportError(
                f"Device closed the Modbus connection while reading model {model_id}"
            ) from exc
        except (SunSpecModbusClientException, ModbusClientException) as exc:
            with_model.warning("Modbus exception while reading model")
            raise DeviceError(f"Modbus exception while reading model {model_id}: {exc}") from exc
        except ModbusClientError as exc:
            with_model.warning("Modbus transport error while reading model")
            raise TransportError(
                f"Modbus transport error while reading model {model_id}: {exc}"
            ) from exc

    async def read(self, model_id: int) -> SunSpecModelWrapper:
        return await self.async_read_model(model_id)

    async def async_write_points(self, model_id: int, points: list[tuple[str, object]]) -> None:
        """Write one or more points of a SunSpec model as one batch.

        Each value is set as the point's ``cvalue``, so pysunspec2
        applies the scale factor, and the batch is flushed with one
        ``model.async_write()``: one Modbus frame per run of consecutive
        registers.

        Args:
            model_id: The model to write to. Its first instance is written.
            points: ``(point name, value)`` pairs.

        Raises:
            TransientError: No answer within the request timeout.
            DeviceError: The model or a point is missing, a value cannot
                be encoded, the device refused the write or the link
                failed during it.
            TransportError: No client could be built, see
                ``async_get_client``.
            ProtocolError: Likewise.
        """
        with_model = SunSpecLoggerAdapter(
            self._log.logger, {**self._log.bound, "model_id": model_id}
        )
        with_model.debug("Write %s", ", ".join(f"{n} = {v!r}" for n, v in points))
        point_name = ", ".join(name for name, _ in points)
        # pysunspec2 has three unrelated exception roots, and the chain
        # below has to name all of them: ModelError (pysunspec2/device.py)
        # and SunSpecModbusValueError are plain Exceptions, and the unit
        # device raises the transport's ModbusClientError classes as they
        # are. An exception this chain misses reaches the Number or Switch
        # entity as a raw traceback, because their ``except SunSpecError``
        # does not match it. The timeouts come before their base classes:
        # only the timeouts are transient.
        try:
            await self._async_write_points(model_id, points)
        except (SunSpecModbusClientTimeout, ModbusClientTimeout) as exc:
            with_model.warning("Modbus write timeout")
            raise TransientError(
                f"Modbus write timeout for model {model_id} point {point_name}"
            ) from exc
        except ModelError as exc:
            # Almost always an unimplemented scale factor: the device
            # answers the *_SF register with 0x8000 ("not implemented"),
            # pysunspec2 nulls sf_value in Point.set_mb, and the cvalue
            # assignment cannot encode the value. The model read in
            # _async_write_points fixes a scale factor that was merely
            # unread, not this case, so the message names the cause.
            with_model.warning("Model error while writing: %s", exc)
            raise DeviceError(
                f"Device rejected the value for model {model_id} point {point_name}. "
                f"The inverter may not implement the scale factor this point needs: {exc}"
            ) from exc
        except (
            SunSpecModbusValueError,
            SunSpecModbusClientException,
            SunSpecModbusClientError,
            ModbusClientException,
            ModbusClientError,
        ) as exc:
            with_model.warning("Modbus exception while writing")
            raise DeviceError(
                f"Modbus exception while writing model {model_id} point {point_name}: {exc}"
            ) from exc

    async def async_read_block(self, address: int, count: int, unit_id_offset: int = 0) -> bytes:
        """Read ``count`` holding registers at ``address`` over the live session.

        For the registers a vendor keeps outside the SunSpec models,
        see ``raw_blocks.py``. The coordinator calls this inside its
        read cycle, on the session the model reads use.
        ``unit_id_offset`` addresses another unit on the same
        connection, the entry's unit id plus the offset: SMA keeps its
        own registers 123 unit ids below the SunSpec ones.

        Raises:
            DeviceError: The device answered with a Modbus exception,
                or with fewer registers than asked.
            TransientError: No answer within the request timeout.
            TransportError: The connection failed or was closed.
        """
        unit_id = self._unit_id + unit_id_offset if unit_id_offset else None
        client = await self.async_get_client()
        try:
            data = await self._async_read_block(client, address, count, unit_id)
        except (SunSpecModbusClientTimeout, ModbusClientTimeout) as exc:
            raise TransientError(f"Modbus read timeout at register {address}") from exc
        except (SunSpecModbusClientException, ModbusClientException) as exc:
            raise DeviceError(f"Modbus exception reading register {address}: {exc}") from exc
        except (SunSpecModbusClientError, ModbusClientError, OSError) as exc:
            raise TransportError(f"Modbus read failed at register {address}: {exc}") from exc
        if data is None or len(data) < count * 2:
            raise DeviceError(
                f"Register {address}: {0 if data is None else len(data) // 2} of {count} answered"
            )
        return bytes(data)

    async def _async_read_block(
        self, client: SunSpecClient, address: int, count: int, unit_id: int | None
    ) -> bytes | None:
        if unit_id is None:
            data: bytes | None = await client.async_read(address, count)
            return data
        other: bytes | None = await client.async_read_unit(unit_id, address, count)
        return other

    async def async_write_block(self, address: int, data: bytes, unit_id_offset: int = 0) -> None:
        """Write register bytes at ``address`` over the live session.

        Raises:
            TransientError: No answer within the request timeout.
            DeviceError: The device refused the write.
            TransportError: The connection failed or was closed.
        """
        self._log.debug("Write %d registers at %d", len(data) // 2, address)
        unit_id = self._unit_id + unit_id_offset if unit_id_offset else None
        client = await self.async_get_client()
        try:
            await self._async_write_block(client, address, data, unit_id)
        except (SunSpecModbusClientTimeout, ModbusClientTimeout) as exc:
            raise TransientError(f"Modbus write timeout at register {address}") from exc
        except (SunSpecModbusClientException, ModbusClientException) as exc:
            raise DeviceError(f"Modbus exception writing register {address}: {exc}") from exc
        except (SunSpecModbusClientError, ModbusClientError, OSError) as exc:
            raise TransportError(f"Modbus write failed at register {address}: {exc}") from exc

    async def _async_write_block(
        self, client: SunSpecClient, address: int, data: bytes, unit_id: int | None
    ) -> None:
        if unit_id is None:
            await client.async_write(address, data)
            return
        await client.async_write_unit(unit_id, address, data)

    @staticmethod
    def _encodable(point: Any, value: object) -> object:
        """Return ``value`` as the encoder of ``point`` takes it: an int for an integer point.

        A float for an unscaled integer point is rounded. Every other
        value is returned unchanged.
        """
        # Home Assistant carries every Number's state as a float, so a
        # Number over a whole-number register hands in 303.0 where
        # ``struct.pack`` wants 303 and raises "required argument is not
        # an integer" (the Fronius battery revert time, #57). A scaled
        # point is left alone: pysunspec2 rounds it after dividing by the
        # scale factor, and 12.5 % with a scale factor of -1 has to stay
        # 12.5.
        if point.sf_required or not isinstance(value, float):
            return value
        if point.info is None or point.info.to_type is not mdef.to_int:
            return value
        return round(value)

    async def _async_write_points(self, model_id: int, points: list[tuple[str, object]]) -> None:
        client = await self.async_get_client()
        models = client.models.get(model_id)
        if not models:
            names = ", ".join(name for name, _ in points)
            raise DeviceError(f"Model {model_id} not present on this device, cannot write {names}")
        # A SunSpec model can repeat (several MPPT modules), but model 123
        # (immediate controls) exists once per inverter, so the write
        # always targets instance 0.
        model = models[0]
        resolved = []
        for point_name, value in points:
            try:
                resolved.append((model.points[point_name], value))
            except KeyError as exc:
                raise DeviceError(f"Point {point_name} not present in model {model_id}") from exc
        # The model is read before cvalue is touched. Neither path that
        # fills ``client.models`` populates the points: the scan runs with
        # full_model_read=False, and the rebuild from the cached layout
        # reads only the header and the group counts. On a session that
        # has not polled this model yet, the scale factor that
        # Point.set_value(computed=True) needs is None, and without the
        # read every scaled write fails with
        #   ModelError: SF field WMaxLimPct_SF value not initialized
        # The failure is not vendor specific (reported for a KACO bp 20.0
        # NX3 M2 in #17). Unscaled points such as the enum16 switches
        # Conn, WMaxLim_Ena and OutPFSet_Ena are not affected. On a
        # session that has polled the model, the read refreshes stale
        # points.
        #
        # The read costs one block of len + 2 registers. It must come
        # before the cvalue assignment, never after: Group.async_read()
        # ends in set_mb(dirty=False) and would discard a pending value.
        await model.async_read()
        for point, value in resolved:
            point.cvalue = self._encodable(point, value)
        # One flush for the whole batch, not one per point.
        # ``Group.async_write_points`` emits only the dirty points and
        # joins consecutive registers into one frame, so "set the limit
        # and enable it" leaves as one Modbus write on model 704, where
        # WMaxLimPctEna and WMaxLimPct are adjacent. With two writes the
        # inverter can act on the first before the second arrives: the
        # limit briefly applies at the old percentage, or the new
        # percentage briefly applies while still disabled.
        await model.async_write()

    async def async_get_device_info(self) -> SunSpecModelWrapper:
        return await self.read(1)

    async def async_get_models(self) -> list[int]:
        self._log.debug("Fetching models")
        client = await self.async_get_client()
        model_ids = sorted(list(filter(lambda m: type(m) is int, client.models.keys())))
        return model_ids

    def reconnect_next(self, *, rescan: bool = True) -> None:
        """Have the next ``async_get_client()`` drop the client and build a new one.

        ``rescan=False`` keeps the cached layout for validation on the
        new client: a transport failure does not show that the model
        tree changed. The default discards the layout, for a device or
        firmware change.
        """
        self._reconnect = True
        if rescan:
            self._invalidate_model_structure()

    def _invalidate_model_structure(self) -> None:
        if self._model_structure is None and self._base_addr is None:
            return
        self._base_addr = None
        self._model_structure = None
        self.structure_revision += 1

    @property
    def last_scan_was_partial(self) -> bool:
        """True if the model list of the live client may be incomplete.

        Set when a scan stopped early, cleared by the next scan that
        walks the whole chain. The coordinator reads it to refuse a
        shortened list as "these models are gone". A restore from the
        cache leaves the flag alone, because the cache is only written
        from a complete scan.
        """
        return self._partial_scan

    def export_model_structure(self) -> dict[str, Any] | None:
        """Return the cached layout for the coordinator to persist, or None without one."""
        if not self._model_structure or self._base_addr is None:
            return None
        # The coordinator stores this dict on disk, and
        # import_model_structure reads ``base_addr`` and ``models`` back
        # from what earlier releases stored. The keys must keep their
        # meaning.
        return {
            "revision": self.structure_revision,
            "base_addr": self._base_addr,
            "models": [list(entry) for entry in self._model_structure],
        }

    def import_model_structure(self, payload: object) -> bool:
        """Adopt a layout persisted by an earlier run. False means "ignored".

        The payload is not trusted. The next connect validates it against
        the device with :meth:`_async_validate_model_structure`, like a
        layout this process scanned itself, so a stale or hand-edited
        store file costs three reads and a rescan, not a wrong reading.
        """
        if not isinstance(payload, dict):
            return False
        base_addr = payload.get("base_addr")
        models = payload.get("models")
        if not isinstance(base_addr, int) or not isinstance(models, list) or not models:
            return False
        structure: list[tuple[int, int, int]] = []
        for entry in models:
            if not isinstance(entry, (list, tuple)) or len(entry) != 3:
                return False
            if not all(isinstance(value, int) for value in entry):
                return False
            structure.append((entry[0], entry[1], entry[2]))
        structure.sort(key=lambda item: item[1])
        self._base_addr = base_addr
        self._model_structure = structure
        self._log.debug(
            "Adopted a persisted SunSpec layout with %d model(s), pending validation",
            len(structure),
        )
        return True

    async def _async_scan_or_restore(self, client: SunSpecClient) -> None:
        """Give ``client`` its model objects, scanning only when needed.

        After a restart, a reload or a transport failure the models are
        rebuilt from the cache after three validating reads. A changed
        layout or an explicit rescan walks the model tree instead.
        """
        if await self._async_restore_model_structure(client):
            self._log.debug(
                "Restored %d cached SunSpec models, skipping scan",
                len(self._model_structure or []),
            )
            return
        self._log.debug("Scanning SunSpec model tree")
        try:
            await client.async_scan(
                connect=False,
                progress=progress,
                full_model_read=False,
                # A delay of 0 means no pacing at all. pysunspec2 skips
                # the sleep only for None, and asyncio.sleep(0) would
                # still hand the loop over on every model.
                delay=self._scan_delay or None,
            )
        except (SunSpecModbusClientTimeout, ModbusClientTimeout, ModbusClientConnectionClosed):
            # A timeout is never swallowed, unlike the other Modbus
            # errors below. A request went unanswered, so the scan is
            # missing a block and nothing here can tell which.
            # Continuing with the models found so far would cache a
            # chain that stops short of the device's real one. Failing
            # the cycle drops the link, and the next connect scans
            # again. A device that hung up mid-scan is the same case.
            raise
        except (SunSpecModbusClientError, ModbusClientError) as err:
            # Not a timeout, so the device did answer, typically with a
            # Modbus exception code for a block it will not serve. A
            # truncated model list is the worst way to continue: it
            # drops entities to "unknown" while the cycle still counts
            # as a success, so the stale-data tolerance never holds the
            # last good value, and the user sees a gap that looks like
            # a connection drop (#42). The last known good layout is
            # preferred, if it still validates against the device.
            if await self._async_restore_model_structure(client):
                self._log.warning(
                    "SunSpec scan stopped early (%s: %s). Continuing with the last known "
                    "good layout of %d model(s), which still validates against the device.",
                    err.__class__.__name__,
                    err,
                    len(self._model_structure or []),
                )
                return
            # No usable cache, so this is a first scan. pysunspec2 calls
            # add_model() as it walks the chain, so the models read
            # before the failing block are in ``client.models``. They
            # are kept: raising here would turn one unreadable block
            # into "this inverter is not SunSpec" (SMA STP110-60,
            # cjne/ha-sunspec#375).
            if not _discovered_model_count(client):
                raise
            self._partial_scan = True
            self._log.warning(
                "SunSpec scan stopped early (%s: %s). Continuing with the %d model(s) "
                "found before the failure; the rest of the chain was not read.",
                err.__class__.__name__,
                err,
                _discovered_model_count(client),
            )
            # There is no _capture_model_structure() here on purpose. A
            # cached truncated layout would keep the missing models away
            # for good, and it can validate while doing so: a truncated
            # chain still has a well-formed tail. With an empty cache
            # the next connect scans again and picks up the rest as
            # soon as the device answers.
            return
        self._partial_scan = False
        self._capture_model_structure(client)

    async def _async_relabel_models(self, client: SunSpecClient) -> None:
        """Replace each model whose header the vendor's firmware labels wrongly.

        Runs after the scan and after the cache restore, and after
        :meth:`_capture_model_structure`, so the cache keeps the ids and
        lengths the device really holds and validates against them. A
        header is only suspect when some profile lists its id and
        length; then one read of ``Mn`` in model 1 says whether the
        device is that vendor's. Any other device costs no read.
        """
        suspects = [
            model
            for key, instances in client.models.items()
            if isinstance(key, int)
            for model in instances
            if any(
                relabel.found_id == model.model_id and relabel.found_len == model.model_len
                for profile in PROFILES
                for relabel in profile.model_relabels
            )
        ]
        common = client.models.get(1)
        if not suspects or not common:
            return
        manufacturer = await client.async_read(common[0].model_addr + 2, 16)
        if not manufacturer:
            return
        profile = profile_for(mb.data_to_str(manufacturer))
        if profile is None:
            return
        for model in suspects:
            for relabel in profile.model_relabels:
                if (relabel.found_id, relabel.found_len) == (model.model_id, model.model_len):
                    await self._async_relabel_model(client, model, relabel)
                    break

    async def _async_relabel_model(
        self, client: SunSpecClient, model: Any, relabel: ModelRelabel
    ) -> None:
        """Swap ``model`` for the model its registers are laid out as."""
        self._log.info(
            "Reading model %s of %s registers at address %s as model %s of %s registers "
            "(vendor firmware labels it wrongly)",
            model.model_id,
            model.model_len,
            model.model_addr,
            relabel.as_id,
            relabel.as_len,
        )
        for key in list(client.models):
            instances = client.models[key]
            if model in instances:
                instances.remove(model)
            if not instances:
                del client.models[key]
        if model in client.model_list:
            client.model_list.remove(model)
        header = mb.u16_to_data(relabel.as_id) + mb.u16_to_data(relabel.as_len)
        data = await client.async_model_data(relabel.as_id, model.model_addr, header)
        relabelled = client.model_class(
            model_id=relabel.as_id,
            model_addr=model.model_addr,
            model_len=relabel.as_len,
            data=data,
            mb_device=client,
        )
        relabelled.mid = model.mid
        client.add_model(relabelled)

    def _capture_model_structure(self, client: SunSpecClient) -> None:
        """Remember the layout a complete scan just discovered."""
        structure: list[tuple[int, int, int]] = []
        # The layout is read from ``client.models``, not from
        # ``client.model_list``: add_model() appends to model_list only
        # for a model with a group name, so a vendor block without a
        # bundled model definition would vanish from the cache and stop
        # being polled after the first reconnect.
        try:
            for key, instances in client.models.items():
                if not isinstance(key, int):
                    continue
                for model in instances:
                    structure.append((model.model_id, model.model_addr, model.model_len))
        except (AttributeError, TypeError) as err:
            # The cache is an optimisation and must never break a poll.
            # Anything unexpected in the client's model map means "no
            # cache", not "no data".
            self._log.debug("Could not capture the model structure: %s", err)
            self._invalidate_model_structure()
            return
        if not structure:
            self._invalidate_model_structure()
            return
        # Address order is the order the device lays the blocks out,
        # which is the order a scan rediscovers them in.
        structure.sort(key=lambda entry: entry[1])
        if structure == self._model_structure and client.base_addr == self._base_addr:
            # The rescan found the layout that is already cached. The
            # revision stays, so the coordinator does not rewrite its
            # store file.
            return
        if self._model_structure is not None:
            self._log.info(
                "SunSpec model layout changed: %d model(s) before, %d now. Adopting the new one.",
                len(self._model_structure),
                len(structure),
            )
        self._base_addr = client.base_addr
        self._model_structure = structure
        self.structure_revision += 1

    async def _async_validate_model_structure(
        self, client: SunSpecClient, structure: list[tuple[int, int, int]]
    ) -> bool:
        """Re-read both ends of the cached chain. False means "rescan".

        Three short reads check that the base address still answers with
        the ``SunS`` marker and the first model id, that the last block
        still carries the recorded id and length, and that the register
        behind the last block is still the end marker.

        A link failure (``_LINK_FAILURES``) is raised, not answered with
        False.
        """
        try:
            header = await client.async_read(self._base_addr, 3)
        except _LINK_FAILURES:
            raise
        except Exception as err:  # noqa: BLE001 - any failure just means "scan"
            self._log.debug("Cached model structure could not be validated: %s", err)
            return False
        if not header or len(header) < 6 or header[:4] != b"SunS":
            self._log.debug("Cached base address no longer answers with the SunSpec marker")
            return False
        if mb.data_to_u16(header[4:6]) != structure[0][0]:
            self._log.info("SunSpec model tree changed at the base address, rescanning")
            return False

        # The header check alone proves nothing: the first model is 1
        # (Common) on every SunSpec device. The chain is a linked list
        # built from lengths, so a change in the middle moves the tail.
        # The tail read therefore catches a firmware update that
        # reorders the tree and a scan that misread a length and cached
        # a shifted layout. A shifted layout read at the old offsets
        # returns wrong values, not missing ones, which is far harder
        # to notice than a failed poll.
        last_id, last_addr, last_len = structure[-1]
        try:
            tail = await client.async_read(last_addr, 2)
        except _LINK_FAILURES:
            raise
        except Exception as err:  # noqa: BLE001 - any failure just means "scan"
            self._log.debug("Cached model chain tail could not be validated: %s", err)
            return False
        if not tail or len(tail) < 4:
            self._log.debug("Cached model chain tail returned no data")
            return False
        found_id = mb.data_to_u16(tail[0:2])
        found_len = mb.data_to_u16(tail[2:4])
        if found_id != last_id or found_len != last_len:
            self._log.info(
                "SunSpec model tree changed: expected model %s of %s registers at address %s, "
                "found model %s of %s registers. Rescanning.",
                last_id,
                last_len,
                last_addr,
                found_id,
                found_len,
            )
            return False
        # The end marker catches a firmware update that only appends
        # models: the tail block is still where it was, but the chain no
        # longer ends behind it. A device that refuses this read is
        # accepted, since pysunspec2's own scan tolerates a chain that
        # stops answering.
        try:
            end = await client.async_read(last_addr + 2 + last_len, 1)
        except _LINK_FAILURES:
            raise
        except Exception as err:  # noqa: BLE001 - device need not answer past the chain
            self._log.debug("No readable end marker behind the cached chain (%s), accepting", err)
            return True
        # Id 0 is where a scan stops on a device whose chain ends in
        # blank memory instead of the marker (APsystems ECU-R, #109).
        if end and len(end) >= 2 and mb.data_to_u16(end[0:2]) not in (mb.SUNS_END_MODEL_ID, 0):
            self._log.info(
                "SunSpec model chain continues past its cached end, rescanning to pick up "
                "the models behind it"
            )
            return False
        return True

    async def _async_restore_model_structure(self, client: SunSpecClient) -> bool:
        """Rebuild ``client.models`` from the cache. False means "scan instead".

        A mismatch returns False so the caller can scan. A timeout or lost
        connection propagates instead of starting a scan on a failing link.
        """
        structure = self._model_structure
        if not structure or self._base_addr is None:
            return False
        if not await self._async_validate_model_structure(client, structure):
            return False

        client.base_addr = self._base_addr
        client.delete_models()
        try:
            for index, (model_id, model_addr, model_len) in enumerate(structure):
                # The model constructor takes the id and length registers
                # plus the registers up to the last group count. It
                # cannot read on its own, so async_model_data reads the
                # group counts here first, as the scan does.
                data = mb.u16_to_data(model_id) + mb.u16_to_data(model_len)
                more = getattr(client, "async_model_data", None)
                if more is not None:
                    data = await more(model_id, model_addr, data)
                model = client.model_class(
                    model_id=model_id,
                    model_addr=model_addr,
                    model_len=model_len,
                    data=data,
                    mb_device=client,
                )
                model.mid = f"{client.did}_{index}"
                client.add_model(model)
        except _LINK_FAILURES:
            # A model with counted groups is read from while it is
            # rebuilt (async_model_data), so the link can fail here too.
            client.delete_models()
            raise
        except Exception as err:  # noqa: BLE001 - a half-built client is worse than none
            self._log.debug("Rebuilding models from cache failed, falling back to scan: %s", err)
            client.delete_models()
            self._invalidate_model_structure()
            return False
        return True

    async def async_close(self, force: bool = False) -> None:
        """Drop the live client's link and the reference to it.

        The next ``async_get_client()`` builds a new client on the same
        connection, which connects again on its first request.

        ``force`` marks a session that is already suspect: a failed
        cycle, or an unload. The flag only changes the log:
        modbus-connection owns the socket, and the link is dropped the
        same way with or without it.
        """
        if self._client is None:
            return
        client = self._client
        self._client = None
        if force:
            self._log.debug("Dropping a suspect Modbus session")
        await self._async_disconnect(client)

    async def async_require_timeout(self, seconds: float) -> None:
        """Have the link wait ``seconds`` for an answer, from the next request on.

        The coordinator calls this once the vendor profile is known.
        Before the connection exists the value is only kept, and applied
        when the next client connects.
        """
        if self._request_timeout == seconds:
            return
        self._request_timeout = seconds
        if self._connection is not None:
            await self._connection.async_require_timeout(self._unit_id, seconds)

    async def async_shutdown(self) -> None:
        """Close the client and the connection for good: the entry is unloading."""
        self._shutdown = True
        await self.async_close(force=True)
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        # Another holder of the link has no reason to wait as long as the
        # timeout this entry asked for.
        connection.withdraw_timeout(self._unit_id)
        try:
            await connection.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            self._log.debug("connection close raised %s, ignoring", exc)

    async def _async_disconnect(self, client: SunSpecClient) -> None:
        """Drop the link of ``client``. Best effort: the cleanup never raises.

        The client is an argument because ``modbus_connect`` has to drop
        a client that never became ``self._client``.
        """
        try:
            await client.async_disconnect()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            self._log.debug("client disconnect raised %s, ignoring", exc)

    def _params(self) -> ModbusTcpParams | ModbusSerialParams:
        """What modbus-connection connects to, from the entry's transport."""
        if self._transport == TRANSPORT_RTU:
            if not self._serial_port:
                raise TransportError(
                    "Serial port is not configured but transport=rtu was requested"
                )
            parity: Literal["N", "E"] = "E" if self._parity == PARITY_EVEN else "N"
            return ModbusSerialParams(
                device=self._serial_port, baudrate=self._baudrate, parity=parity
            )
        return ModbusTcpParams(host=self._host, port=self._port)

    def _get_connection(self) -> SharedConnection:
        """The connection object, built on first use. Constructing it does no I/O."""
        # A retry or a pending scan may resume after HA unloaded the entry.
        if self._shutdown:
            raise TransportError("SunSpec client is shut down")
        if self._connection is None:
            self._connection = SharedConnection(self._hass, self._params(), self._entry)
        return self._connection

    async def _async_get_connection(self) -> SharedConnection:
        """The connection with this device's unit taken on it. Does no I/O."""
        connection = self._get_connection()
        try:
            await connection.async_for_unit(self._unit_id)
        except HomeAssistantError as err:
            # Another integration already talks to this endpoint with
            # other link settings, and one connection cannot be both.
            raise TransportError(str(err)) from err
        if self._request_timeout is not None:
            await connection.async_require_timeout(self._unit_id, self._request_timeout)
        return connection

    async def modbus_connect(self) -> SunSpecClient:
        """Build a new pysunspec2 client on the connection and give it its models.

        The tests patch this method to hand in a file-backed client,
        which is why it is the seam and not ``async_get_client``.

        Raises:
            TransportError: The link failed or timed out during the scan
                or the check of the stored layout.
            ProtocolError: No SunSpec map was found on the device.
        """
        endpoint = (
            f"{self._serial_port} @ {self._baudrate} {self._parity}"
            if self._transport == TRANSPORT_RTU
            else f"{self._host}:{self._port}"
        )
        self._log.debug("Connecting to %s unit id %s", endpoint, self._unit_id)
        # The model definitions are JSON files, read on first use. The
        # scan and the cache restore look them up on the event loop,
        # where an open() trips Home Assistant's blocking-call detector.
        # Reading all of them once in the executor leaves nothing to
        # open later.
        await self._hass.async_add_executor_job(preload_model_defs)
        connection = await self._async_get_connection()
        client = SunSpecModbusClientDeviceUnit(connection, slave_id=self._unit_id)
        self._wrap_capturing_read(client)
        # There is no probe connect in front of the real one, and none
        # must be added. On an inverter that grants one Modbus session a
        # probe makes two connects for one slot, and the second loses:
        # 8 of 12 cycles failed on a KACO Powador 7.8 TL3 with a probe
        # in front, 0 of 12 without it.
        #
        # The link is dropped on every failure path. async_get_client()
        # adopts the client only on success, so after a connect that
        # succeeds and a scan that fails (#25) nothing else would drop
        # the session, and the next attempt would start on a link that
        # has just failed.
        handed_off = False
        try:
            await client.async_connect()
            await self._async_scan_or_restore(client)
            if not client.is_connected():
                raise TransportError(f"Failed to connect to {endpoint} unit id {self._unit_id}")
            await self._async_relabel_models(client)
            handed_off = True
            return client
        except ModbusClientError as err:
            raise TransportError(
                f"Modbus error while connecting to {endpoint} unit id {self._unit_id}: {err}"
            ) from err
        except SunSpecModbusClientError as err:
            # The scan raises this when it finds no SunSpec map at any
            # base address. A timeout or a lost link during the scan is
            # a ModbusClientError and was handled above. Translating the
            # error here keeps the scan's own message in front of the
            # user, who otherwise sees a generic "Unexpected error".
            raise ProtocolError(
                f"SunSpec scan failed for {endpoint} unit id {self._unit_id}: {err}"
            ) from err
        finally:
            if not handed_off:
                await self._async_disconnect(client)

    def _wrap_capturing_read(self, client: SunSpecClient) -> None:
        """Wrap ``client.async_read`` so every read is recorded in ``self._captured_reads``.

        The diagnostics dump includes the captured reads, so a user can
        attach a reproducible fixture to a bug report. The list is
        capped at 1000 entries to bound the JSON size. Does nothing when
        capture is disabled.
        """
        if not self._capture_enabled:
            return
        original_read = client.async_read

        # The signature must stay compatible with the method it replaces.
        # The unit device's async_read takes a third argument ``op`` that
        # selects the Modbus function code. A two-parameter wrapper would
        # raise TypeError for a call that passes ``op``, and only while
        # capture is enabled, which is when a user is already debugging.
        async def capturing_read(addr: int, count: int, *args: Any, **kwargs: Any) -> Any:
            data = await original_read(addr, count, *args, **kwargs)
            if len(self._captured_reads) < 1000:
                self._captured_reads.append(
                    {
                        "ts": time.time(),
                        "addr": addr,
                        "count": count,
                        "hex": data.hex() if data else None,
                    }
                )
            return data

        client.async_read = capturing_read

    async def async_read_model(self, model_id: int) -> SunSpecModelWrapper:
        try:
            return await self._async_read_model_once(model_id)
        except ModbusClientConnectionClosed:
            # The device hung up mid-cycle. Some devices do that after
            # every request or after a short idle time (APsystems ECU-R,
            # cjne/ha-sunspec#170). Without this reconnect the read
            # fails the whole cycle, only the next cycle connects again,
            # and every second poll on such a device is a failure. The
            # link is already gone, so dropping the client is all there
            # is to do; the cached layout survives and the rebuild costs
            # three validating reads, not a scan. There is one attempt:
            # a device that hangs up on the new session too is not
            # answering.
            self._log.info("Device closed the Modbus connection, connecting again")
            await self.async_close()
            return await self._async_read_model_once(model_id)

    async def _async_read_model_once(self, model_id: int) -> SunSpecModelWrapper:
        client = await self.async_get_client()
        models = client.models[model_id]
        for model in models:
            await self._pace_model_read()
            await model.async_read()
            self._last_model_read_end = time.monotonic()

        if model_id == 1:
            common = SunSpecModelWrapper(models)
            profile = profile_for(*(point_text(common, key) for key in ("Mn", "Md", "Opt", "Vr")))
            self._point_transforms = profile.point_transforms if profile is not None else {}
            self._signed_nan_points = (
                profile.signed_nan_points if profile is not None else frozenset()
            )
        transforms = {
            point: transform
            for (target_model, point), transform in self._point_transforms.items()
            if target_model == model_id
        }
        signed_nan = frozenset(
            point for target_model, point in self._signed_nan_points if target_model == model_id
        )
        return SunSpecModelWrapper(models, transforms, signed_nan)

    async def _pace_model_read(self) -> None:
        """Keep the configured pause between the end of one model read and the next."""
        # The pause is measured from the end of the previous read, not
        # slept before every read. The first read of a cycle follows an
        # idle interval or a new connect and has nothing to be spaced
        # from: a pause in front of it is a pure wait, once per entry,
        # and the eight entries behind one APsystems ECU-R spent it
        # eight times in a row on startup (#126).
        if not self._model_read_delay or self._last_model_read_end is None:
            return
        remaining = self._model_read_delay - (time.monotonic() - self._last_model_read_end)
        if remaining > 0:
            await asyncio.sleep(remaining)
