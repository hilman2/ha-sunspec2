"""Sample API Client."""

import logging
import socket
import threading
import time
from types import SimpleNamespace

import sunspec2.modbus.client as modbus_client
from homeassistant.core import HomeAssistant
from sunspec2.modbus.client import SunSpecModbusClientError
from sunspec2.modbus.client import SunSpecModbusClientException
from sunspec2.modbus.client import SunSpecModbusClientTimeout
from sunspec2.modbus.modbus import ModbusClientError

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
# 10s is the new ceiling: an inverter that has not answered after ten
# seconds is gone, full stop. The in-cycle retry in the coordinator
# (5s sleep then one more attempt) and the stale-data tolerance in the
# entity available property cover the actual flaky-network case much
# better than a long socket timeout ever did.
TIMEOUT = 10

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
    ) -> None:
        """Sunspec modbus client."""

        self._host = host
        self._port = port
        self._hass = hass
        self._unit_id = unit_id
        self._lock = threading.Lock()
        self._reconnect = False
        self._client = None
        self._log = get_adapter(host, port, unit_id)
        self._capture_enabled = capture_enabled
        self._captured_reads: list[dict] = []
        self._log.debug("New SunspecApi Client (capture=%s)", capture_enabled)

    def get_client(self, config=None):
        """Return the active pysunspec2 client, building it on first use.

        The legacy ``config`` parameter is ignored - it predates Phase 4
        and was used by the options flow to probe a different host. The
        new probe path is :meth:`known_models`, which never forces a
        connect. The argument is kept only because async_get_models still
        passes it through; it can be removed in a later phase.
        """
        if self._client is not None and self._reconnect:
            try:
                self._client.disconnect()
            except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                self._log.debug("disconnect during reconnect raised %s, ignoring", exc)
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

        Note: ``pysunspec2.modbus.client.SunSpecModbusClientDevice.close()``
        is a ``pass`` stub. The real socket teardown is on
        ``disconnect()``, which we call here.
        """
        if self._client is None:
            return
        try:
            self._client.disconnect()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            self._log.debug("client.disconnect raised %s, ignoring", exc)
        self._client = None

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
        use_config = SimpleNamespace(
            **(config or {"host": self._host, "port": self._port, "unit_id": self._unit_id})
        )
        self._log.debug("Client connect using timeout %s", TIMEOUT)
        client = modbus_client.SunSpecModbusClientDeviceTCP(
            slave_id=use_config.unit_id,
            ipaddr=use_config.host,
            ipport=use_config.port,
            timeout=TIMEOUT,
        )
        if self._capture_enabled:
            # Wrap the device-level read so every modbus read on this client
            # instance lands in self._captured_reads. The diagnostics dump
            # surfaces these so users can post a reproducible fixture in
            # bug reports. Capped at 1000 entries to bound JSON size.
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

    def read_model(self, model_id: int) -> SunSpecModelWrapper:
        client = self.get_client()
        models = client.models[model_id]
        for model in models:
            time.sleep(0.6)
            model.read()

        return SunSpecModelWrapper(models)
