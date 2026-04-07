"""Tests for SunSpecModelWrapper extracted in Phase 4.

Uses the existing tests/test_data/inverter.json fixture via the
sunspec2.file.client.FileClientDevice path. The wrapper has zero
dependencies on the rest of the integration, so the tests instantiate
it directly without any HA fixtures.
"""

from __future__ import annotations

import sunspec2.file.client as file_client

from custom_components.sunspec2.models import SunSpecModelWrapper


def _client():
    """Build a FileClientDevice + scan, return ready-to-use client."""
    client = file_client.FileClientDevice("./tests/test_data/inverter.json")
    client.scan()
    return client


def _wrap(model_id: int) -> SunSpecModelWrapper:
    """Build a SunSpecModelWrapper for the given model_id from the fixture."""
    return SunSpecModelWrapper(_client().models[model_id])


def test_num_models_for_single_instance_model():
    """Model 103 (three-phase inverter) has exactly one instance."""
    wrapper = _wrap(103)
    assert wrapper.num_models == 1


def test_get_keys_filters_invalid_points():
    """isValidPoint excludes points with no value or no unit (unless enum/bitfield).

    Model 103 has many points; the keys returned by getKeys must all
    pass isValidPoint and the list must be sane (more than 5, less than
    50 in a normal inverter).
    """
    wrapper = _wrap(103)
    keys = wrapper.getKeys()
    assert len(keys) > 5
    assert len(keys) < 50
    for key in keys:
        assert wrapper.isValidPoint(key) is True


def test_get_keys_includes_enum16_and_bitfield32():
    """Even points without units must be returned if they are enum16 or bitfield32."""
    wrapper = _wrap(103)
    keys = wrapper.getKeys()
    # St is enum16, Evt1 is bitfield32 - both have no "units" attribute
    # but isValidPoint returns True for these types specifically.
    assert "St" in keys
    assert "Evt1" in keys


def test_get_value_basic_int_point():
    """Model 103 W (Watts) reads as the scaled integer value."""
    wrapper = _wrap(103)
    assert wrapper.getValue("W") == 800


def test_get_value_string_point_from_common_model():
    """Model 1 (Common) string points: Mn (Manufacturer) and SN (Serial)."""
    wrapper = _wrap(1)
    assert wrapper.getValue("Mn") == "SunSpecTest"
    assert wrapper.getValue("SN") == "sn-123456789"


def test_get_value_enum16_returns_raw_int():
    """Enum16 points return the raw integer code, not the symbolic name.

    Decoding to the symbol name (e.g. raw=4 -> "MPPT") happens in
    sensor.py's native_value, not in the wrapper. The wrapper hands the
    integer to the consumer plus the symbol table via getMeta().
    """
    wrapper = _wrap(103)
    assert wrapper.getValue("St") == 4


def test_get_value_bitfield32_returns_raw_int():
    """Bitfield32 points return the raw integer mask.

    Bit-decoding to the symbolic names (Phase 1 sensor.py:222 onwards)
    happens at the sensor layer, not in the wrapper.
    """
    wrapper = _wrap(103)
    assert wrapper.getValue("Evt1") == 3


def test_get_value_with_scale_factor_model_701():
    """Model 701 (DER AC measurements) W=9800 is the SCALED value.

    Model 701 in the fixture has W=980 raw register and a scale factor
    that yields 9800 W. The wrapper transparently returns the cvalue
    (calculated value) so callers never need to know about scale factors.
    """
    wrapper = _wrap(701)
    assert wrapper.getValue("W") == 9800


def test_get_value_repeating_group_member():
    """Model 160 has a 'module' repeating group; access via group:idx:point."""
    wrapper = _wrap(160)
    # The fixture has 2 modules; pull a known DC current value from each.
    # We just assert that both reads return numeric values, exact values
    # are fixture-specific and would couple the test too tightly.
    dca_0 = wrapper.getValue("module:0:DCA")
    dca_1 = wrapper.getValue("module:1:DCA")
    assert isinstance(dca_0, (int, float))
    assert isinstance(dca_1, (int, float))


def test_get_meta_returns_pdef_dict():
    """getMeta(point) returns the pysunspec2 point definition dict."""
    wrapper = _wrap(103)
    meta = wrapper.getMeta("W")
    assert meta["label"] == "Watts"
    assert meta["units"] == "W"
    assert meta["type"] == "int16"


def test_get_meta_for_enum_includes_symbols():
    """Enum16 metadata must expose the symbol table for sensor.py to decode."""
    wrapper = _wrap(103)
    meta = wrapper.getMeta("St")
    assert meta["type"] == "enum16"
    assert "symbols" in meta
    # MPPT is one of the standard SunSpec inverter operating states.
    symbol_names = [s["name"] for s in meta["symbols"]]
    assert "MPPT" in symbol_names


def test_get_group_meta_returns_gdef():
    """getGroupMeta returns the model group definition (name, label, ...)."""
    wrapper = _wrap(103)
    gdef = wrapper.getGroupMeta()
    # pysunspec2 1.3.x renamed model 103's group from "inverter" to
    # "inverter_three_phase" - documented in REWRITE_PLAN.md Phase 1.
    assert gdef["name"] == "inverter_three_phase"
    assert gdef["label"] == "Inverter (Three Phase)"


def test_get_point_top_level():
    """getPoint with a single-segment key returns the top-level point."""
    wrapper = _wrap(103)
    point = wrapper.getPoint("W")
    assert point.pdef["name"] == "W"


def test_get_point_in_repeating_group():
    """getPoint with a 'group:idx:point' key navigates the repeating group."""
    wrapper = _wrap(160)
    point = wrapper.getPoint("module:0:DCA")
    assert point.pdef["name"] == "DCA"
