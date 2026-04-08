"""Sample API Client."""

import logging
import socket
import struct
import threading
import time
from types import SimpleNamespace

import sunspec2.modbus.client as modbus_client
from homeassistant.core import HomeAssistant
from sunspec2.modbus.client import SunSpecModbusClientError
from sunspec2.modbus.client import SunSpecModbusClientException
from sunspec2.modbus.client import SunSpecModbusClientTimeout
from sunspec2.modbus.modbus import ModbusClientError

from .const import DEFAULT_BAUDRATE
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
        self._transport = transport
        self._serial_port = serial_port
        self._baudrate = baudrate
        self._parity = parity
        self._lock = threading.Lock()
        self._reconnect = False
        self._client = None
        self._log = get_adapter(host, port, unit_id)
        self._capture_enabled = capture_enabled
        self._captured_reads: list[dict] = []
        self._log.debug(
            "New SunspecApi Client (transport=%s, capture=%s, timeout=%ds)",
            transport,
            capture_enabled,
            timeout,
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

    async def async_get_device_info(self) -> SunSpecModelWrapper:
        return await self.read(1)

    async def async_get_models(self, config: dict | None = None) -> list[int]:
        self._log.debug("Fetching models")
        client = await self.async_get_client(config)
        model_ids = sorted(list(filter(lambda m: type(m) is int, client.models.keys())))
        return model_ids

    def reconnect_next(self) -> None:
        self._reconnect = True

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

    def _force_disconnect(self) -> None:
        """Tear down ``self._client`` as aggressively as possible.

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
        """Check if port is available"""
        with self._lock:
            sock_timeout = float(3)
            self._log.debug("Check_Port: opening socket with %ss timeout", sock_timeout)
            socket.setdefaulttimeout(sock_timeout)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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
        if self.check_port():
            self._log.debug("Inverter ready for Modbus TCP connection")
            try:
                with self._lock:
                    client.connect()
                if not client.is_connected():
                    raise TransportError(
                        f"Failed to connect to {self._host}:{self._port} unit id {self._unit_id}"
                    )
                self._log.debug("Client connected, perform initial scan")
                client.scan(connect=False, progress=progress, full_model_read=False, delay=0.5)
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
        else:
            self._log.debug("Inverter not ready for Modbus TCP connection")
            raise TransportError(f"Inverter not active on {self._host}:{self._port}")

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
        try:
            with self._lock:
                client.open()
            self._log.debug("RTU port opened, perform initial scan")
            client.scan(connect=False, progress=progress, full_model_read=False, delay=0.5)
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

        def capturing_read(addr, count):
            data = original_read(addr, count)
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
