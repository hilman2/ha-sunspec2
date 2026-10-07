"""The pysunspec2 file client, kept for the tests: a device read from a JSON file.

Upstream: sunspec/pysunspec2 v1.3.6, Apache 2.0, see pysunspec2/__init__.py.
It serves the fixtures in tests/test_data and the register images built from them.
"""

import json
import uuid

from custom_components.sunspec2.pysunspec2 import device
from custom_components.sunspec2.pysunspec2 import mb
from custom_components.sunspec2.pysunspec2 import mdef


class FileClientError(Exception):
    pass


class FileClientPoint(device.Point):
    def read(self):
        pass

    def write(self):
        pass

    # ha-sunspec2: the async twins the integration reads and writes
    # through; a file holds no registers, so they do what read and write do.

    async def async_read(self):
        self.read()

    async def async_write(self):
        self.write()


class FileClientGroup(device.Group):
    def read(self):
        pass

    def write(self):
        pass

    async def async_read(self, len=None):
        self.read()

    async def async_write(self):
        self.write()


class FileClientModel(FileClientGroup):
    def __init__(
        self,
        model_id=None,
        model_addr=0,
        model_len=0,
        model_def=None,
        data=None,
        group_class=FileClientGroup,
        point_class=FileClientPoint,
    ):
        self.model_id = model_id
        self.model_addr = model_addr
        if model_len is None:
            self.model_len = 0
        else:
            self.model_len = model_len
        self.model_def = model_def
        self.error_info = ""
        self.mid = None
        self.device = None
        self.model = self

        gdef = None
        try:
            if self.model_def is None and model_id is not None:
                self.model_def = device.get_model_def(model_id)
            if self.model_def is not None:
                gdef = self.model_def.get(mdef.GROUP)
        except Exception as e:
            self.add_error(str(e))

        FileClientGroup.__init__(
            self,
            gdef=gdef,
            model=self,
            model_offset=0,
            group_len=self.model_len,
            data=data,
            data_offset=0,
            group_class=group_class,
            point_class=point_class,
        )

    def add_error(self, error_info):
        self.error_info = f"{self.error_info}{error_info}\n"


class FileClientDevice(device.Device):
    def __init__(self, filename=None, addr=40002, model_class=FileClientModel):
        device.Device.__init__(self, model_class=model_class)
        self.did = str(uuid.uuid4())
        self.filename = filename
        self.addr = addr

    def scan(self, data=None):
        try:
            if self.filename:
                with open(self.filename) as f:
                    data = json.load(f)

                mid = 0
                addr = self.addr
                for m in data.get("models"):
                    model_id = m.get("ID")
                    model_len = m.get("L")
                    if model_id != mb.SUNS_END_MODEL_ID:
                        model = self.model_class(
                            model_id=model_id,
                            model_addr=addr,
                            model_len=model_len,
                            model_def=None,
                            data=m,
                        )
                        model.mid = f"{self.did}_{mid}"
                        mid += 1
                        self.add_model(model)
                        addr += model.len
        except Exception as e:
            raise FileClientError(str(e)) from e

    def read(self):
        return ""

    def write(self):
        return

    def close(self):
        return
