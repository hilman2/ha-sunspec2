# Modified for ha-sunspec2: the socket and serial clients are gone, see __init__.py. Origin and license there.
"""
    Copyright (C) 2018 SunSpec Alliance
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

# What is left of upstream's transport module: the request limits, the
# function codes and the exception classes the rest of the fork raises.
# The socket and serial clients that used to live here were replaced by
# unit_device.py, which talks through modbus-connection and raises these
# same classes, so nothing above the transport had to change.

PARITY_NONE = 'N'
PARITY_EVEN = 'E'

REQ_COUNT_MAX = 125
REQ_WRITE_COUNT_MAX = 123

FUNC_READ_HOLDING = 3
FUNC_READ_INPUT = 4
FUNC_WRITE_MULTIPLE = 16
FUNC_WRITE_SINGLE = 6


class ModbusClientError(Exception):
    pass


class ModbusClientTimeout(ModbusClientError):
    pass


class ModbusClientException(ModbusClientError):
    pass


class ModbusClientConnectionClosed(ModbusClientError):
    """The peer closed the connection while a response was due.

    Not a timeout: the device answered by hanging up, which some do after
    every request or after a short idle time (the APsystems ECU-R, for
    one). The link is gone; the caller has to connect again.
    """
