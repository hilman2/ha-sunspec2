"""Tests for the curated write-control specs (#17, #32)."""

from __future__ import annotations

import pytest

from custom_components.sunspec2.select import storage_bits_to_int
from custom_components.sunspec2.write_controls import DER_AC_CONTROLS_MODEL
from custom_components.sunspec2.write_controls import IMMEDIATE_CONTROLS_MODEL
from custom_components.sunspec2.write_controls import PLATFORM_NUMBER
from custom_components.sunspec2.write_controls import PLATFORM_SELECT
from custom_components.sunspec2.write_controls import PLATFORM_SWITCH
from custom_components.sunspec2.write_controls import STORAGE_CONTROL_MODEL
from custom_components.sunspec2.write_controls import active_specs
from custom_components.sunspec2.write_controls import active_specs_for_platform
from custom_components.sunspec2.write_controls import export_limit_points
from custom_components.sunspec2.write_controls import specs_for_model


def _models(specs):
    return {spec.model_id for spec in specs}


def _points(specs):
    return {spec.point_name for spec in specs}


# ---------- model selection -------------------------------------------------


def test_only_123_present_uses_123():
    specs = active_specs({1, 103, IMMEDIATE_CONTROLS_MODEL})

    assert _models(specs) == {IMMEDIATE_CONTROLS_MODEL}


def test_only_704_present_uses_704():
    specs = active_specs({1, 103, DER_AC_CONTROLS_MODEL})

    assert _models(specs) == {DER_AC_CONTROLS_MODEL}


def test_both_present_prefers_704_and_drops_123():
    """A device with both must not get two export-limit entities.

    @tisoft's KACO exposes both, and they do not agree: 704 reports a
    lapsed limit honestly while 123 keeps showing it as active. Two
    controls for one physical setting would be confusing even if they
    did agree.
    """
    specs = active_specs({1, 103, IMMEDIATE_CONTROLS_MODEL, DER_AC_CONTROLS_MODEL})

    assert _models(specs) == {DER_AC_CONTROLS_MODEL}


def test_storage_is_added_alongside_either_control_model():
    """124 is orthogonal: capping export and steering a battery are different jobs."""
    with_123 = active_specs({IMMEDIATE_CONTROLS_MODEL, STORAGE_CONTROL_MODEL})
    with_704 = active_specs({DER_AC_CONTROLS_MODEL, STORAGE_CONTROL_MODEL})

    assert _models(with_123) == {IMMEDIATE_CONTROLS_MODEL, STORAGE_CONTROL_MODEL}
    assert _models(with_704) == {DER_AC_CONTROLS_MODEL, STORAGE_CONTROL_MODEL}


def test_storage_alone_is_enough():
    """A battery-only device still gets its controls."""
    specs = active_specs({1, 802, STORAGE_CONTROL_MODEL})

    assert _models(specs) == {STORAGE_CONTROL_MODEL}


def test_no_control_models_yields_nothing():
    assert active_specs({1, 103, 160}) == []


# ---------- what the grid-protection models must never get ------------------


@pytest.mark.parametrize("model_id", [121, 703, 705, 706, 707, 708, 709, 710, 126, 132])
def test_protection_and_curve_models_have_no_specs(model_id):
    """707..710 are the trip curves, 121 holds VMax/VMin and WMax.

    SunSpec marks all of them RW. Writing there does not misconfigure an
    inverter, it disables grid protection or silently redefines the
    reference every percentage is measured against.
    """
    assert specs_for_model(model_id) == ()
    assert active_specs({model_id}) == []


# ---------- platform routing ------------------------------------------------


def test_export_limit_is_a_number_and_its_enable_a_switch():
    number_points = _points(active_specs_for_platform({IMMEDIATE_CONTROLS_MODEL}, PLATFORM_NUMBER))
    switch_points = _points(active_specs_for_platform({IMMEDIATE_CONTROLS_MODEL}, PLATFORM_SWITCH))

    assert "WMaxLimPct" in number_points
    assert "WMaxLimPct_RvrtTms" in number_points
    assert "WMaxLim_Ena" in switch_points


def test_storage_control_mode_is_a_single_select_not_two_switches():
    """StorCtl_Mod is a bitfield16 and must stay one entity.

    Two switches would each compute their read-modify-write base from
    coordinator.data, which is at most one scan interval old, so
    flipping both in one automation would send two writes from the same
    stale base and the second would clobber the first.
    """
    selects = active_specs_for_platform({STORAGE_CONTROL_MODEL}, PLATFORM_SELECT)
    switches = active_specs_for_platform({STORAGE_CONTROL_MODEL}, PLATFORM_SWITCH)

    mode = [spec for spec in selects if spec.point_name == "StorCtl_Mod"]
    assert len(mode) == 1
    assert set(mode[0].options) == {"off", "charge", "discharge", "both"}
    assert mode[0].options["both"] == 3
    # HA rejects a translation key that is not [a-z0-9-_]+, and hassfest
    # catches it only in CI, so assert it where it is cheap to see.
    assert all(key.islower() for key in mode[0].options)
    assert switches == []


def test_battery_rate_points_are_numbers():
    """The pair an automation actually moves (#32)."""
    numbers = _points(active_specs_for_platform({STORAGE_CONTROL_MODEL}, PLATFORM_NUMBER))

    assert {"InWRte", "OutWRte", "WChaMax"} <= numbers


# ---------- service action routing ------------------------------------------


def test_export_limit_points_follow_the_same_preference():
    """The service action must not write 123 while the entities show 704."""
    assert export_limit_points({IMMEDIATE_CONTROLS_MODEL}) == (123, "WMaxLimPct", "WMaxLim_Ena")
    assert export_limit_points({DER_AC_CONTROLS_MODEL}) == (704, "WMaxLimPct", "WMaxLimPctEna")
    assert export_limit_points({IMMEDIATE_CONTROLS_MODEL, DER_AC_CONTROLS_MODEL}) == (
        704,
        "WMaxLimPct",
        "WMaxLimPctEna",
    )
    assert export_limit_points({1, 103}) is None


# ---------- spec hygiene ----------------------------------------------------


def test_every_spec_has_a_translation_key_and_icon():
    """A control with no name is a row of raw point names in the UI."""
    for model_id in (123, 124, 704):
        for spec in specs_for_model(model_id):
            assert spec.translation_key, spec.point_name
            assert spec.icon.startswith("mdi:"), spec.point_name


def test_selects_declare_options_and_others_do_not():
    for model_id in (123, 124, 704):
        for spec in specs_for_model(model_id):
            if spec.platform == PLATFORM_SELECT:
                assert len(spec.options) >= 2, spec.point_name
            else:
                assert spec.options == {}, spec.point_name


def test_daily_use_controls_are_enabled_by_default():
    """The reason someone turns the beta on should not need a second click."""
    enabled = {
        spec.point_name
        for spec in active_specs({DER_AC_CONTROLS_MODEL, STORAGE_CONTROL_MODEL})
        if spec.enabled_by_default
    }

    assert {"WMaxLimPct", "WMaxLimPctEna", "InWRte", "OutWRte", "StorCtl_Mod"} <= enabled


# ---------- StorCtl_Mod bitfield decoding -----------------------------------


@pytest.mark.parametrize(
    ("symbols", "expected"),
    [
        ([], 0),
        (["CHARGE"], 1),
        # model_124.json's own spelling of the second symbol.
        (["DiSCHARGE"], 2),
        (["DISCHARGE"], 2),
        (["CHARGE", "DiSCHARGE"], 3),
        # Prefix matching must not let DISCHARGE fall into the charge
        # branch, which a naive "CHA" in name test would do.
        (["DISCHARGE", "CHARGE"], 3),
        (["something_vendor_specific"], 0),
    ],
)
def test_storage_bits_to_int(symbols, expected):
    assert storage_bits_to_int(symbols) == expected


def test_select_option_keys_are_valid_ha_translation_keys():
    """hassfest rejects anything outside [a-z0-9-_], and only in CI."""
    import re

    for model_id in (123, 124, 704):
        for spec in specs_for_model(model_id):
            for option in spec.options:
                assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*[a-z0-9]", option), (
                    spec.point_name,
                    option,
                )
