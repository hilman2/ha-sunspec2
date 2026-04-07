"""Tests for the typed error hierarchy in custom_components.sunspec2.errors."""

import pytest

from custom_components.sunspec2.errors import (
    CATEGORIES,
    DeviceError,
    ProtocolError,
    SunSpecError,
    TransientError,
    TransportError,
)


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
