"""Sample API Client."""

import logging
import socket
import threading
import time
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
import sunspec2.modbus.client as modbus_client
from sunspec2.modbus.client import SunSpecModbusClientError
from sunspec2.modbus.client import SunSpecModbusClientException
from sunspec2.modbus.client import SunSpecModbusClientTimeout
from sunspec2.modbus.modbus import ModbusClientError

from .logger import SunSpecLoggerAdapter
from .logger import get_adapter

TIMEOUT = 120

_LOGGER: logging.Logger = logging.getLogger(__package__)


class ConnectionTimeoutError(Exception):
    pass


class ConnectionError(Exception):
    pass


class SunSpecModelWrapper:
    def __init__(self, models) -> None:
        """Sunspec model wrapper"""
        self._models = models
        self.num_models = len(models)

    def isValidPoint(self, point_name):
        point = self.getPoint(point_name)
        if point.value is None:
            return False
        if point.pdef["type"] in ("enum16", "bitfield32"):
            return True
        if point.pdef.get("units", None) is None:
            return False
        return True

    def getKeys(self):
        keys = list(filter(self.isValidPoint, self._models[0].points.keys()))
        for group_name in self._models[0].groups:
            model_group = self._models[0].groups[group_name]
            if type(model_group) is list:
                for idx, group in enumerate(model_group):
                    key_prefix = f"{group_name}:{idx}"
                    group_keys = map(
                        lambda gp: f"{key_prefix}:{gp}", group.points.keys()
                    )
                    keys.extend(filter(self.isValidPoint, group_keys))
            else:
                key_prefix = f"{group_name}:0"
                group_keys = map(
                    lambda gp: f"{key_prefix}:{gp}", model_group.points.keys()
                )
                keys.extend(filter(self.isValidPoint, group_keys))
        return keys

    def getValue(self, point_name, model_index=0):
        point = self.getPoint(point_name, model_index)
        return point.cvalue

    def getMeta(self, point_name):
        return self.getPoint(point_name).pdef

    def getGroupMeta(self):
        return self._models[0].gdef

    def getPoint(self, point_name, model_index=0):
        point_path = point_name.split(":")
        if len(point_path) == 1:
            return self._models[model_index].points[point_name]

        group = self._models[model_index].groups[point_path[0]]
        if type(group) is list:
            return group[int(point_path[1])].points[point_path[2]]
        else:
            if len(point_path) > 2:
                return group.points[
                    point_path[2]
                ]  # Access to the specific point within the group
            return group.points[
                point_name
            ]  # Generic access if no specific subgrouping is specified


# pragma: not covered
def progress(msg):
    _LOGGER.debug(msg)
    return True


class SunSpecApiClient:
    CLIENT_CACHE = {}

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
        self._client_key = f"{host}:{port}:{unit_id}"
        self._lock = threading.Lock()
        self._reconnect = False
        self._log = get_adapter(host, port, unit_id)
        self._capture_enabled = capture_enabled
        self._captured_reads: list[dict] = []
        self._log.debug("New SunspecApi Client (capture=%s)", capture_enabled)

    def get_client(self, config=None):
        cached = SunSpecApiClient.CLIENT_CACHE.get(self._client_key, None)
        if cached is None or config is not None:
            self._log.debug("Not using cached connection")
            cached = self.modbus_connect(config)
            SunSpecApiClient.CLIENT_CACHE[self._client_key] = cached
        if self._reconnect:
            if self.check_port():
                cached.connect()
                self._reconnect = False
        return cached

    def async_get_client(self, config=None):
        return self._hass.async_add_executor_job(self.get_client, config)

    async def async_get_data(self, model_id) -> SunSpecModelWrapper:
        with_model = SunSpecLoggerAdapter(
            self._log.logger, {**self._log.extra, "model_id": model_id}
        )
        try:
            with_model.debug("Get data")
            return await self.read(model_id)
        except SunSpecModbusClientTimeout as timeout_error:
            with_model.warning("Async get data timeout")
            raise ConnectionTimeoutError() from timeout_error
        except SunSpecModbusClientException as connect_error:
            with_model.warning("Async get data connect_error")
            raise ConnectionError() from connect_error

    async def read(self, model_id) -> SunSpecModelWrapper:
        return await self._hass.async_add_executor_job(self.read_model, model_id)

    async def async_get_device_info(self) -> SunSpecModelWrapper:
        return await self.read(1)

    async def async_get_models(self, config=None) -> list:
        self._log.debug("Fetching models")
        client = await self.async_get_client(config)
        model_ids = sorted(list(filter(lambda m: type(m) is int, client.models.keys())))
        return model_ids

    def reconnect_next(self):
        self._reconnect = True

    def close(self):
        """Close the underlying TCP socket on the cached client.

        Important: pysunspec2's SunSpecModbusClientDevice.close() is a
        no-op stub - the real socket teardown lives on disconnect().
        SunSpecModbusClientDeviceTCP does not override close() either, so
        calling client.close() did literally nothing for years. The TCP
        connection stayed open across update cycles, and across config
        entry reloads, until the Python process exited.

        That latent behaviour did not bite cjne/ha-sunspec because nothing
        in that integration triggered a runtime reload. Phase 2's
        capture_raw_registers options-flow toggle does, and the leftover
        socket then competed with the new client for the inverter's
        single Modbus TCP slot - producing the "sensors go unavailable
        right after toggling capture" symptom.

        We therefore call client.disconnect() (which is real) and use the
        cached lookup directly instead of going through get_client(),
        because get_client() would silently rebuild the client if the
        cache was already invalidated, defeating the purpose of close().
        """
        cached = SunSpecApiClient.CLIENT_CACHE.get(self._client_key)
        if cached is None:
            return
        try:
            cached.disconnect()
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
                self._log.debug(
                    "Check_Port (ERROR): port not available - error: %s", sock_res
                )
            sock.close()
            time.sleep(0.1)
        return is_open

    def modbus_connect(self, config=None):
        use_config = SimpleNamespace(
            **(
                config
                or {"host": self._host, "port": self._port, "unit_id": self._unit_id}
            )
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
                    raise ConnectionError(
                        f"Failed to connect to {self._host}:{self._port} unit id {self._unit_id}"
                    )
                self._log.debug("Client connected, perform initial scan")
                client.scan(
                    connect=False, progress=progress, full_model_read=False, delay=0.5
                )
                return client
            except ModbusClientError as err:
                raise ConnectionError(
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
                raise ConnectionError(
                    f"SunSpec scan failed for "
                    f"{use_config.host}:{use_config.port} unit id "
                    f"{use_config.unit_id}: {err}"
                ) from err
        else:
            self._log.debug("Inverter not ready for Modbus TCP connection")
            raise ConnectionError(f"Inverter not active on {self._host}:{self._port}")

    def read_model(self, model_id) -> dict:
        client = self.get_client()
        models = client.models[model_id]
        for model in models:
            time.sleep(0.6)
            model.read()

        return SunSpecModelWrapper(models)
