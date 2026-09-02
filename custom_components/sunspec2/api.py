"""Sample API Client."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import Literal

from homeassistant.core import HomeAssistant
from modbus_connection import ModbusSerialParams
from modbus_connection import ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection

from .const import DEFAULT_BAUDRATE
from .const import DEFAULT_SCAN_DELAY_SECONDS
from .const import MAX_SCAN_DELAY_SECONDS
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

# The embedded pysunspec2 is untyped, so every object it hands back reaches
# mypy as Any. The alias names which Any is meant: a connected
# SunSpecModbusClientDevice over a modbus-connection unit. It buys no
# checking, only a name a reader of a signature can look up.
type SunSpecClient = Any

# Modbus request timeout (seconds): how long modbus-connection waits for
# the answer to one request, and for the connect in front of it.
#
# Was 120 historically, which is wildly too generous: when the inverter
# silently dropped the link, every coordinator update would block the
# event loop for two full minutes per cycle waiting on a connect that
# was never going to come back. With a 30s scan interval that meant a
# single bad cycle delayed three normal cycles, and the per-gateway
# lock starved any other config entry behind the same TCP endpoint.
#
# 10s is the steady-state ceiling: an inverter that has not answered
# after ten seconds is gone, full stop. The in-cycle retry in the
# coordinator (5s sleep then one more attempt) and the stale-data
# tolerance in the entity available property cover the actual
# flaky-network case much better than a long socket timeout ever did.
TIMEOUT = 10

# There is no separate connect timeout any more. The embedded TCP client
# had one of 2 s, measured against real hardware where every successful
# connect landed between 0.08 s and 0.33 s, so a connect still open after
# two seconds was a refused one: the inverter had no free Modbus session
# and dropped the SYN. modbus-connection hands its backend one timeout and
# leaves tmodbus's own connect timeout at 10 s, so a refused connect now
# costs TIMEOUT. If that shows up as a lost cycle after the nightly drop,
# the fix is upstream: pass connect_timeout through ModbusConnection.

# Initial-setup socket timeout (seconds). The very first
# ``client.scan()`` after a fresh connect walks every SunSpec model
# block on the device, which can be 16+ models deep on a fully featured
# inverter and is much slower than a single read in steady state. The
# steady-state TIMEOUT of 10s is too tight for that walk on slower
# devices (notably KACO Powador on 100 Mbit), so the config-flow probe
# and the diagnostics probe pass this longer timeout to the API client.
SETUP_TIMEOUT = 60

# Pause before every model instance async_read_model reads (seconds).
# Inherited verbatim from cjne/ha-sunspec in the phase 0 baseline, where
# it paced the request stream for inverters that answer slowly, and never
# re-examined since. A named constant so the test suite can switch it
# off: the file-backed test client needs no pacing, and at 0.6 s per
# model every test that sets the integration up paid two seconds for
# nothing, most of the suite's three minutes.
MODEL_READ_PACING_SECONDS = 0.6

_LOGGER: logging.Logger = logging.getLogger(__package__)


# pragma: not covered
def progress(msg: str) -> bool:
    _LOGGER.debug(msg)
    return True


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
    """Modbus client wrapper, instance-scoped lifecycle.

    Phase 4 dropped the class-level CLIENT_CACHE that the cjne version
    inherited. The cache was the root cause of the hot-reload bug:
    config-entry reloads created a new SunSpecApiClient instance, but
    get_client() would reach into the shared cache and reuse a stale
    pysunspec2 client whose TCP socket had been replaced by an unrelated
    options-flow probe earlier in the same HA process. With each api
    instance owning exactly one client there is no cross-instance
    interference, and async_unload_entry's close() reliably tears down
    the only socket before the next async_setup_entry builds a fresh one.
    """

    def __init__(
        self,
        host: str,
        port: int,
        unit_id: int,
        hass: HomeAssistant,
        capture_enabled: bool = False,
        timeout: int = TIMEOUT,
        transport: str = TRANSPORT_TCP,
        serial_port: str | None = None,
        baudrate: int = DEFAULT_BAUDRATE,
        parity: str = PARITY_NONE,
        scan_delay: float = DEFAULT_SCAN_DELAY_SECONDS,
    ) -> None:
        """Sunspec modbus client.

        Two transports are supported:

        * ``transport="tcp"`` (default): connects to ``host:port`` over
          Modbus TCP. ``host``, ``port`` and ``unit_id`` are the only
          relevant config keys; the serial-line parameters are
          ignored.
        * ``transport="rtu"``: connects to ``serial_port`` over
          Modbus RTU (RS-485 typically via a USB adapter on
          ``/dev/ttyUSB0`` or ``COM3``). ``unit_id``, ``serial_port``,
          ``baudrate`` and ``parity`` are the relevant config keys;
          ``host`` and ``port`` are kept only so the logger and
          diagnostics dump can render a stable identifier (we use
          ``serial_port:baudrate`` as the synthetic host string).

        ``host`` is always required for the logger adapter even on
        RTU - it ends up in every log line as ``[host:port#unit_id]``
        prefix - so RTU callers pass the serial port name as ``host``
        and ``baudrate`` as ``port``. The coordinator does this
        synthesis transparently.
        """

        self._host = host
        self._port = port
        self._hass = hass
        self._unit_id = unit_id
        # Steady-state coordinator instances pass nothing here and get
        # the short ``TIMEOUT``. The config-flow probe and any other
        # one-shot caller that needs to walk the full SunSpec model tree
        # passes ``timeout=SETUP_TIMEOUT`` so the initial scan has time
        # to finish on slower devices.
        self._timeout = timeout
        # Seconds pysunspec2 sleeps between models during scan(). Until
        # v0.22.0 the coordinator closed the client after every cycle,
        # so every poll rescanned and paid this once per model. The
        # session is held open now, and the layout is cached (v0.17.0)
        # and persisted across restarts (v0.21.0), so scan() only runs
        # when there is no usable cached layout: a first setup with
        # nothing stored, or a reconnect after a failure, which drops
        # the cache on purpose. Paid per model on that rare walk.
        # See CONF_SCAN_DELAY.
        # Clamped here rather than trusting the caller because a
        # corrupted options save reaching pysunspec2 as a negative
        # sleep would raise ValueError deep inside the scan walk.
        self._scan_delay = min(
            MAX_SCAN_DELAY_SECONDS, max(MIN_SCAN_DELAY_SECONDS, float(scan_delay))
        )
        self._transport = transport
        self._serial_port = serial_port
        self._baudrate = baudrate
        self._parity = parity
        self._reconnect = False
        self._client: SunSpecClient | None = None
        # The connection outlives the client. A close drops the link and
        # the model objects; the next client connects on the same object,
        # which modbus-connection reconnects on demand.
        self._connection: ModbusConnection | None = None
        # Cached SunSpec layout: base address plus (model_id, addr, len)
        # per block. Survives close(), which since v0.22.0 only runs on
        # unload, on the failure path, and after a poll or a write when
        # the slot has to be handed back (CONF_RELEASE_SLOT, or two
        # config entries behind one gateway), and is dropped by
        # reconnect_next(), which only runs after a failure: the one
        # situation where the layout is a suspect rather than an asset.
        #
        # Deliberately without an expiry. A SunSpec model tree changes on
        # a firmware update and never otherwise, so a timer can only ever
        # rescan a layout that was still correct, and the rescan is the
        # one operation that can silently corrupt it. pysunspec2 walks
        # the chain with ``addr += model_len + 2``, so a single misread
        # length shifts every block behind it and scan() still returns
        # without raising. What guards against a real change is
        # :meth:`_validate_model_structure`, which re-reads both ends of
        # the chain on every reconnect.
        self._base_addr: int | None = None
        self._model_structure: list[tuple[int, int, int]] | None = None
        # Bumped on every layout change so the coordinator can tell when
        # what it persisted is stale without diffing the lists.
        self.structure_revision: int = 0
        # True while the model list in the live client is known to be
        # incomplete. The coordinator reads it through
        # :attr:`last_scan_was_partial` and refuses to treat a shortened
        # list as "these models are gone".
        self._partial_scan = False
        self._log = get_adapter(host, port, unit_id)
        self._capture_enabled = capture_enabled
        self._captured_reads: list[dict[str, Any]] = []
        self._log.debug(
            "New SunspecApi Client (transport=%s, capture=%s, timeout=%ds, scan_delay=%ss)",
            transport,
            capture_enabled,
            timeout,
            self._scan_delay,
        )

    async def async_get_client(self) -> SunSpecClient:
        """Return the active pysunspec2 client, building it on first use.

        On the explicit reconnect path (``reconnect_next()`` set
        ``_reconnect=True`` after the previous cycle failed) the old
        client's link is dropped first, so a single-slot inverter has
        its slot back before the new client asks for it. Otherwise the
        client that is already open is handed straight back. Since
        v0.22.0 that is the steady state: one session serves the 16+
        ``async_read_model`` calls of a cycle and every cycle after it,
        until a failure, an unload, or a close between polls
        (CONF_RELEASE_SLOT, or two config entries behind one gateway)
        drops it - hence the conditional, not an unconditional rebuild
        on every entry.
        """
        # Clear the flag whether or not there was a client to drop. It
        # arrives both ways: _after_failed_cycle closes before setting
        # it, so _client is already None there, while the in-cycle retry
        # sets it on a session that is still up.
        #
        # Until v0.22.0 there was never a client here, because every
        # cycle ended in close() and that set _client to None. So when
        # the flag was set by a failed cycle it used to survive into
        # the next one, the first get_client() built a client with the
        # flag still standing, and the SECOND get_client() of that same
        # cycle tore that fresh client down and built another one.
        # Every cycle following a failure therefore cost two full
        # connects and two full model scans, on hardware that grants
        # exactly one Modbus session at a time - which is how a single
        # failed cycle turned into a run of them (#42).
        if self._reconnect:
            if self._client is not None:
                await self._async_disconnect(self._client)
                self._client = None
            self._reconnect = False
        if self._client is None:
            self._client = await self.modbus_connect()
        return self._client

    def known_models(self) -> list[int]:
        """Return integer model IDs the active client has already discovered.

        Returns an empty list if no client is alive yet. The options flow
        uses this for its model-selection form: it must NOT force a fresh
        TCP connect (which would race the coordinator's active socket on
        inverters with a single Modbus TCP slot like KACO Powador). The
        coordinator already discovered the models during async_setup_entry,
        so the form just reads what we know.
        """
        if self._client is None:
            return []
        return sorted(m for m in self._client.models if isinstance(m, int))

    async def async_get_data(self, model_id: int) -> SunSpecModelWrapper:
        with_model = SunSpecLoggerAdapter(
            self._log.logger, {**self._log.bound, "model_id": model_id}
        )
        # The model layer raises the fork's SunSpec* classes for what it
        # notices itself and lets the transport's ModbusClient* classes
        # through as they are; both sides of each pair land in the same
        # category. Subclasses before their bases: the timeout and the
        # closed connection both derive from ModbusClientError.
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
        """Write one or more points on a SunSpec model as a single batch.

        v0.12.0 EXPERIMENTAL: this is the only path the new Number /
        Switch / service-action write features take. Resolves the
        points on the live client, sets each ``cvalue`` (which lets
        pysunspec2 handle the scale-factor encoding for us), and
        flushes them together via ``model.write()``. That emits one
        Modbus frame per run of consecutive registers, so a batch
        touching adjacent points costs a single write.

        Errors translate into the same typed exception hierarchy the
        read path uses, so the service handler / entity layer can
        surface them as ``HomeAssistantError`` with consistent
        messaging.

        The handler chain below has to be this wide because pysunspec2
        has three unrelated exception roots and only one of them
        (``SunSpecModbusClientError``) was covered before v0.13.4:

        - ``ModelError`` from ``pysunspec2/device.py`` is a plain ``Exception``. It is
          what an unread scale factor or an unimplemented ``*_SF``
          register produces on the ``cvalue`` assignment.
        - ``SunSpecModbusValueError`` is a plain ``Exception`` too,
          despite the name suggesting it sits under the client tree.
        - ``ModbusClientError`` and its subclasses come from the
          transport layer below the SunSpec client and are the ones
          raised when the device NAKs the write with a Modbus
          exception code or the link fails. The unit device raises
          them as they are, not translated into the SunSpec hierarchy.

        Anything left uncaught here reaches the Number / Switch entity
        as a raw traceback in the UI, because their ``except
        SunSpecError`` never matches. Subclasses must be listed before
        their bases: ``ModbusClientTimeout`` and
        ``SunSpecModbusClientTimeout`` are transient, their respective
        base classes are not.
        """
        with_model = SunSpecLoggerAdapter(
            self._log.logger, {**self._log.bound, "model_id": model_id}
        )
        with_model.debug("Write %s", ", ".join(f"{n} = {v!r}" for n, v in points))
        point_name = ", ".join(name for name, _ in points)
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
            # assignment cannot encode the value. Re-reading the model
            # (see _write_points_blocking) fixes the merely-unread case,
            # not this one, so say what is actually wrong.
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
        see ``raw_blocks.py``. Same session, same lock discipline as
        the model reads: the coordinator calls this inside its cycle.
        ``unit_id_offset`` asks another unit on the same connection,
        the entry's unit id plus the offset: SMA keeps its own
        registers 123 unit ids below the SunSpec ones.

        Raises:
            DeviceError: The device answered with a Modbus exception,
                or with fewer registers than asked.
            TransientError: No answer within the socket timeout.
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
        read_unit = getattr(client, "async_read_unit", None)
        if read_unit is not None:
            other: bytes | None = await read_unit(unit_id, address, count)
            return other
        swapped: bytes | None = await self._as_unit(
            client, unit_id, lambda: client.async_read(address, count)
        )
        return swapped

    async def _as_unit(
        self, client: SunSpecClient, unit_id: int, action: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Run ``action`` with the session's unit id swapped for ``unit_id``.

        For a device that binds the unit id to the device object rather
        than to the request, as the fork's own clients do through
        ``slave_id``. Swapping it for one request is safe because the
        coordinator holds the gateway lock around every use of the
        session.
        """
        targets = [
            obj
            for obj in (client, getattr(client, "client", None))
            if obj is not None and hasattr(obj, "slave_id")
        ]
        saved = [obj.slave_id for obj in targets]
        for obj in targets:
            obj.slave_id = unit_id
        try:
            return await action()
        finally:
            for obj, value in zip(targets, saved, strict=True):
                obj.slave_id = value

    async def async_write_block(self, address: int, data: bytes, unit_id_offset: int = 0) -> None:
        """Write register bytes at ``address`` over the live session.

        Raises:
            TransientError: No answer within the socket timeout.
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
        write_unit = getattr(client, "async_write_unit", None)
        if write_unit is not None:
            await write_unit(unit_id, address, data)
            return
        await self._as_unit(client, unit_id, lambda: client.async_write(address, data))

    @staticmethod
    def _encodable(point: Any, value: object) -> object:
        """``value`` as the point's Modbus encoder takes it: an integer point gets an int.

        Home Assistant carries every Number's state as a float, so a
        Number over a whole-number register hands this path 303.0 where
        ``struct.pack`` wants 303. pysunspec2 rounds on its own for a
        *scaled* point, dividing by the scale factor, so those are left
        alone: 12.5 % with a scale factor of -1 has to stay 12.5.
        A point without one takes the value as given and raised
        "required argument is not an integer" on every seconds and
        percent register that carries no scale factor, the Fronius
        battery revert time in #57 among them.

        Args:
            point (Any): The pysunspec2 point being written.
            value (object): What the caller wants in it.

        Returns:
            object: The value, rounded to an int where the register holds one.
        """
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
        # SunSpec model blocks can repeat (multiple MPPT modules etc.)
        # but model 123 (immediate controls) is always a single
        # instance per inverter, so we always target index 0.
        model = models[0]
        resolved = []
        for point_name, value in points:
            try:
                resolved.append((model.points[point_name], value))
            except KeyError as exc:
                raise DeviceError(f"Point {point_name} not present in model {model_id}") from exc
        # Populate the model block before touching cvalue. Without this,
        # every scaled write fails on real hardware with
        #   ModelError: SF field WMaxLimPct_SF value not initialized
        # (reported for a KACO bp 20.0 NX3 M2 in #17, but it is not
        # vendor specific - it hits every device and every scaled point).
        #
        # Why the point can be empty: neither path that fills
        # ``client.models`` populates it. scan() runs with
        # full_model_read=False and pysunspec2 only calls model.read()
        # during scan when that flag is set; rebuilding from the cached
        # layout writes only the id and length registers. So on a
        # session that has not polled this model yet - one rebuilt after
        # a failure-driven reconnect, or after the close that a
        # slot-releasing setup does between polls - every point except
        # ID and L is still None here, including the scale factor that
        # Point.set_value(computed=True) needs to encode the value.
        # Since v0.22.0 the write usually lands on the very session the
        # coordinator polls on, where the points are stale rather than
        # absent. The read covers both.
        #
        # The switches never hit this because Conn, WMaxLim_Ena and
        # OutPFSet_Ena are enum16 with sf: null, so sf_required is False.
        # That is why #17 reported working switches and broken numbers.
        #
        # Cost is one block read of len + 2 registers, which is noise
        # next to the round trips of the write itself. It no longer
        # rides along on a reconnect and a full scan: since v0.22.0 the
        # write normally lands on the session the coordinator already
        # has open. Read before setting cvalue, never after:
        # Group.read() ends in set_mb(dirty=False) and would discard a
        # pending value.
        await model.async_read()
        for point, value in resolved:
            point.cvalue = self._encodable(point, value)
        # One flush for the whole batch, not one per point.
        # ``Group.write_points`` only emits the points it finds dirty
        # and coalesces consecutive registers into a single frame, so
        # "set the limit and enable it" leaves as one Modbus write on
        # model 704, where WMaxLimPctEna and WMaxLimPct are adjacent.
        # Writing point by point also meant one model.read() per point,
        # since each call re-entered this function.
        #
        # It matters beyond efficiency: with two writes the inverter can
        # act on the first before the second arrives, so a limit briefly
        # applies at the old percentage or the new percentage briefly
        # applies while still disabled. Borrowed from
        # milanhin/pv_curtailment, which does the same thing for the
        # same reason.
        await model.async_write()

    async def async_get_device_info(self) -> SunSpecModelWrapper:
        return await self.read(1)

    async def async_get_models(self) -> list[int]:
        self._log.debug("Fetching models")
        client = await self.async_get_client()
        model_ids = sorted(list(filter(lambda m: type(m) is int, client.models.keys())))
        return model_ids

    def reconnect_next(self) -> None:
        """Force a fresh client, and a fresh scan, on the next get_client().

        Drops the cached model layout too. This only ever runs after a
        failed cycle, and a layout read at addresses that just stopped
        answering is exactly the thing not to reuse: a different device
        answering on a recycled IP would otherwise be read at the
        previous device's offsets and return plausible garbage rather
        than an error.
        """
        self._reconnect = True
        self._invalidate_model_structure()

    def _invalidate_model_structure(self) -> None:
        if self._model_structure is None and self._base_addr is None:
            return
        self._base_addr = None
        self._model_structure = None
        self.structure_revision += 1

    @property
    def last_scan_was_partial(self) -> bool:
        """True if the model list the client is holding may be incomplete.

        Set when a scan stopped early, cleared by the next scan that
        walks the whole chain. A restore from cache leaves it alone:
        the cache is only ever written from a complete scan, so a
        restored layout carries the completeness of the scan it came
        from.
        """
        return self._partial_scan

    def export_model_structure(self) -> dict[str, Any] | None:
        """Serialise the cached layout so the coordinator can persist it.

        Returns ``None`` when there is nothing worth storing. The
        revision travels with the payload: the coordinator writes it
        back on save and compares it on the next cycle, which is how it
        knows a rescan produced something new without diffing lists.
        """
        if not self._model_structure or self._base_addr is None:
            return None
        return {
            "revision": self.structure_revision,
            "base_addr": self._base_addr,
            "models": [list(entry) for entry in self._model_structure],
        }

    def import_model_structure(self, payload: object) -> bool:
        """Adopt a layout persisted by an earlier run. False means "ignored".

        Nothing here trusts the payload. It is validated against the
        live device by :meth:`_validate_model_structure` on the very
        next connect, exactly like a layout this process scanned itself,
        so a stale or hand-edited store file costs three reads and a
        rescan rather than a wrong reading.
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
        """Give ``client`` its model objects, rescanning only when needed.

        pysunspec2 populates ``client.models`` exclusively inside
        ``scan()``, so building a client used to mean walking the
        entire model tree again even though nothing about the device
        had changed, and until v0.22.0 the coordinator rebuilt its
        client on every single cycle to free the inverter's Modbus
        slot. The session is held open now, so what is left to save is
        the client rebuilt after the close that CONF_RELEASE_SLOT (or a
        second config entry behind the same gateway) still does between
        polls, and the first connect after a restart or a reload, where
        the layout comes back from the store. Not the failure path:
        reconnect_next() drops the cache on purpose, because a layout
        read at addresses that just stopped answering is the one thing
        not to reuse. Restoring the cached layout produces the same
        model objects from the three validating reads in
        :meth:`_validate_model_structure` instead of ``1 + 2n``, and
        skips the per-model pacing sleep completely.
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
                # 0 means "no pacing at all"; pysunspec2 only skips the
                # sleep on None, and asyncio.sleep(0) still hands the
                # loop over on every model.
                delay=self._scan_delay or None,
            )
        except (SunSpecModbusClientTimeout, ModbusClientTimeout, ModbusClientConnectionClosed):
            # Never swallowed, unlike the other Modbus errors below.
            #
            # A timeout means a request went out and no answer came
            # back, so the scan is missing a block and nothing below can
            # tell which. Continuing with the models found so far would
            # cache a chain that stops short of the device's real one.
            # Failing the cycle drops the socket and scans again on the
            # next connect. (Since v0.31.0 the transport checks the
            # transaction id, so a late answer can no longer be taken
            # for the answer to the next request; the scan is still
            # incomplete.) A device that hung up mid-scan is the same
            # case with the socket already gone.
            raise
        except (SunSpecModbusClientError, ModbusClientError) as err:
            # Not a timeout, so the device did answer, typically with a
            # Modbus exception code for a block it will not serve. The
            # socket is still in sync, so we have a real choice about
            # how to continue - and a truncated model list is the worst
            # of the options. It drops entities to "unknown" while the
            # cycle still counts as a success, so the stale-data
            # tolerance never gets its chance to hold the last good
            # value, and the user sees a gap that looks like a
            # connection drop but is not one (#42).
            #
            # Prefer the last layout we know was good, if it still
            # validates against the device.
            if await self._async_restore_model_structure(client):
                self._log.warning(
                    "SunSpec scan stopped early (%s: %s). Continuing with the last known "
                    "good layout of %d model(s), which still validates against the device.",
                    err.__class__.__name__,
                    err,
                    len(self._model_structure or []),
                )
                return
            # No usable cache, so this is a first scan. pysunspec2 walks
            # the model chain by reading a length and then the next id,
            # and calls add_model() as it goes. A read that fails
            # partway therefore throws away every model it had already
            # found, which turns one unreadable block into "this
            # inverter is not SunSpec". Reported against the upstream
            # integration for an SMA STP110-60 in cjne/ha-sunspec#375,
            # with this fix; the bug is the same here.
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
            # Deliberately no _capture_model_structure() here. Caching a
            # layout we know is truncated would pin the missing models
            # out of existence indefinitely, and it would validate
            # cleanly while doing so: a truncated chain still has a
            # well-formed tail. An empty cache means the next connect
            # scans again and picks the rest up as soon as the device
            # answers.
            return
        self._partial_scan = False
        self._capture_model_structure(client)

    def _capture_model_structure(self, client: SunSpecClient) -> None:
        """Remember the layout a successful scan just discovered.

        Reads back out of ``client.models`` rather than hooking into
        scan(), and deliberately not out of ``client.model_list``:
        add_model() only appends to that list for models it could
        resolve a group name for, so a vendor block with no bundled
        model definition would silently vanish from the cache and stop
        being polled after the first reconnect.
        """
        structure: list[tuple[int, int, int]] = []
        try:
            for key, instances in client.models.items():
                if not isinstance(key, int):
                    continue
                for model in instances:
                    structure.append((model.model_id, model.model_addr, model.model_len))
        except (AttributeError, TypeError) as err:
            # The cache is an optimisation and must never be able to
            # break a poll. Anything unexpected in the client's model
            # map means "no cache", not "no data".
            self._log.debug("Could not capture the model structure: %s", err)
            self._invalidate_model_structure()
            return
        if not structure:
            self._invalidate_model_structure()
            return
        # Address order is the order the device lays the blocks out,
        # which is the order scan() would rediscover them in.
        structure.sort(key=lambda entry: entry[1])
        if structure == self._model_structure and client.base_addr == self._base_addr:
            # A rescan that found exactly what we already had. Leave the
            # revision alone so the coordinator does not rewrite its
            # store file for nothing.
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

        Three short reads, and none of them is optional:

        * the base address still answers with the ``SunS`` marker and
          the first model id,
        * the last block still carries the id and length we recorded
          for it,
        * the register right behind that block is still the end marker.

        Checking only the first model, which is what this did before,
        checks nothing at all: the first model is 1 (Common) on every
        SunSpec device ever built, so the test passed for any layout.
        The chain is a linked list built out of lengths, so anything
        that changes in the middle moves the tail. Verifying the tail
        therefore catches both cases that matter: a firmware update
        that reorders the tree, and a previous scan that misread a
        length and cached a shifted layout. Reading a shifted layout at
        the old offsets returns values that are wrong rather than
        absent, which is far harder to notice than a failed poll.

        The end-marker read is the one that catches a firmware update
        which only appends models: the tail block is then still where
        it was, but the chain no longer ends behind it. A device that
        refuses that read is not held against it, since pysunspec2's
        own scan tolerates a chain that just stops answering.
        """
        try:
            header = await client.async_read(self._base_addr, 3)
        except Exception as err:  # noqa: BLE001 - any failure just means "scan"
            self._log.debug("Cached model structure could not be validated: %s", err)
            return False
        if not header or len(header) < 6 or header[:4] != b"SunS":
            self._log.debug("Cached base address no longer answers with the SunSpec marker")
            return False
        if mb.data_to_u16(header[4:6]) != structure[0][0]:
            self._log.info("SunSpec model tree changed at the base address, rescanning")
            return False

        last_id, last_addr, last_len = structure[-1]
        try:
            tail = await client.async_read(last_addr, 2)
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
        try:
            end = await client.async_read(last_addr + 2 + last_len, 1)
        except Exception as err:  # noqa: BLE001 - device need not answer past the chain
            self._log.debug("No readable end marker behind the cached chain (%s), accepting", err)
            return True
        if end and len(end) >= 2 and mb.data_to_u16(end[0:2]) != mb.SUNS_END_MODEL_ID:
            self._log.info(
                "SunSpec model chain continues past its cached end, rescanning to pick up "
                "the models behind it"
            )
            return False
        return True

    async def _async_restore_model_structure(self, client: SunSpecClient) -> bool:
        """Rebuild ``client.models`` from the cache. False means "scan instead".

        Every failure path returns False rather than raising, so a cache
        that no longer matches the device costs three wasted reads and
        falls back to exactly the behaviour we had before.
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
                # scan() hands the constructor the raw id and length
                # registers it just read; rebuilt from the cache those
                # are the same two words, plus the registers up to the
                # last group count that the constructor would otherwise
                # read on its own, through the sync path an asyncio
                # device cannot serve (see async_model_data).
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
        except Exception as err:  # noqa: BLE001 - a half-built client is worse than none
            self._log.debug("Rebuilding models from cache failed, falling back to scan: %s", err)
            client.delete_models()
            self._invalidate_model_structure()
            return False
        return True

    async def async_close(self, force: bool = False) -> None:
        """Drop the active client's link and the reference to it.

        After ``async_close()`` the next ``async_get_client()`` builds a
        brand new client on the same connection, which connects again on
        its first request.

        ``force`` says the session is already suspect: a failed cycle,
        or a reload that has to get the slot back before the new
        coordinator connects. The embedded TCP client used it to send a
        TCP RST instead of a FIN, on the theory that a single-slot
        inverter releases its slot faster after an abort.
        modbus-connection owns the socket and closes it with a FIN
        either way, so today the flag only says so in the log. Measured
        on a KACO Powador 7.8 TL3, a FIN is what the steady state has
        always used; whether the failure path misses the RST is what
        the pre-release on that inverter is for.
        """
        if self._client is None:
            return
        client = self._client
        self._client = None
        if force:
            self._log.debug("Dropping a suspect Modbus session")
        await self._async_disconnect(client)

    async def async_shutdown(self) -> None:
        """Close the client and the connection for good: the entry is unloading."""
        await self.async_close(force=True)
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            await connection.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            self._log.debug("connection close raised %s, ignoring", exc)

    async def _async_disconnect(self, client: SunSpecClient) -> None:
        """Drop a client's link. Best effort: cleanup must never raise.

        Takes the client explicitly rather than ``self._client``: the
        connect path only adopts its client on success, so on the
        failure path there is a connected client that nothing else
        could reach (#25).
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

    def _get_connection(self) -> ModbusConnection:
        """The connection object, built on first use. Constructing it does no I/O."""
        if self._connection is None:
            self._connection = ModbusConnection(self._params(), timeout=self._timeout)
        return self._connection

    async def modbus_connect(self) -> SunSpecClient:
        """Build a fresh pysunspec2 client on the connection and give it its models.

        Returns the connected client on success or raises one of our
        typed errors. The test fixtures patch this method to hand in a
        file-backed client, which is why it is the seam and not
        ``async_get_client``.
        """
        endpoint = (
            f"{self._serial_port} @ {self._baudrate} {self._parity}"
            if self._transport == TRANSPORT_RTU
            else f"{self._host}:{self._port}"
        )
        self._log.debug(
            "Connecting to %s unit id %s, timeout %ss", endpoint, self._unit_id, self._timeout
        )
        # The model definitions are JSON files next to the fork, read on
        # first use. The scan and the cache restore look them up on the
        # event loop, and an open() there is what Home Assistant's
        # blocking-call detector reports. Reading all of them once, from
        # a thread, leaves nothing to open later.
        await self._hass.async_add_executor_job(preload_model_defs)
        client = SunSpecModbusClientDeviceUnit(self._get_connection(), slave_id=self._unit_id)
        self._wrap_capturing_read(client)
        # No probe connect in front of the real one. The old check_port
        # opened a TCP session of its own and let the client connect
        # 100 ms behind it; on an inverter that grants one Modbus
        # session that is two connects for one slot, and the second
        # loses: 8 of 12 cycles failed on a KACO Powador 7.8 TL3 with
        # the probe in front, 0 of 12 without it.
        #
        # Drop the client's link on every failure path. Without this a
        # connect that SUCCEEDS and a scan that then fails - the shape
        # reported in #25, where the socket survives long enough to be
        # reset during the base-address walk - leaves a connected
        # client behind that nothing can reach: async_get_client() only
        # adopts the client on success. On a single-slot inverter that
        # makes the failure sticky, because the next attempt genuinely
        # finds the slot occupied, and the second error message
        # describes our own leak rather than the original fault.
        handed_off = False
        try:
            # Eager rather than on the first request, so a refused
            # connect fails here with a transport error and not inside
            # the scan as a protocol one.
            await client.async_connect()
            if not client.is_connected():
                raise TransportError(f"Failed to connect to {endpoint} unit id {self._unit_id}")
            self._log.debug("Client connected, perform initial scan")
            await self._async_scan_or_restore(client)
            handed_off = True
            return client
        except ModbusClientError as err:
            raise TransportError(
                f"Modbus error while connecting to {endpoint} unit id {self._unit_id}: {err}"
            ) from err
        except SunSpecModbusClientError as err:
            # Raised by the scan when no SunSpec base address is found,
            # when the device responds without the SunSpec marker, or
            # on read timeouts during the scan. Without this catch the
            # original message ("Unknown error", "data time out", etc.)
            # is hidden behind a generic "Unexpected error" further up
            # the stack and the user has nothing actionable to report.
            raise ProtocolError(
                f"SunSpec scan failed for {endpoint} unit id {self._unit_id}: {err}"
            ) from err
        finally:
            if not handed_off:
                await self._async_disconnect(client)

    def _wrap_capturing_read(self, client: SunSpecClient) -> None:
        """Wrap ``client.async_read`` so every byte landing on the wire is captured.

        The diagnostics dump surfaces ``self._captured_reads`` so
        users can post a reproducible fixture in bug reports. Capped
        at 1000 entries to bound JSON size. No-op when capture is
        disabled.
        """
        if not self._capture_enabled:
            return
        original_read = client.async_read

        # Signature must stay compatible with the method it replaces.
        # The unit device's async_read takes a third positional ``op``
        # argument selecting the Modbus function code, and pysunspec2
        # passes it positionally from a few internal call sites. A
        # two-parameter wrapper raises TypeError the moment one of those
        # runs, but only while capture is enabled, which is exactly when
        # a user is already trying to debug something.
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
            # The device hung up mid-cycle. Some do that after every
            # request or after a short idle time (APsystems ECU-R,
            # cjne/ha-sunspec#170), and until now that cost the whole
            # cycle: the read failed, the coordinator tore the session
            # down, and only the next cycle connected again, so every
            # second poll on such a device was a failure. The link is
            # already gone, so dropping the client is all there is to
            # do; the cached layout survives it and the rebuild costs
            # three validating reads, not a scan. One attempt: a device
            # that hangs up on the fresh session too is not answering.
            self._log.info("Device closed the Modbus connection, connecting again")
            await self.async_close()
            return await self._async_read_model_once(model_id)

    async def _async_read_model_once(self, model_id: int) -> SunSpecModelWrapper:
        client = await self.async_get_client()
        models = client.models[model_id]
        for model in models:
            if MODEL_READ_PACING_SECONDS:
                await asyncio.sleep(MODEL_READ_PACING_SECONDS)
            await model.async_read()

        return SunSpecModelWrapper(models)
