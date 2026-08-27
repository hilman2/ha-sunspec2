"""Tests for the typed error hierarchy in custom_components.sunspec2.errors
and the Phase-3 coordinator hooks (per-category buffer, consecutive
failure counters, Repairs panel).
"""

import pytest
import sunspec2.file.client as file_client
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2 import SunSpecDataUpdateCoordinator
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.errors import CATEGORIES
from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.errors import ProtocolError
from custom_components.sunspec2.errors import SunSpecError
from custom_components.sunspec2.errors import TransientError
from custom_components.sunspec2.errors import TransportError
from custom_components.sunspec2.models import SunSpecModelWrapper

from .const import MOCK_CONFIG


def test_categories_tuple_matches_classes():
    """CATEGORIES must list every concrete error class category, exactly once."""
    classes = (TransportError, ProtocolError, DeviceError, TransientError)
    assert tuple(c.category for c in classes) == CATEGORIES


@pytest.mark.parametrize(
    ("cls", "expected_category"),
    [
        (TransportError, "transport"),
        (ProtocolError, "protocol"),
        (DeviceError, "device"),
        (TransientError, "transient"),
    ],
)
def test_each_class_has_its_category(cls, expected_category):
    assert cls.category == expected_category
    assert cls("msg").category == expected_category


def test_all_subclasses_inherit_from_sunspec_error():
    for cls in (TransportError, ProtocolError, DeviceError, TransientError):
        assert issubclass(cls, SunSpecError)
        assert issubclass(cls, Exception)


def test_can_be_raised_and_caught_as_base():
    with pytest.raises(SunSpecError):
        raise TransportError("boom")
    with pytest.raises(SunSpecError):
        raise ProtocolError("nope")
    with pytest.raises(SunSpecError):
        raise DeviceError("bad value")
    with pytest.raises(SunSpecError):
        raise TransientError("timeout")


def test_preserves_cause_chain():
    inner = ValueError("underlying")
    try:
        try:
            raise inner
        except ValueError as exc:
            raise TransportError("wrapped") from exc
    except TransportError as outer:
        assert outer.__cause__ is inner


# ----- Coordinator integration tests for Phase 3 -----------------------------


def _build_coordinator(hass, options=None) -> SunSpecDataUpdateCoordinator:
    """Construct a coordinator + entry pair for direct error-recording tests."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, options=options or {}, entry_id="test_p3"
    )
    entry.add_to_hass(hass)
    api = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    return SunSpecDataUpdateCoordinator(hass, client=api, entry=entry)


async def test_record_error_routes_to_correct_category(hass):
    """Each category lands in its own deque, the others stay empty."""
    coordinator = _build_coordinator(hass)

    coordinator._record_error(TransportError("tcp gone"))

    assert len(coordinator._recent_errors["transport"]) == 1
    assert coordinator._recent_errors["protocol"] == deque_empty()
    assert coordinator._recent_errors["device"] == deque_empty()
    assert coordinator._recent_errors["transient"] == deque_empty()
    entry = coordinator._recent_errors["transport"][0]
    assert entry["type"] == "TransportError"
    assert entry["msg"] == "tcp gone"
    assert "ts" in entry


async def test_consecutive_failures_increment_per_category(hass):
    """Multiple errors of the same kind bump only that counter."""
    coordinator = _build_coordinator(hass)

    coordinator._record_error(TransportError("a"))
    coordinator._record_error(TransportError("b"))
    coordinator._record_error(DeviceError("c"))

    assert coordinator._consecutive_failures["transport"] == 2
    assert coordinator._consecutive_failures["device"] == 1
    assert coordinator._consecutive_failures["protocol"] == 0
    assert coordinator._consecutive_failures["transient"] == 0


async def test_repair_issue_after_three_transport_errors(hass):
    """Three consecutive TransportErrors register an issue in the registry."""
    coordinator = _build_coordinator(hass)
    issue_id = f"{coordinator.entry.entry_id}_transport"

    coordinator._record_error(TransportError("first"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    coordinator._record_error(TransportError("second"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    coordinator._record_error(TransportError("third - tipping point"))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "transport_error"
    assert issue.translation_placeholders["host"] == "test_host"
    assert "third - tipping point" in issue.translation_placeholders["error"]


async def test_repair_issue_after_three_device_errors(hass):
    """Three consecutive DeviceErrors register a device_error issue."""
    coordinator = _build_coordinator(hass)
    issue_id = f"{coordinator.entry.entry_id}_device"

    for i in range(3):
        coordinator._record_error(DeviceError(f"modbus exception {i}"))

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "device_error"


async def test_protocol_error_registers_immediately(hass):
    """ProtocolError fires the issue on the very first occurrence."""
    coordinator = _build_coordinator(hass)
    issue_id = f"{coordinator.entry.entry_id}_protocol"

    coordinator._record_error(ProtocolError("no SunS marker"))

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "protocol_error"


async def test_transient_errors_never_register_issue(hass):
    """Even ten transient errors must not produce a Repairs entry."""
    coordinator = _build_coordinator(hass)
    issue_id = f"{coordinator.entry.entry_id}_transient"

    for _ in range(10):
        coordinator._record_error(TransientError("response timeout"))

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    # Counter still ticks up - the diagnostics dump shows it.
    assert coordinator._consecutive_failures["transient"] == 10


async def test_clear_repair_issues_removes_active_issue(hass):
    """_clear_repair_issues drops every per-category issue this entry owns."""
    coordinator = _build_coordinator(hass)
    coordinator._record_error(ProtocolError("scan failed"))
    issue_id = f"{coordinator.entry.entry_id}_protocol"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    coordinator._clear_repair_issues()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# ----- Issue #52: an inverter that sleeps at night is not a broken one -------


class _FakeWrapper:
    """Minimal stand-in for SunSpecModelWrapper carrying one point."""

    def __init__(self, points: dict) -> None:
        self._points = points

    def getValue(self, point_name: str, model_index: int = 0):
        return self._points[point_name]


@pytest.mark.parametrize("state", [1, 2, 6, 8])
async def test_transport_repair_suppressed_while_inverter_reported_shutdown(hass, state):
    """OFF / SLEEPING / SHUTTING_DOWN / STANDBY means the silence was announced."""
    coordinator = _build_coordinator(hass)
    coordinator._last_operating_state = state
    issue_id = f"{coordinator.entry.entry_id}_transport"

    for i in range(10):
        coordinator._record_error(TransportError(f"connect timeout {i}"))

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    # The failures are still recorded, so diagnostics can show the night.
    assert coordinator._consecutive_failures["transport"] == 10
    assert len(coordinator._recent_errors["transport"]) == 10


@pytest.mark.parametrize("state", [None, 3, 4, 5, 7])
async def test_transport_repair_still_fires_for_an_awake_inverter(hass, state):
    """A device that was running, or never said, still escalates as before."""
    coordinator = _build_coordinator(hass)
    coordinator._last_operating_state = state
    issue_id = f"{coordinator.entry.entry_id}_transport"

    for i in range(3):
        coordinator._record_error(TransportError(f"connect timeout {i}"))

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_transport_repair_suppressed_by_option(hass):
    """The opt-out covers devices whose shutdown we never get to observe."""
    coordinator = _build_coordinator(hass, options={"standby_when_idle": True})
    assert coordinator.standby_when_idle is True
    issue_id = f"{coordinator.entry.entry_id}_transport"

    for i in range(10):
        coordinator._record_error(TransportError(f"connect timeout {i}"))

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_device_and_protocol_errors_escalate_during_standby(hass):
    """Only transport is suppressed: an answering inverter is an awake one."""
    coordinator = _build_coordinator(hass, options={"standby_when_idle": True})
    coordinator._last_operating_state = 2

    for i in range(3):
        coordinator._record_error(DeviceError(f"modbus exception {i}"))
    coordinator._record_error(ProtocolError("no SunS marker"))

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"{coordinator.entry.entry_id}_device") is not None
    assert registry.async_get_issue(DOMAIN, f"{coordinator.entry.entry_id}_protocol") is not None


async def test_read_operating_state_prefers_a_real_inverter_model(hass):
    """Model 101/103-family points are read, model 701's homonym is not."""
    coordinator = _build_coordinator(hass)

    assert coordinator._read_operating_state({103: _FakeWrapper({"St": 2})}) == 2
    assert coordinator._read_operating_state({111: _FakeWrapper({"St": 6})}) == 6
    # Model 701 has an "St" point too, but its enum is 0 OFF / 1 ON.
    # Reading it here would turn a healthy inverter into a sleeping one.
    assert coordinator._read_operating_state({701: _FakeWrapper({"St": 1})}) is None
    # Nothing to read is not the same as "asleep".
    assert coordinator._read_operating_state({160: _FakeWrapper({"DCW": 4200})}) is None
    assert coordinator._read_operating_state({}) is None
    assert coordinator._downtime_is_expected() is False


async def test_read_operating_state_against_the_real_wrapper(hass):
    """The point path works against a genuine SunSpecModelWrapper."""
    client = file_client.FileClientDevice("./tests/test_data/inverter.json")
    client.scan()
    coordinator = _build_coordinator(hass)

    state = coordinator._read_operating_state({103: SunSpecModelWrapper(client.models[103])})

    # The fixture inverter is running: MPPT, not one of the standby states.
    assert state == 4
    coordinator._last_operating_state = state
    assert coordinator._downtime_is_expected() is False


async def test_successful_cycle_records_the_operating_state(hass):
    """The state has to be captured while the inverter still answers."""
    coordinator = _build_coordinator(hass)

    coordinator._after_successful_cycle({103: _FakeWrapper({"St": 8})})

    assert coordinator._last_operating_state == 8
    assert coordinator._downtime_is_expected() is True


async def test_ignored_issue_survives_a_successful_cycle(hass):
    """Clearing an issue must not revoke the user's "Ignore".

    HA's async_delete drops dismissed_version with the entry, so an
    inverter that is unreachable every night used to re-raise the issue
    unignored every evening no matter how often the user dismissed it.
    """
    coordinator = _build_coordinator(hass)
    registry = ir.async_get(hass)
    issue_id = f"{coordinator.entry.entry_id}_transport"
    for i in range(3):
        coordinator._record_error(TransportError(f"gone {i}"))
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    registry.async_ignore(DOMAIN, issue_id, True)

    coordinator._clear_repair_issues()

    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.dismissed_version is not None


async def test_teardown_clears_even_an_ignored_issue(hass):
    """Unloading the entry leaves nothing behind, ignored or not."""
    coordinator = _build_coordinator(hass)
    registry = ir.async_get(hass)
    issue_id = f"{coordinator.entry.entry_id}_transport"
    for i in range(3):
        coordinator._record_error(TransportError(f"gone {i}"))
    registry.async_ignore(DOMAIN, issue_id, True)

    coordinator._clear_repair_issues(force=True)

    assert registry.async_get_issue(DOMAIN, issue_id) is None


def deque_empty():
    """Helper for clean-empty deque equality assertions."""
    from collections import deque

    return deque(maxlen=20)
