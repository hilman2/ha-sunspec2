"""Tests for the model label helpers.

``device_model_suffix`` feeds ``SunSpecEntity.device_info`` and runs
inside the event loop, so it must stay a pure in-memory lookup: these
tests only cover its resolution order and label cleanup, never file
IO. The file-reading ``sunspec_model_label`` path is exercised via
the config-flow tests.
"""

from custom_components.sunspec2.model_labels import device_model_suffix


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
