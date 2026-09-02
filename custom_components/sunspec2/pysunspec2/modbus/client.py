# Modified for ha-sunspec2: imports made relative to this package. Origin and license in __init__.py.
"""
    Copyright (C) 2020 SunSpec Alliance

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the "Software"),
    to deal in the Software without restriction, including without limitation
    the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included
    in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
    THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
    FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
    IN THE SOFTWARE.
"""

import asyncio
import uuid
from .. import mdef, device, mb
from . import modbus as modbus_client


class SunSpecModbusClientError(Exception):
    pass


class SunSpecModbusValueError(Exception):
    pass


class SunSpecModbusClientTimeout(SunSpecModbusClientError):
    pass


class SunSpecModbusClientException(SunSpecModbusClientError):
    pass


class SunSpecModbusClientPoint(device.Point):

    # ha-sunspec2: upstream's read and write called the device from the
    # calling thread. These await it, for a device that talks through an
    # asyncio transport (see unit_device.py); the sync pair is gone with
    # the socket and serial clients.

    async def async_read(self):
        data = await self.model.device.async_read(self.model.model_addr + self.offset, self.len)
        self.set_mb(data=data, dirty=False)

    async def async_write(self):
        """Write the point to the physical device"""
        try:
            data = self.info.to_data(self.value, int(self.len) * 2)
        except Exception as e:
            raise SunSpecModbusValueError('Point value error for %s %s: %s' % (self.pdef.get(mdef.NAME), self.value,
                                                                               str(e)))
        addr = self.model.model_addr + self.offset
        await self.model.device.async_write(addr, data)
        self.dirty = False


class SunSpecModbusClientGroup(device.Group):

    def __init__(self, gdef=None, model=None, model_offset=0, group_len=0, data=None, data_offset=0, group_class=None,
                 point_class=None, index=None):

        device.Group.__init__(self, gdef=gdef, model=model, model_offset=model_offset, group_len=group_len,
                              data=data, data_offset=data_offset, group_class=group_class, point_class=point_class,
                              index=index)

    # ha-sunspec2: upstream's read, write and write_points, awaiting the
    # device. The connect-if-not-connected dance around the read is gone
    # with it: the device connects on demand.

    async def async_read(self, len=None):
        if len is None:
            len = self.len
        if self.access_regions:
            data = bytearray()
            for region in self.access_regions:
                data += await self.model.device.async_read(self.model.model_addr + self.offset + region[0],
                                                           region[1])
            data = bytes(data)
        else:
            data = await self.model.device.async_read(self.model.model_addr + self.offset, len)
        self.set_mb(data=data, dirty=False)

    async def async_write(self):
        start_addr = next_addr = self.model.model_addr + self.offset
        data = b''
        start_addr, next_addr, data = await self.async_write_points(start_addr, next_addr, data)
        if data:
            await self.model.device.async_write(start_addr, data)

    async def async_write_points(self, start_addr=None, next_addr=None, data=None):
        """
        Write all points that have been modified since the last write operation to the physical device
        """

        for name, point in self.points.items():
            model_addr = self.model.model_addr
            point_offset = point.offset
            point_addr = model_addr + point_offset
            if data and (not point.dirty or point_addr != next_addr):
                await self.model.device.async_write(start_addr, data)
                data = b''
            if point.dirty:
                point_len = point.len
                try:
                    point_data = point.info.to_data(point.value, int(point_len) * 2)
                except Exception as e:
                    raise SunSpecModbusValueError('Point value error for %s %s: %s' % (name, point.value, str(e)))
                if not data:
                    start_addr = point_addr
                next_addr = point_addr + point_len
                data += point_data
                point.dirty = False

        for name, group in self.groups.items():
            if isinstance(group, list):
                for g in group:
                    start_addr, next_addr, data = await g.async_write_points(start_addr, next_addr, data)
            else:
                start_addr, next_addr, data = await group.async_write_points(start_addr, next_addr, data)

        return start_addr, next_addr, data


class SunSpecModbusClientModel(SunSpecModbusClientGroup):
    def __init__(self, model_id=None, model_addr=0, model_len=0, model_def=None, data=None, mb_device=None,
                 group_class=SunSpecModbusClientGroup, point_class=SunSpecModbusClientPoint):
        self.model_id = model_id
        self.model_addr = model_addr
        self.model_len = model_len
        self.model_def = model_def
        self.error_info = ''
        self.mid = None
        self.device = mb_device
        self.model = self

        gdef = None
        try:
            if self.model_def is None:
                self.model_def = device.get_model_def(model_id)
            if self.model_def is not None:
                gdef = self.model_def.get(mdef.GROUP)
        except Exception as e:
            self.add_error(str(e))

        # determine largest point index that contains a group len
        group_len_points_index = mdef.get_group_len_points_index(gdef)
        # Upstream read the rest of the point data here, from the calling
        # thread. A device over an asyncio transport cannot serve that, so
        # the scan and the cache restore read it first (async_model_data)
        # and a constructor that is still short says so instead.
        data_regs = len(data)/2
        remaining = group_len_points_index - data_regs
        if remaining > 0:
            raise SunSpecModbusClientError('Model %s needs %s more register(s) up to its group counts; '
                                           'read them with async_model_data first' % (model_id, remaining))

        SunSpecModbusClientGroup.__init__(self, gdef=gdef, model=self.model, model_offset=0, group_len=self.model_len,
                                          data=data, data_offset=0, group_class=group_class, point_class=point_class)

        if self.model_len is not None:
            self.len = self.model_len

        if self.model_len and self.len:
            if self.model_len != self.len:
                self.add_error('Model error: Discovered length %s does not match computed length %s' %
                               (self.model_len, self.len))

    def add_error(self, error_info):
        self.error_info = '%s%s\n' % (self.error_info, error_info)

    async def async_read(self, len=None):
        await SunSpecModbusClientGroup.async_read(self, len=self.len + 2)


class SunSpecModbusClientDevice(device.Device):
    def __init__(self, model_class=SunSpecModbusClientModel):
        device.Device.__init__(self, model_class=model_class)
        self.did = str(uuid.uuid4())
        self.retry_count = 2
        self.base_addr_list = [40000, 0, 50000]
        self.base_addr = None

    # ha-sunspec2: the transport interface a device implements, awaited.
    # Upstream's sync connect, read, write and scan went with the socket
    # and serial clients; unit_device.py is the one implementation.

    def is_connected(self):
        return True

    async def async_connect(self):
        pass

    async def async_disconnect(self):
        pass

    async def async_close(self):
        pass

    # must be overridden by Modbus protocol implementation
    async def async_read(self, addr, count):
        return b''

    # must be overridden by Modbus protocol implementation
    async def async_write(self, addr, data):
        return

    async def async_model_data(self, model_id, addr, data):
        """Extend the id and length registers in ``data`` so the model
        constructor has no register left to read.

        SunSpecModbusClientModel reads the registers up to the last
        group-count point itself when it is handed too few, through the
        sync ``read``, which a device over an asyncio transport cannot
        serve. Reading them here first keeps the constructor off the wire.
        """
        try:
            model_def = device.get_model_def(model_id)
        except Exception:
            return data
        gdef = model_def.get(mdef.GROUP) if model_def else None
        if not gdef:
            return data
        try:
            index = mdef.get_group_len_points_index(gdef)
        except Exception:
            return data
        have = len(data) // 2
        if index > have:
            data += await self.async_read(addr + have, index - have)
        return data

    async def async_scan(self, progress=None, delay=None, connect=True, full_model_read=True):
        """Scan all the models of the physical device and create the
        corresponding model objects within the device object based on the
        SunSpec model definitions.
        """
        self.base_addr = None
        self.delete_models()

        data = ''
        error = ''
        connected = False

        if connect:
            await self.async_connect()
            connected = True

            if delay is not None:
                await asyncio.sleep(delay)

        error_dict = {}
        if self.base_addr is None:
            for addr in self.base_addr_list:
                error_dict[addr] = ''
                try:
                    data = await self.async_read(addr, 3)
                    if data:
                        if data[:4] == b'SunS':
                            self.base_addr = addr
                            break
                        else:
                            error_dict[addr] = 'Device responded - not SunSpec register map'
                    else:
                        error_dict[addr] = 'Data time out'
                except SunSpecModbusClientError as e:
                    error_dict[addr] = str(e)
                except modbus_client.ModbusClientTimeout as e:
                    error_dict[addr] = str(e)
                except modbus_client.ModbusClientException as e:
                    error_dict[addr] = str(e)
                except Exception as e:
                    error_dict[addr] = str(e)

                if delay is not None:
                    await asyncio.sleep(delay)

        error = 'Error scanning SunSpec base addresses. \n'
        for k, v in error_dict.items():
            error += 'Base address %s error = %s. \n' % (k, v)

        if self.base_addr is not None:
            model_id_data = data[4:6]
            model_id = mb.data_to_u16(model_id_data)
            addr = self.base_addr + 2

            mid = 0
            while model_id != mb.SUNS_END_MODEL_ID:
                model_len_data = await self.async_read(addr + 1, 1)
                if model_len_data and len(model_len_data) == 2:
                    if progress is not None:
                        cont = progress('Scanning model %s' % model_id)
                        if not cont:
                            raise SunSpecModbusClientError('Device scan terminated')
                    model_len = mb.data_to_u16(model_len_data)

                    model_data = await self.async_model_data(model_id, addr, model_id_data + model_len_data)
                    model = self.model_class(model_id=model_id, model_addr=addr, model_len=model_len, data=model_data,
                                             mb_device=self)
                    if full_model_read and model.model_def:
                        await model.async_read()
                    model.mid = '%s_%s' % (self.did, mid)
                    mid += 1
                    self.add_model(model)

                    addr += model_len + 2
                    model_id_data = await self.async_read(addr, 1)
                    if model_id_data and len(model_id_data) == 2:
                        model_id = mb.data_to_u16(model_id_data)
                    else:
                        break
                else:
                    break

                if delay is not None:
                    await asyncio.sleep(delay)

        else:
            raise SunSpecModbusClientError(error)

        if connected:
            await self.async_disconnect()
