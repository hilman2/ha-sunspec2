import struct


class MockSocket(object):
    def __init__(self):
        self.connected = False
        self.timeout = 0
        self.ipaddr = None
        self.ipport = None
        self.buffer = []

        self.request = []

        # A Modbus TCP server echoes the transaction id of the request into
        # its response, and the client checks it. The canned responses in
        # the tests were written with id 0 throughout, so the mock stamps
        # the id of the last request onto every response frame it starts,
        # the way a real device would. Tests that need to deliver a frame
        # with a wrong id switch this off.
        self.echo_transaction_id = True
        self._frame_remaining = 0

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, ipaddrAndipportTup):
        self.connected = True
        self.ipaddr = ipaddrAndipportTup[0]
        self.ipport = ipaddrAndipportTup[1]

    def close(self):
        self.connected = False

    def recv(self, size):
        if len(self.buffer) == 0:
            return b''
        print(f"MockSocket.recv: size={size}. Message: {self.buffer[0]}")
        chunk = self.buffer.pop(0)
        if self._frame_remaining <= 0 and len(chunk) >= 6:
            # First chunk of a frame: the MBAP header is complete, so the
            # frame length is known and the transaction id can be stamped.
            if self.echo_transaction_id and self.request:
                chunk = self.request[-1][:2] + chunk[2:]
            self._frame_remaining = 6 + struct.unpack('>H', chunk[4:6])[0]
        self._frame_remaining -= len(chunk)
        return chunk

    def sendall(self, data):
        self.request.append(data)

    def _set_buffer(self, resp_list):
        for bs in resp_list:
            self.buffer.append(bs)

    def clear_buffer(self):
        self.buffer = []
        self._frame_remaining = 0


def mock_socket(AF_INET, SOCK_STREAM):
    return MockSocket()


def mock_tcp_connect(self):
    if self.client.socket is None:
        self.client.socket = mock_socket('foo', 'bar')
    self.client.socket.settimeout(999)
    self.client.socket.connect((999, 999))
    pass


def mock_tcp_disconnect(self):
    pass
