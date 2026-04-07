"""Tests for the typed error hierarchy in custom_components.sunspec2.errors
and the Phase-3 coordinator hooks (per-category buffer, consecutive
failure counters, Repairs panel).
"""

import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sunspec2 import SunSpecDataUpdateCoordinator
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.errors import (
    CATEGORIES,
    DeviceError,
    ProtocolError,
    SunSpecError,
    TransientError,
    TransportError,
)

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


def _build_coordinator(hass) -> SunSpecDataUpdateCoordinator:
    """Construct a coordinator + entry pair for direct error-recording tests."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test_p3")
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


def deque_empty():
    """Helper for clean-empty deque equality assertions."""
    from collections import deque

    return deque(maxlen=20)
