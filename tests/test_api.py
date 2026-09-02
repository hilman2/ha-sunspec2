"""Tests for SunSpec api."""

import time
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
from modbus_connection import ModbusSerialParams
from modbus_connection import ModbusTcpParams

import custom_components.sunspec2.pysunspec2.mb as mb
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.const import DEFAULT_SCAN_DELAY_SECONDS
from custom_components.sunspec2.const import MAX_SCAN_DELAY_SECONDS
from custom_components.sunspec2.const import MIN_SCAN_DELAY_SECONDS
from custom_components.sunspec2.const import TRANSPORT_RTU
from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.errors import ProtocolError
from custom_components.sunspec2.errors import TransientError
from custom_components.sunspec2.errors import TransportError
from custom_components.sunspec2.pysunspec2.modbus.client import SunSpecModbusClientError
from custom_components.sunspec2.pysunspec2.modbus.client import SunSpecModbusClientException
from custom_components.sunspec2.pysunspec2.modbus.client import SunSpecModbusClientTimeout
from custom_components.sunspec2.pysunspec2.modbus.modbus import ModbusClientConnectionClosed
from custom_components.sunspec2.pysunspec2.modbus.modbus import ModbusClientError
from custom_components.sunspec2.pysunspec2.modbus.unit_device import SunSpecModbusClientDeviceUnit

from .fake_unit import FakeConnection


async def test_api(hass, sunspec_client_mock):
    """Test API calls."""

    # To test the api submodule, we first create an instance of our API client
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    models = await api.async_get_models()
    assert models == [
        1,
        103,
        160,
        304,
        701,
        702,
        703,
        704,
        705,
        706,
        707,
        708,
        709,
        710,
        711,
        712,
    ]

    device_info = await api.async_get_device_info()

    assert device_info.getValue("Mn") == "SunSpecTest"
    assert device_info.getValue("SN") == "sn-123456789"

    model = await api.async_get_data(701)
    assert model.getValue("W") == 9800
    assert model.getMeta("W")["label"] == "Active Power"

    model = await api.async_get_data(705)
    keys = model.getKeys()
    assert len(keys) == 22


async def test_get_client(hass, sunspec_modbus_client_mock):
    """Test API calls."""

    # To test the api submodule, we first create an instance of our API client
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    client = await api.async_get_client()
    client.async_scan.assert_awaited_once()


async def test_modbus_connect(hass, sunspec_modbus_client_mock):
    """Test API calls."""

    # To test the api submodule, we first create an instance of our API client
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    client = await api.async_get_client()
    client.async_scan.assert_awaited_once()


async def test_modbus_connect_fail(hass, sunspec_modbus_client_mock):
    """A client that reports no link after connecting is a transport error."""
    sunspec_modbus_client_mock.is_connected.return_value = False
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    with pytest.raises(TransportError):
        await api.modbus_connect()


async def test_modbus_connect_exception(hass, sunspec_modbus_client_mock):
    sunspec_modbus_client_mock.async_connect.side_effect = ModbusClientError("refused")
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    with pytest.raises(TransportError):
        await api.modbus_connect()


async def test_read_model_timeout(hass, mocker):
    mocker.patch(
        "custom_components.sunspec2.api.SunSpecApiClient.async_read_model",
        side_effect=SunSpecModbusClientTimeout,
    )
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    with pytest.raises(TransientError):
        await api.async_get_data(1)


async def test_read_model_error(hass, mocker):
    mocker.patch(
        "custom_components.sunspec2.api.SunSpecApiClient.async_read_model",
        side_effect=SunSpecModbusClientException,
    )
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    with pytest.raises(DeviceError):
        await api.async_get_data(1)


class _HangingUpModel:
    """A model whose first read finds the device gone, like an ECU-R does."""

    def __init__(self, hangs_up_first: bool) -> None:
        self.reads = 0
        self.hangs_up_first = hangs_up_first

    async def async_read(self) -> None:
        self.reads += 1
        if self.hangs_up_first and self.reads == 1:
            raise ModbusClientConnectionClosed("Connection closed by peer")


async def test_read_model_connects_again_when_the_device_hangs_up(hass, mocker):
    """A hang-up mid-cycle costs one reconnect inside the cycle, not the cycle.

    Until now the transport reported the closed connection as a timeout,
    the cycle failed, and only the next cycle connected again. On a device
    that hangs up after every request (cjne/ha-sunspec#170) that meant
    every second poll was lost.
    """
    from types import SimpleNamespace

    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    first = SimpleNamespace(
        models={1: [_HangingUpModel(hangs_up_first=True)]}, async_disconnect=AsyncMock()
    )
    second = SimpleNamespace(
        models={1: [_HangingUpModel(hangs_up_first=False)]}, async_disconnect=AsyncMock()
    )
    connect = mocker.patch.object(api, "modbus_connect", side_effect=[first, second])

    wrapper = await api.async_read_model(1)

    assert connect.await_count == 2
    assert first.models[1][0].reads == 1
    assert second.models[1][0].reads == 1
    assert wrapper.num_models == 1
    # The dead session went out politely; the cached layout is not dropped.
    first.async_disconnect.assert_awaited_once()


async def test_device_that_keeps_hanging_up_is_a_transport_error(hass, mocker):
    mocker.patch(
        "custom_components.sunspec2.api.SunSpecApiClient.async_read_model",
        side_effect=ModbusClientConnectionClosed,
    )
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    with pytest.raises(TransportError):
        await api.async_get_data(1)


# ---------------------------------------------------------------------------
# Connection teardown on the failure path (#25)
# ---------------------------------------------------------------------------


async def test_failed_scan_tears_down_the_connected_client(
    hass, mocker, sunspec_modbus_client_mock
):
    """A client that connects and then fails its scan must not leak.

    This is the exact shape reported in #25: the TCP connect succeeds,
    the inverter resets the connection during the base-address walk, and
    scan() raises. Before the fix nothing could reach that client -
    get_client() only assigns self._client on success, so both close()
    and _force_disconnect() saw None and did nothing. On a single-slot
    inverter the abandoned session then occupies the only Modbus slot,
    which makes the failure sticky and makes the second attempt's error
    message describe our own leak instead of the original fault.
    """
    sunspec_modbus_client_mock.async_scan.side_effect = SunSpecModbusClientError(
        "Error scanning SunSpec base addresses"
    )
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    torn_down = mocker.patch.object(SunSpecApiClient, "_async_disconnect")

    with pytest.raises(ProtocolError):
        await api.modbus_connect()

    # Called with the client explicitly, because self._client is still
    # None at this point.
    torn_down.assert_awaited_once()
    assert torn_down.call_args.args[-1] is sunspec_modbus_client_mock


async def test_successful_connect_does_not_tear_down(hass, mocker, sunspec_modbus_client_mock):
    """The teardown must not fire on the happy path."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    torn_down = mocker.patch.object(SunSpecApiClient, "_async_disconnect")

    client = await api.modbus_connect()

    assert client is sunspec_modbus_client_mock
    torn_down.assert_not_awaited()


async def test_disconnect_takes_the_client_explicitly(hass):
    """The explicit-client form must work while self._client is None."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    orphan = Mock(async_disconnect=AsyncMock())

    await api._async_disconnect(orphan)

    orphan.async_disconnect.assert_awaited_once_with()
    # The instance's own (absent) client is untouched.
    assert api._client is None


# ---------- scan delay (#17) ------------------------------------------------


async def test_scan_delay_defaults_and_reaches_pysunspec2(hass, sunspec_modbus_client_mock):
    """The configured pacing is what scan() is actually called with.

    The coordinator rebuilds its client on every cycle, so scan() runs
    once per poll and pysunspec2 sleeps this long after every model it
    walks. A default that never reaches scan() would be invisible.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    client = await api.async_get_client()

    assert api._scan_delay == DEFAULT_SCAN_DELAY_SECONDS
    assert client.async_scan.call_args.kwargs["delay"] == DEFAULT_SCAN_DELAY_SECONDS


async def test_scan_delay_is_configurable(hass, sunspec_modbus_client_mock):
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass, scan_delay=0.75)
    client = await api.async_get_client()

    assert client.async_scan.call_args.kwargs["delay"] == 0.75


async def test_scan_delay_zero_passes_none(hass, sunspec_modbus_client_mock):
    """0 has to become None: pysunspec2 only skips the sleep on None."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass, scan_delay=0)
    client = await api.async_get_client()

    assert client.async_scan.call_args.kwargs["delay"] is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (-1.0, MIN_SCAN_DELAY_SECONDS),
        (99.0, MAX_SCAN_DELAY_SECONDS),
        ("0.5", 0.5),
    ],
)
async def test_scan_delay_is_clamped(hass, given, expected):
    """A corrupted options save must not reach time.sleep() as-is.

    A negative delay raises ValueError deep inside pysunspec2's scan
    walk, where the error message says nothing about our options form.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass, scan_delay=given)

    assert api._scan_delay == expected


# ---------- cached model structure (#17) ------------------------------------


class _FakeModel:
    def __init__(self, model_id=None, model_addr=0, model_len=0, data=None, mb_device=None):
        self.model_id = model_id
        self.model_addr = model_addr
        self.model_len = model_len
        self.data = data
        self.device = mb_device
        self.mid = None
        self.gname = f"model_{model_id}"


class _FakeClient:
    """Enough of a pysunspec2 client to exercise scan-vs-restore.

    scan() populates models the way the real walk does; read() answers
    the base-address header so the validating read can succeed.
    """

    model_class = _FakeModel

    def __init__(self, layout=None, header_model_id=None):
        self.base_addr = 40000
        self.did = "fake-did"
        self.models = {}
        self.scan_calls = 0
        self.reads = []
        self._layout = layout if layout is not None else [(1, 40002, 66), (103, 40070, 50)]
        self._header_model_id = (
            header_model_id if header_model_id is not None else self._layout[0][0]
        )
        # What sits right behind the last block. The end-of-chain marker
        # unless a test wants to model a firmware update that appended
        # models to the tree.
        self.end_marker = mb.SUNS_END_MODEL_ID

    def scan(self, **kwargs):
        self.scan_calls += 1
        self.models = {}
        for model_id, addr, length in self._layout:
            self.models.setdefault(model_id, []).append(_FakeModel(model_id, addr, length))

    def read(self, addr, count):
        self.reads.append((addr, count))
        if addr == self.base_addr:
            return b"SunS" + mb.u16_to_data(self._header_model_id)
        for model_id, model_addr, model_len in self._layout:
            if addr == model_addr:
                return mb.u16_to_data(model_id) + mb.u16_to_data(model_len)
        last_id, last_addr, last_len = self._layout[-1]
        if addr == last_addr + 2 + last_len:
            return mb.u16_to_data(self.end_marker)
        raise SunSpecModbusClientError(f"unexpected read at {addr}")

    def delete_models(self):
        self.models = {}

    def add_model(self, model):
        self.models.setdefault(model.model_id, []).append(model)

    def connect(self):
        pass

    def is_connected(self):
        return True

    # What the api client goes through: the twins hand the call to the
    # sync methods above, so a test can still patch ``read`` or ``scan``.

    async def async_scan(self, **kwargs):
        self.scan(**kwargs)

    async def async_read(self, addr, count):
        return self.read(addr, count)


async def _api_with_structure(hass, client):
    """An api client that has already scanned ``client`` once."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    await api._async_scan_or_restore(client)
    return api


async def test_first_connect_scans_and_caches(hass):
    client = _FakeClient()
    api = await _api_with_structure(hass, client)

    assert client.scan_calls == 1
    assert api._model_structure == [(1, 40002, 66), (103, 40070, 50)]
    assert api._base_addr == 40000


async def test_second_connect_restores_without_scanning(hass):
    """The whole point: a reconnect must not walk the model tree again."""
    client = _FakeClient()
    api = await _api_with_structure(hass, client)

    fresh = _FakeClient()
    await api._async_scan_or_restore(fresh)

    assert fresh.scan_calls == 0
    assert sorted(k for k in fresh.models) == [1, 103]
    rebuilt = fresh.models[103][0]
    assert (rebuilt.model_addr, rebuilt.model_len) == (40070, 50)
    # Three reads, and no more: base address, chain tail, end marker.
    assert fresh.reads == [(40000, 3), (40070, 2), (40122, 1)]


async def test_cached_structure_does_not_expire(hass):
    """There is no timer. A layout stays valid while it keeps validating.

    A rescan cannot confirm a layout, only replace it, and it is the one
    operation that can silently corrupt it: pysunspec2 walks the chain
    with ``addr += model_len + 2``, so a single misread length shifts
    every block behind it and scan() still returns without raising.
    """
    client = _FakeClient()
    api = await _api_with_structure(hass, client)

    for _ in range(50):
        fresh = _FakeClient()
        await api._async_scan_or_restore(fresh)
        assert fresh.scan_calls == 0


async def test_changed_chain_tail_triggers_a_rescan(hass):
    """The check that actually catches a moved model tree.

    Validating only the base address validates nothing: the first model
    is 1 (Common) on every SunSpec device ever built. Anything that
    changes in the middle of the chain moves the tail, so the tail is
    what has to be re-read.
    """
    client = _FakeClient()
    api = await _api_with_structure(hass, client)

    # Same first model, same base address, but the second block grew.
    moved = _FakeClient(layout=[(1, 40002, 66), (103, 40070, 60)])
    await api._async_scan_or_restore(moved)

    assert moved.scan_calls == 1


async def test_chain_that_grew_past_its_end_triggers_a_rescan(hass):
    """A firmware update that only appends models still gets noticed.

    The tail block is where it always was, so only the end marker can
    tell us the chain now continues past it.
    """
    client = _FakeClient()
    api = await _api_with_structure(hass, client)

    grown = _FakeClient()
    grown.end_marker = 704
    await api._async_scan_or_restore(grown)

    assert grown.scan_calls == 1


async def test_device_that_will_not_read_past_the_chain_is_not_punished(hass):
    """pysunspec2's own scan tolerates a chain that just stops answering.

    So a device that errors on the register behind the last block keeps
    its cache instead of rescanning on every single connect.
    """
    client = _FakeClient()
    api = await _api_with_structure(hass, client)

    quiet = _FakeClient()
    last_id, last_addr, last_len = quiet._layout[-1]
    end_addr = last_addr + 2 + last_len

    original_read = quiet.read

    def _read(addr, count):
        if addr == end_addr:
            raise SunSpecModbusClientError("Illegal data address")
        return original_read(addr, count)

    quiet.read = _read
    await api._async_scan_or_restore(quiet)

    assert quiet.scan_calls == 0


async def test_changed_model_tree_triggers_a_rescan(hass):
    """A firmware update that reorders the tree must not be read at old offsets.

    Without the validating read this is the dangerous case: the reads
    would succeed and return values that are simply wrong.
    """
    client = _FakeClient()
    api = await _api_with_structure(hass, client)

    swapped = _FakeClient(layout=[(103, 40002, 50)], header_model_id=103)
    await api._async_scan_or_restore(swapped)

    assert swapped.scan_calls == 1


async def test_failed_validation_read_falls_back_to_scanning(hass):
    client = _FakeClient()
    api = await _api_with_structure(hass, client)

    fresh = _FakeClient()
    fresh.read = Mock(side_effect=OSError("boom"))
    await api._async_scan_or_restore(fresh)

    assert fresh.scan_calls == 1


async def test_reconnect_next_drops_the_cached_structure(hass):
    """After a failure the layout is a suspect, not an asset."""
    client = _FakeClient()
    api = await _api_with_structure(hass, client)
    assert api._model_structure is not None

    api.reconnect_next()

    assert api._model_structure is None
    fresh = _FakeClient()
    await api._async_scan_or_restore(fresh)
    assert fresh.scan_calls == 1


async def test_close_keeps_the_cached_structure(hass, sunspec_modbus_client_mock):
    """close() runs at the end of every healthy cycle and must not invalidate.

    If it did, the cache would never survive to its second use and the
    scan would still run on every poll.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    api._base_addr = 40000
    api._model_structure = [(1, 40002, 66)]

    await api.async_close()

    assert api._model_structure == [(1, 40002, 66)]


async def test_vendor_models_without_a_group_name_stay_cached(hass):
    """model_list would drop them; models keeps them.

    A vendor block with no bundled definition gets no gname, so
    add_model() never puts it in model_list. Capturing from there would
    silently stop polling it after the first reconnect.
    """
    client = _FakeClient(layout=[(1, 40002, 66), (64110, 40120, 30)])
    api = await _api_with_structure(hass, client)

    assert (64110, 40120, 30) in api._model_structure


async def test_restore_builds_real_pysunspec2_models(hass):
    """Proof against the library, not just against our fake client.

    The rebuild reproduces what scan() does internally, so it has to be
    checked against the real SunSpecModbusClientModel: if the model
    definition does not resolve, model.read() later has nothing to
    decode registers with and every point comes back None.
    """
    client = SunSpecModbusClientDeviceUnit(FakeConnection(), slave_id=1)

    async def _read(addr, count, op=None):
        if addr == 40000:
            return b"SunS" + mb.u16_to_data(103)
        if addr == 40002:
            return mb.u16_to_data(103) + mb.u16_to_data(50)
        return mb.u16_to_data(mb.SUNS_END_MODEL_ID)

    client.async_read = _read

    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    api._base_addr = 40000
    api._model_structure = [(103, 40002, 50)]

    assert await api._async_restore_model_structure(client) is True

    assert client.base_addr == 40000
    model = client.models[103][0]
    assert (model.model_addr, model.model_len) == (40002, 50)
    # The bundled model_103.json resolved, so the points are decodable.
    assert model.model_def is not None
    assert model.gname == "inverter_three_phase"
    # And it is reachable the way read_model() reaches it.
    assert client.models[103] == [model]


# ---------- partial scan tolerance (cjne/ha-sunspec#375) --------------------


async def test_partial_scan_keeps_the_models_it_found(hass, mocker):
    """A read that fails partway must not discard the whole chain.

    pysunspec2 walks the model chain and calls add_model() as it goes,
    so a failure at model seven throws away models one to six and the
    inverter looks like it speaks no SunSpec at all. Reported upstream
    for an SMA STP110-60 (cjne/ha-sunspec#375).
    """
    mocker.patch.object(SunSpecApiClient, "_get_connection", return_value=FakeConnection())

    async def _partial_scan(self, **kwargs):
        self.models = {1: [Mock()], "common": [Mock()], 103: [Mock()]}
        raise SunSpecModbusClientError("Unknown error")

    mocker.patch.object(SunSpecModbusClientDeviceUnit, "async_scan", _partial_scan)

    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    client = await api.async_get_client()

    assert sorted(k for k in client.models if isinstance(k, int)) == [1, 103]


async def test_partial_scan_does_not_cache_the_truncated_layout(hass, mocker):
    """Caching a layout we know is short would pin the missing models out.

    And it would validate cleanly while doing so, because a truncated
    chain still has a well-formed tail, so nothing would ever prompt the
    rescan that would find the rest.
    """
    mocker.patch.object(SunSpecApiClient, "_get_connection", return_value=FakeConnection())

    async def _partial_scan(self, **kwargs):
        self.models = {1: [Mock()]}
        raise SunSpecModbusClientError("Unknown error")

    mocker.patch.object(SunSpecModbusClientDeviceUnit, "async_scan", _partial_scan)

    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    await api.async_get_client()

    assert api._model_structure is None


async def test_scan_that_found_nothing_still_fails(hass, mocker):
    """Tolerating a partial scan must not tolerate a total failure.

    A device that answers nothing is a misconfiguration the user needs
    told about, not something to paper over with an empty model list.
    """
    mocker.patch.object(SunSpecApiClient, "_get_connection", return_value=FakeConnection())

    async def _failed_scan(self, **kwargs):
        self.models = {}
        raise SunSpecModbusClientError("Error scanning SunSpec base addresses")

    mocker.patch.object(SunSpecModbusClientDeviceUnit, "async_scan", _failed_scan)

    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    with pytest.raises(ProtocolError):
        await api.async_get_client()


async def test_partial_scan_falls_back_to_the_last_known_layout(hass):
    """A scan that stops early must not shorten the model list.

    A truncated list drops the affected entities to "unknown" on a cycle
    that still counts as a success, so the stale-data tolerance never
    gets to hold the last good value and the user sees a gap that looks
    exactly like a connection drop (#42). The layout we already have
    still validates against the device, so it is strictly better than
    what the failed scan produced.
    """
    api = await _api_with_structure(hass, _FakeClient())

    class _TruncatingClient(_FakeClient):
        def scan(self, **kwargs):
            self.scan_calls += 1
            self.models = {1: [_FakeModel(1, 40002, 66)]}
            raise SunSpecModbusClientException("Illegal data address")

    truncated = _TruncatingClient()
    await api._async_scan_or_restore(truncated)

    assert sorted(k for k in truncated.models) == [1, 103]
    assert api.last_scan_was_partial is False


async def test_scan_timeout_is_never_swallowed(hass):
    """A timeout leaves the socket suspect, so the cycle has to fail.

    pysunspec2 builds every Modbus TCP frame with transaction id 0 and
    never checks it on the way in, so a late answer to the request we
    gave up on is read as the answer to the next one. From there every
    register lands in the wrong point and the values look plausible
    rather than missing. Continuing on that socket, with a cached layout
    or without, is the one thing that must not happen.
    """
    api = await _api_with_structure(hass, _FakeClient())

    class _TimingOutClient(_FakeClient):
        def scan(self, **kwargs):
            self.scan_calls += 1
            self.models = {1: [_FakeModel(1, 40002, 66)]}
            raise SunSpecModbusClientTimeout("Response timeout")

    stuck = _TimingOutClient()
    # Make the cached layout unusable so the code has to reach the scan.
    stuck.end_marker = 704

    with pytest.raises(SunSpecModbusClientTimeout):
        await api._async_scan_or_restore(stuck)


async def test_structure_survives_an_export_import_round_trip(hass):
    """What the coordinator persists has to rebuild the same layout."""
    api = await _api_with_structure(hass, _FakeClient())
    payload = api.export_model_structure()

    restored = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    assert restored.import_model_structure(payload) is True

    fresh = _FakeClient()
    await restored._async_scan_or_restore(fresh)

    assert fresh.scan_calls == 0
    assert sorted(k for k in fresh.models) == [1, 103]


async def test_import_rejects_junk(hass):
    """A hand-edited or half-written store file must not reach the device."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)

    for junk in (
        None,
        {},
        {"base_addr": 40000},
        {"base_addr": "40000", "models": [[1, 40002, 66]]},
        {"base_addr": 40000, "models": []},
        {"base_addr": 40000, "models": [[1, 40002]]},
        {"base_addr": 40000, "models": [[1, "40002", 66]]},
    ):
        assert api.import_model_structure(junk) is False
        assert api._model_structure is None


async def test_revision_only_moves_when_the_layout_does(hass):
    """A rescan that confirms the layout must not trigger a store write."""
    client = _FakeClient()
    api = await _api_with_structure(hass, client)
    revision = api.structure_revision

    # Force a real rescan by invalidating, then rescan the same device.
    api._invalidate_model_structure()
    await api._async_scan_or_restore(_FakeClient())
    assert api.structure_revision > revision

    revision = api.structure_revision
    same = _FakeClient()
    same.scan()
    api._capture_model_structure(same)
    assert api.structure_revision == revision


async def test_the_connection_is_built_once_with_the_request_timeout(hass, mocker):
    """One ModbusConnection per api client, carrying the entry's timeout.

    The connection outlives the client: a close drops the link and the
    models, and the next client connects on the same object.
    """
    from custom_components.sunspec2.api import SETUP_TIMEOUT

    built = mocker.patch("custom_components.sunspec2.api.ModbusConnection")
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass, timeout=SETUP_TIMEOUT)

    first = api._get_connection()
    second = api._get_connection()

    assert first is second
    built.assert_called_once_with(ModbusTcpParams(host="test", port=123), timeout=SETUP_TIMEOUT)


async def test_rtu_entries_connect_over_serial_params(hass):
    api = SunSpecApiClient(
        host="/dev/ttyUSB0",
        port=19200,
        unit_id=3,
        hass=hass,
        transport=TRANSPORT_RTU,
        serial_port="/dev/ttyUSB0",
        baudrate=19200,
        parity="E",
    )

    assert api._params() == ModbusSerialParams(device="/dev/ttyUSB0", baudrate=19200, parity="E")

    without_port = SunSpecApiClient(
        host="rtu", port=9600, unit_id=1, hass=hass, transport=TRANSPORT_RTU
    )
    with pytest.raises(TransportError):
        without_port._params()


async def test_close_drops_the_client_whether_forced_or_not(hass):
    """Both closes go through the client's disconnect; there is no RST to send any more.

    Every close used to be an RST, on the theory that it frees a
    single-slot inverter faster. modbus-connection owns the socket and
    closes it with a FIN either way; ``force`` is what the log says.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    polite = Mock(async_disconnect=AsyncMock())
    api._client = polite

    await api.async_close()
    polite.async_disconnect.assert_awaited_once_with()
    assert api._client is None

    forced = Mock(async_disconnect=AsyncMock())
    api._client = forced
    await api.async_close(force=True)
    forced.async_disconnect.assert_awaited_once_with()
    assert api._client is None


async def test_reconnect_flag_is_cleared_even_without_a_live_client(hass, mocker):
    """One cycle must not build two clients.

    The flag used to be cleared only inside "if self._client is not
    None", and between cycles the client is always None because every
    cycle ended in close(). So a flag set by a failed cycle survived
    into the next one: its first get_client() built a client with the
    flag still standing, and the second get_client() of that same cycle
    tore that fresh client down and built another. Two connects and two
    model scans per cycle, on hardware that grants one session at a
    time, is how one failed cycle became a run of them.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    built = []

    def _build_and_record():
        built.append(Mock())
        return built[-1]

    mocker.patch.object(api, "modbus_connect", side_effect=_build_and_record)
    dropped = mocker.patch.object(api, "_async_disconnect")

    api.reconnect_next()
    assert api._client is None

    first = await api.async_get_client()
    second = await api.async_get_client()

    assert first is second
    assert len(built) == 1
    dropped.assert_not_awaited()
