"""Sample API Client."""

import logging
import socket
import struct
import threading
import time
from types import SimpleNamespace

import sunspec2.mb as mb
import sunspec2.modbus.client as modbus_client
from homeassistant.core import HomeAssistant
from sunspec2.device import ModelError
from sunspec2.modbus.client import SunSpecModbusClientError
from sunspec2.modbus.client import SunSpecModbusClientException
from sunspec2.modbus.client import SunSpecModbusClientTimeout
from sunspec2.modbus.client import SunSpecModbusValueError
from sunspec2.modbus.modbus import ModbusClientError
from sunspec2.modbus.modbus import ModbusClientException
from sunspec2.modbus.modbus import ModbusClientTimeout

from .const import DEFAULT_BAUDRATE
from .const import DEFAULT_SCAN_DELAY_SECONDS
from .const import MAX_SCAN_DELAY_SECONDS
from .const import MIN_SCAN_DELAY_SECONDS
from .const import MODEL_STRUCTURE_TTL_SECONDS
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

# Modbus TCP socket timeout (seconds). Used by pysunspec2 for both the
# initial TCP connect and every subsequent register read on this client.
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

# Initial-setup socket timeout (seconds). The very first
# ``client.scan()`` after a fresh connect walks every SunSpec model
# block on the device, which can be 16+ models deep on a fully featured
# inverter and is much slower than a single read in steady state. The
# steady-state TIMEOUT of 10s is too tight for that walk on slower
# devices (notably KACO Powador on 100 Mbit), so the config-flow probe
# and the diagnostics probe pass this longer timeout to the API client.
SETUP_TIMEOUT = 60

_LOGGER: logging.Logger = logging.getLogger(__package__)


# pragma: not covered
def progress(msg):
    _LOGGER.debug(msg)
    return True


def _discovered_model_count(client) -> int:
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
        # Seconds pysunspec2 sleeps between models during scan(). The
        # coordinator rescans on every cycle (it closes the client to
        # free the inverter's Modbus slot), so this is paid once per
        # model per poll, not once per setup. See CONF_SCAN_DELAY.
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
        self._lock = threading.Lock()
        self._reconnect = False
        self._client = None
        # Cached SunSpec layout: base address plus (model_id, addr, len)
        # per block. Survives close(), which runs at the end of every
        # cycle, and is dropped by reconnect_next(), which only runs
        # after a failure: the one situation where the layout is a
        # suspect rather than an asset. See MODEL_STRUCTURE_TTL_SECONDS.
        self._base_addr: int | None = None
        self._model_structure: list[tuple[int, int, int]] | None = None
        self._structure_scanned_at: float | None = None
        self._log = get_adapter(host, port, unit_id)
        self._capture_enabled = capture_enabled
        self._captured_reads: list[dict] = []
        self._log.debug(
            "New SunspecApi Client (transport=%s, capture=%s, timeout=%ds, scan_delay=%ss)",
            transport,
            capture_enabled,
            timeout,
            self._scan_delay,
        )

    def get_client(self, config=None):
        """Return the active pysunspec2 client, building it on first use.

        On the explicit reconnect path (``reconnect_next()`` set
        ``_reconnect=True`` after the previous cycle failed) we
        force-disconnect the old client via :meth:`_force_disconnect`,
        which sends a TCP RST so single-slot inverters free their slot
        immediately instead of waiting on their own keep-alive timeout.
        Within a single update cycle the client is reused across the
        16+ ``read_model`` calls - hence the conditional, not an
        unconditional rebuild on every entry.

        The legacy ``config`` parameter is ignored - it predates Phase 4
        and was used by the options flow to probe a different host. The
        new probe path is :meth:`known_models`, which never forces a
        connect. The argument is kept only because async_get_models still
        passes it through; it can be removed in a later phase.
        """
        if self._client is not None and self._reconnect:
            self._force_disconnect()
            self._client = None
            self._reconnect = False
        if self._client is None:
            self._client = self.modbus_connect()
        return self._client

    def async_get_client(self, config=None):
        return self._hass.async_add_executor_job(self.get_client, config)

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
            self._log.logger, {**self._log.extra, "model_id": model_id}
        )
        try:
            with_model.debug("Get data")
            return await self.read(model_id)
        except SunSpecModbusClientTimeout as exc:
            with_model.warning("Modbus read timeout")
            raise TransientError(f"Modbus read timeout for model {model_id}") from exc
        except SunSpecModbusClientException as exc:
            with_model.warning("Modbus exception while reading model")
            raise DeviceError(f"Modbus exception while reading model {model_id}: {exc}") from exc

    async def read(self, model_id: int) -> SunSpecModelWrapper:
        return await self._hass.async_add_executor_job(self.read_model, model_id)

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

        - ``sunspec2.device.ModelError`` is a plain ``Exception``. It is
          what an unread scale factor or an unimplemented ``*_SF``
          register produces on the ``cvalue`` assignment.
        - ``SunSpecModbusValueError`` is a plain ``Exception`` too,
          despite the name suggesting it sits under the client tree.
        - ``ModbusClientError`` and its subclasses come from the
          transport layer below the SunSpec client and are the ones
          raised when the device NAKs the write with a Modbus
          exception code or the socket write fails.
          ``SunSpecModbusClientDeviceTCP.write`` does not translate
          them into the SunSpec hierarchy.

        Anything left uncaught here reaches the Number / Switch entity
        as a raw traceback in the UI, because their ``except
        SunSpecError`` never matches. Subclasses must be listed before
        their bases: ``ModbusClientTimeout`` and
        ``SunSpecModbusClientTimeout`` are transient, their respective
        base classes are not.
        """
        with_model = SunSpecLoggerAdapter(
            self._log.logger, {**self._log.extra, "model_id": model_id}
        )
        with_model.debug("Write %s", ", ".join(f"{n} = {v!r}" for n, v in points))
        point_name = ", ".join(name for name, _ in points)
        try:
            await self._hass.async_add_executor_job(self._write_points_blocking, model_id, points)
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

    def _write_points_blocking(self, model_id: int, points: list[tuple[str, object]]) -> None:
        client = self.get_client()
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
        # Why the point is empty: the coordinator calls api.close() at the
        # end of each update cycle, so get_client() above builds a brand
        # new client and rescans with full_model_read=False. pysunspec2
        # only calls model.read() during scan when that flag is set, so
        # every point except ID and L is still None here - including the
        # scale factor that Point.set_value(computed=True) needs to encode
        # the value. The model objects the coordinator read into are a
        # different, already discarded client.
        #
        # The switches never hit this because Conn, WMaxLim_Ena and
        # OutPFSet_Ena are enum16 with sf: null, so sf_required is False.
        # That is why #17 reported working switches and broken numbers.
        #
        # Cost is one block read of len + 2 registers, which is noise next
        # to the reconnect and full scan this call already paid for. Read
        # before setting cvalue, never after: Group.read() ends in
        # set_mb(dirty=False) and would discard a pending value.
        model.read()
        for point, value in resolved:
            point.cvalue = value
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
        model.write()

    async def async_get_device_info(self) -> SunSpecModelWrapper:
        return await self.read(1)

    async def async_get_models(self, config: dict | None = None) -> list[int]:
        self._log.debug("Fetching models")
        client = await self.async_get_client(config)
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
        self._base_addr = None
        self._model_structure = None
        self._structure_scanned_at = None

    def _model_structure_is_fresh(self) -> bool:
        if self._model_structure is None or self._structure_scanned_at is None:
            return False
        return (time.monotonic() - self._structure_scanned_at) < MODEL_STRUCTURE_TTL_SECONDS

    def _scan_or_restore(self, client) -> None:
        """Give ``client`` its model objects, rescanning only when needed.

        pysunspec2 populates ``client.models`` exclusively inside
        ``scan()``, so a reconnect used to mean walking the entire model
        tree again even though nothing about the device had changed, and
        the coordinator reconnects on every single cycle to free the
        inverter's Modbus slot. Restoring the cached layout produces the
        same model objects from one read instead of ``1 + 2n``, and
        skips the per-model pacing sleep completely.
        """
        if self._model_structure_is_fresh() and self._restore_model_structure(client):
            self._log.debug(
                "Restored %d cached SunSpec models, skipping scan",
                len(self._model_structure or []),
            )
            return
        self._log.debug("Scanning SunSpec model tree")
        try:
            client.scan(
                connect=False,
                progress=progress,
                full_model_read=False,
                # 0 means "no pacing at all"; pysunspec2 only skips the
                # sleep on None, and time.sleep(0) still yields the GIL
                # inside the executor thread on every model.
                delay=self._scan_delay or None,
            )
        except (SunSpecModbusClientError, ModbusClientError) as err:
            # pysunspec2 walks the model chain by reading a length and
            # then the next id, and calls add_model() as it goes. A read
            # that fails partway therefore throws away every model it
            # had already found, which turns one unreadable block into
            # "this inverter is not SunSpec". Reported against the
            # upstream integration for an SMA STP110-60 in
            # cjne/ha-sunspec#375, with this fix; the bug is the same
            # here.
            if not _discovered_model_count(client):
                raise
            self._log.warning(
                "SunSpec scan stopped early (%s: %s). Continuing with the %d model(s) "
                "found before the failure; the rest of the chain was not read.",
                err.__class__.__name__,
                err,
                _discovered_model_count(client),
            )
            # Deliberately no _capture_model_structure() here. Caching a
            # layout we know is truncated would pin the missing models
            # out of existence for MODEL_STRUCTURE_TTL_SECONDS; leaving
            # the cache empty means the next cycle scans again and can
            # pick them up as soon as the device answers.
            return
        self._capture_model_structure(client)

    def _capture_model_structure(self, client) -> None:
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
        self._base_addr = client.base_addr
        self._model_structure = structure
        self._structure_scanned_at = time.monotonic()

    def _restore_model_structure(self, client) -> bool:
        """Rebuild ``client.models`` from the cache. False means "scan instead".

        Every failure path returns False rather than raising, so a cache
        that no longer matches the device costs one wasted read and
        falls back to exactly the behaviour we had before.

        The validating read is not optional. Without it a firmware
        update that reorders the model tree would be read at the old
        offsets and produce values that are wrong rather than absent,
        which is far harder to notice than a failed poll.
        """
        structure = self._model_structure
        if not structure or self._base_addr is None:
            return False
        try:
            header = client.read(self._base_addr, 3)
        except Exception as err:  # noqa: BLE001 - any failure just means "scan"
            self._log.debug("Cached model structure could not be validated: %s", err)
            return False
        if not header or len(header) < 6 or header[:4] != b"SunS":
            self._log.debug("Cached base address no longer answers with the SunSpec marker")
            return False
        if mb.data_to_u16(header[4:6]) != structure[0][0]:
            self._log.info("SunSpec model tree changed at the base address, rescanning")
            return False

        client.base_addr = self._base_addr
        client.delete_models()
        try:
            for index, (model_id, model_addr, model_len) in enumerate(structure):
                model = client.model_class(
                    model_id=model_id,
                    model_addr=model_addr,
                    model_len=model_len,
                    # scan() hands the constructor the raw id and length
                    # registers it just read; rebuilt from the cache
                    # those are the same two words.
                    data=mb.u16_to_data(model_id) + mb.u16_to_data(model_len),
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

    def close(self) -> None:
        """Tear down the active client's TCP socket and drop the reference.

        After ``close()`` the next ``get_client()`` will build a brand new
        client. This is the lifecycle hook ``async_unload_entry`` calls so
        the inverter's single Modbus TCP slot is freed before the new
        coordinator (built in the subsequent ``async_setup_entry``) tries
        to connect.

        Uses :meth:`_force_disconnect` so the underlying socket goes out
        with an RST instead of a polite FIN, which makes the inverter
        free its single Modbus TCP slot immediately instead of waiting
        on its own TCP keepalive / connection timeout.
        """
        if self._client is None:
            return
        self._force_disconnect()
        self._client = None

    def _force_disconnect(self, client=None) -> None:
        """Tear down a client as aggressively as possible.

        Defaults to ``self._client``. Callers holding a client that is
        not (yet) ``self._client`` pass it explicitly: the connect paths
        only adopt their client on success, so on the failure path there
        is a fully connected socket that neither this method nor
        :meth:`close` could otherwise reach.

        For TCP: sets SO_LINGER=(1, 0) on the underlying socket so the
        kernel sends a TCP RST instead of a polite FIN. This makes
        single-slot inverters (KACO Powador et al) free their slot
        immediately instead of waiting on their own keepalive / 30s+
        idle timeout, which would otherwise race the next reconnect
        after a flaky-network blip.

        For RTU: there is no socket and no FIN/RST distinction. We
        just call ``client.close()`` which is pysunspec2's RTU-side
        teardown method, equivalent in spirit to TCP's
        ``disconnect()``.

        Best-effort in both modes: any failure walking pysunspec2's
        internals is swallowed. Cleanup must never raise from here.
        """
        if client is None:
            client = self._client
        if client is None:
            return

        if self._transport == TRANSPORT_RTU:
            # RTU lifecycle: client.close() releases the serial port.
            # No socket-level tricks apply.
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                self._log.debug("client.close raised %s, ignoring", exc)
            return

        # TCP path. pysunspec2 layout:
        # SunSpecModbusClientDeviceTCP.client is a ModbusClientTCP
        # whose .socket attribute is the raw Python socket. Both
        # attributes can legitimately be missing on a half-built or
        # already-closed client, hence the careful getattr chain.
        raw_sock = None
        try:
            raw_sock = getattr(getattr(client, "client", None), "socket", None)
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            self._log.debug("could not reach raw socket: %s, ignoring", exc)

        if raw_sock is not None:
            try:
                # struct linger { int l_onoff; int l_linger; }
                # l_onoff=1, l_linger=0 => RST instead of FIN on close
                raw_sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, 0),
                )
                self._log.debug("SO_LINGER=0 set, will RST on close")
            except OSError as exc:
                self._log.debug("setsockopt SO_LINGER failed: %s, ignoring", exc)

        try:
            client.disconnect()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            self._log.debug("client.disconnect raised %s, ignoring", exc)

    def check_port(self) -> bool:
        """Check if port is available.

        Note this opens and closes a full TCP session of its own,
        roughly 100 ms before the real client connects. On inverters
        that allow exactly one Modbus TCP slot that is a second
        connection to the same endpoint in quick succession, which is
        an open suspect in #25. Removing it needs a reporter to confirm
        the double-connect hypothesis first, so for now it stays and
        only its process-global side effect is gone.
        """
        with self._lock:
            sock_timeout = float(3)
            self._log.debug("Check_Port: opening socket with %ss timeout", sock_timeout)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Per-socket, not socket.setdefaulttimeout(): that mutates
            # process-global state, so our 3s leaked into every other
            # integration in the same Home Assistant process that
            # created a socket without setting its own timeout.
            sock.settimeout(sock_timeout)
            sock_res = sock.connect_ex((self._host, self._port))
            is_open = sock_res == 0  # True if open, False if not
            if is_open:
                sock.shutdown(socket.SHUT_RDWR)
                self._log.debug("Check_Port (SUCCESS): port open")
            else:
                self._log.debug("Check_Port (ERROR): port not available - error: %s", sock_res)
            sock.close()
            time.sleep(0.1)
        return is_open

    def modbus_connect(self, config: dict | None = None):
        """Build a fresh pysunspec2 client and run its initial SunSpec scan.

        Dispatches to TCP or RTU based on ``self._transport``. The
        legacy ``config`` parameter is only honoured by the TCP path
        (it predates the transport split). Returns the connected
        client on success or raises one of our typed errors.
        """
        if self._transport == TRANSPORT_RTU:
            return self._modbus_connect_rtu()
        return self._modbus_connect_tcp(config)

    def _modbus_connect_tcp(self, config: dict | None = None):
        use_config = SimpleNamespace(
            **(config or {"host": self._host, "port": self._port, "unit_id": self._unit_id})
        )
        self._log.debug("TCP client connect using timeout %s", self._timeout)
        client = modbus_client.SunSpecModbusClientDeviceTCP(
            slave_id=use_config.unit_id,
            ipaddr=use_config.host,
            ipport=use_config.port,
            timeout=self._timeout,
        )
        self._wrap_capturing_read(client)
        if not self.check_port():
            self._log.debug("Inverter not ready for Modbus TCP connection")
            raise TransportError(f"Inverter not active on {self._host}:{self._port}")

        self._log.debug("Inverter ready for Modbus TCP connection")
        # Tear the client down on every failure path. Without this a
        # connect that SUCCEEDS and a scan that then fails - which is
        # exactly the shape reported in #25, where the socket survives
        # long enough to be reset during the base-address walk - leaves
        # a fully connected client behind that nothing can reach:
        # get_client() only assigns self._client on success, so both
        # close() and _force_disconnect() see None and do nothing. On a
        # single-slot inverter that makes the failure sticky, because
        # the next attempt genuinely does find the slot occupied, and
        # the second error message describes our own leak rather than
        # the original fault.
        handed_off = False
        try:
            with self._lock:
                client.connect()
            if not client.is_connected():
                raise TransportError(
                    f"Failed to connect to {self._host}:{self._port} unit id {self._unit_id}"
                )
            self._log.debug("Client connected, perform initial scan")
            self._scan_or_restore(client)
            handed_off = True
            return client
        except ModbusClientError as err:
            raise TransportError(
                f"Modbus error while connecting to "
                f"{use_config.host}:{use_config.port} unit id "
                f"{use_config.unit_id}: {err}"
            ) from err
        except SunSpecModbusClientError as err:
            # Raised by client.scan() when no SunSpec base address is
            # found, when the device responds without the SunSpec marker,
            # or on read timeouts during the scan. Without this catch the
            # original message ("Unknown error", "data time out", etc.)
            # is hidden behind a generic "Unexpected error" further up
            # the stack and the user has nothing actionable to report.
            raise ProtocolError(
                f"SunSpec scan failed for "
                f"{use_config.host}:{use_config.port} unit id "
                f"{use_config.unit_id}: {err}"
            ) from err
        finally:
            if not handed_off:
                self._force_disconnect(client)

    def _modbus_connect_rtu(self):
        """Build a Modbus RTU client over a serial port (RS-485).

        Lifecycle is different from TCP: pysunspec2's RTU client uses
        ``open()`` / ``close()`` instead of ``connect()`` / ``disconnect()``
        and has no ``is_connected()``. There's also no socket-level
        ``check_port()`` analogue - if the serial port doesn't exist
        the constructor (or open()) raises immediately, which we
        translate into a TransportError.
        """
        if not self._serial_port:
            raise TransportError("Serial port is not configured but transport=rtu was requested")
        self._log.debug(
            "RTU client connect on %s @ %d %s, timeout=%s",
            self._serial_port,
            self._baudrate,
            self._parity,
            self._timeout,
        )
        try:
            with self._lock:
                client = modbus_client.SunSpecModbusClientDeviceRTU(
                    slave_id=self._unit_id,
                    name=self._serial_port,
                    baudrate=self._baudrate,
                    parity=self._parity,
                    timeout=self._timeout,
                )
        except SunSpecModbusClientError as err:
            raise TransportError(
                f"Could not open serial port {self._serial_port} "
                f"({self._baudrate} {self._parity}): {err}"
            ) from err
        except OSError as err:
            raise TransportError(f"Serial port {self._serial_port} not available: {err}") from err
        self._wrap_capturing_read(client)
        # Same leak as the TCP path: open() can succeed and scan() fail,
        # and the serial port would then stay claimed by a client that
        # nothing holds a reference to.
        handed_off = False
        try:
            with self._lock:
                client.open()
            self._log.debug("RTU port opened, perform initial scan")
            self._scan_or_restore(client)
            handed_off = True
            return client
        except ModbusClientError as err:
            raise TransportError(
                f"Modbus error on serial port {self._serial_port} unit id {self._unit_id}: {err}"
            ) from err
        except SunSpecModbusClientError as err:
            raise ProtocolError(
                f"SunSpec scan failed on serial port {self._serial_port} "
                f"unit id {self._unit_id}: {err}"
            ) from err
        finally:
            if not handed_off:
                self._force_disconnect(client)

    def _wrap_capturing_read(self, client) -> None:
        """Wrap ``client.read`` so every byte landing on the wire is captured.

        The diagnostics dump surfaces ``self._captured_reads`` so
        users can post a reproducible fixture in bug reports. Capped
        at 1000 entries to bound JSON size. No-op when capture is
        disabled, called from both the TCP and RTU build paths.
        """
        if not self._capture_enabled:
            return
        original_read = client.read

        # Signature must stay compatible with the method it replaces.
        # SunSpecModbusClientDeviceTCP.read takes a third positional
        # ``op`` argument selecting the Modbus function code, and
        # pysunspec2 passes it positionally from a few internal call
        # sites. A two-parameter wrapper raises TypeError the moment
        # one of those runs, but only while capture is enabled, which
        # is exactly when a user is already trying to debug something.
        def capturing_read(addr, count, *args, **kwargs):
            data = original_read(addr, count, *args, **kwargs)
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

        client.read = capturing_read

    def read_model(self, model_id: int) -> SunSpecModelWrapper:
        client = self.get_client()
        models = client.models[model_id]
        for model in models:
            time.sleep(0.6)
            model.read()

        return SunSpecModelWrapper(models)
