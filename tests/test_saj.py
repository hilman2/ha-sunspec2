"""The SAJ profile: the energy counters and the MPPT inputs from SAJ's own registers on an R6."""

import pytest
from homeassistant.components.sensor import SensorStateClass

from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.vendor_blocks import RawBlockSensor
from custom_components.sunspec2.vendors import profile_for
from custom_components.sunspec2.vendors.saj import SAJ

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG
from .saj_registers import PV_ADDRESS


@pytest.mark.parametrize(
    "model",
    [
        "R6-3K-T2",
        "R6-4K-T2",
        "R6-5K-T2",
        "R6-6K-T2",
        "R6-8K-T2",
        "R6-10K-T2",
        "R6-12K-T2",
        "R6-15K-T2",
        "R6-15K-T2-32",
        "R6-17K-T2-32",
        "R6-20K-T2-32",
        "R6-22K-T2-32",
        "R6-25K-T2-32",
        "R6-25K-T2-32-B",
        "R6-10K-T2-AUS",
    ],
)
def test_saj_matches_the_two_mppt_r6_family(model):
    assert profile_for("SAJ", model, "R6", "6.199") is SAJ


def test_saj_matching_normalizes_names_and_checks_the_manufacturer():
    assert profile_for(" SAJ ", " r6-15k-t2-32 ") is SAJ
    assert profile_for("Fronius", "R6-15K-T2-32") is not SAJ


@pytest.mark.parametrize(
    "model",
    [
        "R6-25K-T3-32-B",
        "R6-50K-T4-32",
        "R6-5K-S2",
        "R6-15K-T20",
        "R60-15K-T2",
        "R6-unknown-T2",
        "H2-10K-T2",
        "R6",
        "",
    ],
)
def test_unverified_models_stay_generic_even_with_an_r6_option(model):
    assert profile_for("SAJ", model, "R6", "6.199") is None


def test_the_blocks_sit_where_the_r6_keeps_them():
    blocks = {block.key: block for block in SAJ.raw_blocks}
    assert (blocks["saj_energy"].address, blocks["saj_energy"].count) == (0x6004, 8)
    assert (blocks["saj_pv"].address, blocks["saj_pv"].count) == (0x5194, 9)
    offsets = {field.name: field.offset for field in blocks["saj_pv"].fields}
    # Total, then voltage, current, a spare register and power per input.
    assert offsets == {
        "dcw": 0,
        "dcv1": 1,
        "dca1": 2,
        "dcw1": 4,
        "dcv2": 5,
        "dca2": 6,
        "dcw2": 8,
    }


async def _entry(hass):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    client = SunSpecApiClient(host="test", port=123, unit_id=1, hass=hass)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)
    return entry


def _sensors(hass):
    component = hass.data["entity_components"]["sensor"]
    return {e.translation_key: e for e in component.entities if isinstance(e, RawBlockSensor)}


async def test_the_counters_and_the_inputs_of_an_r6_become_sensors(hass, sunspec_saj_client_mock):
    entry = await _entry(hass)

    assert entry.runtime_data.vendor is SAJ
    sensors = _sensors(hass)

    assert round(sensors["saj_energy_today"].native_value, 2) == 0.40
    assert round(sensors["saj_energy_month"].native_value, 2) == 330.72
    assert sensors["saj_energy_today"].state_class == SensorStateClass.TOTAL_INCREASING

    assert round(sensors["saj_dc_voltage_1"].native_value, 1) == 186.2
    assert round(sensors["saj_dc_current_1"].native_value, 2) == 2.32
    assert sensors["saj_dc_power_1"].native_value == 433
    assert round(sensors["saj_dc_voltage_2"].native_value, 1) == 695.2
    assert round(sensors["saj_dc_current_2"].native_value, 2) == 0.29
    assert sensors["saj_dc_power_2"].native_value == 214

    blocks = entry.runtime_data.raw_blocks
    assert blocks["saj_pv"]["dcw"] == blocks["saj_pv"]["dcw1"] + blocks["saj_pv"]["dcw2"]
    assert round(blocks["saj_energy"]["total"], 2) == 12391.13


async def test_a_module_that_does_not_serve_the_dc_block_keeps_the_counters(
    hass, sunspec_saj_client_mock
):
    del sunspec_saj_client_mock.registers[PV_ADDRESS]
    entry = await _entry(hass)

    assert "saj_pv" not in entry.runtime_data.raw_blocks
    assert "saj_energy" in entry.runtime_data.raw_blocks
