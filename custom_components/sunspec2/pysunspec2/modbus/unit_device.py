# Added for ha-sunspec2: a SunSpec device over a modbus-connection unit. Origin and license in __init__.py.
"""A SunSpec Modbus client device that talks through a ``modbus_connection`` unit.

Upstream's device classes owned a socket or a serial port and read it
from whatever thread called them. This one owns nothing: it is handed
a ``ModbusConnection`` and asks its ``ModbusUnit`` for registers,
awaiting every request on the event loop. The SunSpec model layer
reaches it through ``async_read`` and ``async_write`` on the groups and
points in ``client.py``; the sync ``read`` and ``write`` raise, because
there is no thread to block.

Errors come back as the fork's own classes, so everything above this
module keeps its exception handling: a Modbus exception response is a
``ModbusClientException`` with the same "Modbus exception N" text the
old TCP client produced, a timeout a ``ModbusClientTimeout``, a dropped
link a ``ModbusClientConnectionClosed``, and anything else the library
raises a ``ModbusClientError``.
"""

import struct

from modbus_connection import ModbusConnectionError
from modbus_connection import ModbusError
from modbus_connection import ModbusExceptionError
from modbus_connection import ModbusTimeoutError

from . import client
from . import modbus as modbus_client
from .modbus import ModbusClientConnectionClosed
from .modbus import ModbusClientError
from .modbus import ModbusClientException
from .modbus import ModbusClientTimeout


def translate_error(err):
    """The fork's exception for a ``modbus_connection`` one."""
    if isinstance(err, ModbusTimeoutError):
        return ModbusClientTimeout(str(err))
    if isinstance(err, ModbusExceptionError):
        code = getattr(err, 'code', None)
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        if code is not None:
            return ModbusClientException('Modbus exception %s' % code)
        return ModbusClientException(str(err))
    if isinstance(err, ModbusConnectionError):
        return ModbusClientConnectionClosed(str(err))
    return ModbusClientError(str(err))


class SunSpecModbusClientDeviceUnit(client.SunSpecModbusClientDevice):
    """Provides access to a SunSpec device through a ``modbus_connection`` connection.

    Parameters:

        connection :
            A ``modbus_connection`` ``ModbusConnection``. Constructing
            one does no I/O; the first request connects on demand.

        slave_id :
            Modbus unit id of the device.

        max_count :
            Most registers asked for in one read request.

        max_write_count :
            Most registers sent in one write request.

        single_register_write :
            Write one register with function code 6 instead of 16.
    """

    def __init__(self, connection, slave_id=1, max_count=modbus_client.REQ_COUNT_MAX,
                 max_write_count=modbus_client.REQ_WRITE_COUNT_MAX, single_register_write=False,
                 model_class=client.SunSpecModbusClientModel):
        client.SunSpecModbusClientDevice.__init__(self, model_class=model_class)
        self.connection = connection
        self.slave_id = slave_id
        self.max_count = max_count
        self.max_write_count = max_write_count
        self.single_register_write = single_register_write
        self.unit = connection.for_unit(slave_id)

    def connect(self):
        raise ModbusClientError('SunSpecModbusClientDeviceUnit connects asynchronously, use async_connect')

    def disconnect(self):
        raise ModbusClientError('SunSpecModbusClientDeviceUnit disconnects asynchronously, use async_disconnect')

    def is_connected(self):
        return bool(self.connection.connected)

    def read(self, addr, count, op=modbus_client.FUNC_READ_HOLDING):
        raise ModbusClientError('SunSpecModbusClientDeviceUnit reads asynchronously, use async_read')

    def write(self, addr, data):
        raise ModbusClientError('SunSpecModbusClientDeviceUnit writes asynchronously, use async_write')

    async def async_connect(self):
        try:
            await self.connection.connect()
        except ModbusError as e:
            raise translate_error(e) from e

    async def async_disconnect(self):
        """Drop the link. The next request connects again."""
        await self.connection.disconnect()

    async def async_close(self):
        """Close the connection for good."""
        await self.connection.close()

    async def async_read(self, addr, count, op=modbus_client.FUNC_READ_HOLDING):
        return await self._async_read_with(self.unit, addr, count, op)

    async def async_write(self, addr, data):
        await self._async_write_with(self.unit, addr, data)

    async def async_read_unit(self, unit_id, addr, count):
        """Read from another unit id on the same connection."""
        return await self._async_read_with(self.connection.for_unit(unit_id), addr, count,
                                           modbus_client.FUNC_READ_HOLDING)

    async def async_write_unit(self, unit_id, addr, data):
        """Write to another unit id on the same connection."""
        await self._async_write_with(self.connection.for_unit(unit_id), addr, data)

    async def _async_read_with(self, unit, addr, count, op):
        data = bytearray()
        address = addr
        remaining = count
        try:
            while remaining > 0:
                chunk = min(remaining, self.max_count)
                if op == modbus_client.FUNC_READ_INPUT:
                    words = await unit.read_input_registers(address, chunk)
                else:
                    words = await unit.read_holding_registers(address, chunk)
                data.extend(struct.pack('>%dH' % len(words), *words))
                address += chunk
                remaining -= chunk
        except ModbusError as e:
            raise translate_error(e) from e
        return bytes(data)

    async def _async_write_with(self, unit, addr, data):
        count = len(data) // 2
        words = list(struct.unpack('>%dH' % count, data[:count * 2]))
        try:
            for start in range(0, len(words), self.max_write_count):
                chunk = words[start:start + self.max_write_count]
                if len(chunk) == 1 and self.single_register_write:
                    await unit.write_register(addr + start, chunk[0])
                else:
                    await unit.write_registers(addr + start, chunk)
        except ModbusError as e:
            raise translate_error(e) from e
