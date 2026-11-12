"""Tests for the model label helpers.

``device_model_suffix`` feeds ``SunSpecEntity.device_info`` and runs
inside the event loop, so it must stay a pure in-memory lookup: its
tests only cover its resolution order and label cleanup, never file
IO. ``sunspec_model_label`` reads the bundled model definitions for
the config-flow forms; its tests cover the label it builds and what it
falls back to when a definition is missing or unusable.
"""

import logging

import pytest

from custom_components.sunspec2 import model_labels
from custom_components.sunspec2.model_labels import device_model_suffix
from custom_components.sunspec2.model_labels import sunspec_model_label
from custom_components.sunspec2.model_labels import sunspec_model_labels


def test_suffix_uses_spec_label() -> None:
    assert (
        device_model_suffix(
            103, {"name": "inverter_three_phase", "label": "Inverter (Three Phase)"}
        )
        == "Inverter (Three Phase)"
    )


def test_suffix_curated_override_beats_label() -> None:
    # Model 160's official label is "Multiple MPPT Inverter Extension
    # Model", which would bloat every composed entity friendly name.
    assert device_model_suffix(160, {"name": "mppt", "label": "whatever"}) == "MPPT"


def test_suffix_normalises_underscores() -> None:
    # Model 122 is labelled "Measurements_Status" in the spec.
    assert device_model_suffix(122, {"name": "status", "label": "Measurements_Status"}) == (
        "Measurements Status"
    )


def test_suffix_strips_redundant_model_tail() -> None:
    assert device_model_suffix(304, {"name": "inclinometer", "label": "Inclinometer Model"}) == (
        "Inclinometer"
    )


def test_suffix_falls_back_to_group_name() -> None:
    assert device_model_suffix(64201, {"name": "vendor_ext"}) == "vendor ext"


def test_suffix_falls_back_to_model_id() -> None:
    assert device_model_suffix(64201, {}) == "Model 64201"


def test_suffix_without_any_information() -> None:
    # Test stubs may construct entities without a model id; the caller
    # then falls back to the bare Md-based device name.
    assert device_model_suffix(None, {}) is None


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    """An empty directory in place of the bundled model definitions.

    ``sunspec_model_label`` caches per model id for the life of the
    process, so the cache is emptied on both sides of the test.
    """
    monkeypatch.setattr(model_labels, "_MODEL_DIR", tmp_path)
    sunspec_model_label.cache_clear()
    yield tmp_path
    sunspec_model_label.cache_clear()


def test_label_is_the_spec_label_with_the_model_id() -> None:
    assert sunspec_model_label(103) == "Inverter (Three Phase) (103)"


def test_labels_are_keyed_by_the_id_as_text_in_ascending_order() -> None:
    """``cv.multi_select`` compares the keys with the strings the frontend sends back."""
    assert list(sunspec_model_labels({160, 1, 103}).items()) == [
        ("1", "Common (1)"),
        ("103", "Inverter (Three Phase) (103)"),
        ("160", "Multiple MPPT Inverter Extension Model (160)"),
    ]


def test_label_of_a_model_without_a_bundled_definition_is_generic(model_dir, caplog) -> None:
    """A vendor model has no definition to parse, so there is no parse failure to log."""
    with caplog.at_level(logging.DEBUG, logger="custom_components.sunspec2.model_labels"):
        assert sunspec_model_label(64999) == "Model 64999"

    assert "Could not parse" not in caplog.text


def test_label_of_a_definition_that_does_not_parse_is_generic(model_dir, caplog) -> None:
    (model_dir / "model_103.json").write_text('{"id": 103, "group": ', encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="custom_components.sunspec2.model_labels"):
        assert sunspec_model_label(103) == "Model 103"

    assert "Could not parse model_103.json" in caplog.text


def test_label_of_a_definition_without_a_group_label_is_generic(model_dir) -> None:
    (model_dir / "model_103.json").write_text(
        '{"id": 103, "group": {"name": "inverter"}}', encoding="utf-8"
    )

    assert sunspec_model_label(103) == "Model 103"
