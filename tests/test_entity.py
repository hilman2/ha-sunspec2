"""The device a SunSpec model becomes, and what an entity on it reports before there is data.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

import pytest

from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.coordinator import SunSpecDataUpdateCoordinator
from custom_components.sunspec2.entity import SunSpecEntity
from custom_components.sunspec2.entity import device_info_for
from custom_components.sunspec2.entity import home_for
from custom_components.sunspec2.models import SunSpecModelWrapper

from . import TEST_CONFIG_ENTRY_ID
from . import create_mock_sunspec_client
from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .file_client import FileClientDevice

INVERTER_GROUP = {"name": "inverter_three_phase", "label": "Inverter (Three Phase)"}


def _common(model_string: str | None = "Test-1547-1") -> SunSpecModelWrapper:
    """Common model 1 of the fixture inverter, with ``Md`` as given."""
    device = FileClientDevice("./tests/test_data/inverter.json")
    device.scan()
    device.models[1][0].points["Md"].set_value(model_string)
    return SunSpecModelWrapper(device.models[1])


def _coordinator_before_its_first_cycle(hass) -> SunSpecDataUpdateCoordinator:
    entry = create_mock_sunspec_config_entry(hass)
    return SunSpecDataUpdateCoordinator(hass, client=create_mock_sunspec_client(hass), entry=entry)


async def test_an_entity_is_unavailable_until_the_coordinator_has_data(hass):
    coordinator = _coordinator_before_its_first_cycle(hass)
    common = _common()
    entity = SunSpecEntity(coordinator, coordinator.entry, common, INVERTER_GROUP, model_id=103)

    assert coordinator.data is None
    assert entity.available is False

    coordinator.data = {1: common}
    assert entity.available is True


async def test_there_is_no_home_before_the_first_cycle(hass):
    coordinator = _coordinator_before_its_first_cycle(hass)

    assert home_for(coordinator, (103,)) is None


async def test_the_home_is_the_first_preferred_model_the_device_has(hass, sunspec_client_mock):
    entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = entry.runtime_data

    home = home_for(coordinator, (802, 160, 103))

    assert home is not None
    assert home.model_id == 160
    assert home.model_info["name"] == "mppt"
    assert home.device_info is coordinator.device_info


async def test_without_a_preferred_model_the_home_is_the_lowest_model_but_common(
    hass, sunspec_client_mock
):
    entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = entry.runtime_data
    assert coordinator.device_info is not None
    coordinator.data = {**coordinator.data, 1: coordinator.device_info}

    home = home_for(coordinator, (802,))

    assert home is not None
    assert home.model_id == 103


async def test_there_is_no_home_on_a_device_polled_for_the_common_model_alone(
    hass, sunspec_client_mock
):
    """Model 1 names the device and is every model's device info, not a device of its own."""
    entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = entry.runtime_data
    assert coordinator.device_info is not None
    coordinator.data = {1: coordinator.device_info}

    assert home_for(coordinator, (103, 160)) is None


@pytest.mark.parametrize(
    ("prefix", "model_string", "name"),
    [
        ("", "Test-1547-1", "Test-1547-1 Inverter (Three Phase)"),
        ("Garage", "Test-1547-1", "Garage Inverter (Three Phase)"),
        # An inverter that leaves Md blank is named after its SunSpec model alone.
        ("", None, "Inverter (Three Phase)"),
    ],
)
def test_the_device_name_is_the_prefix_or_model_string_and_the_sunspec_model(
    hass, prefix, model_string, name
):
    entry = create_mock_sunspec_config_entry(hass)

    info = device_info_for(entry, _common(model_string), INVERTER_GROUP, prefix, 103)

    assert info["name"] == name
    assert info["model"] == model_string
    assert info["model_id"] == "SunSpec 103"
    assert info["manufacturer"] == "SunSpecTest"
    # One device per SunSpec model: the registry of every installation holds this triple.
    assert [tuple(identifier) for identifier in info["identifiers"]] == [
        (DOMAIN, TEST_CONFIG_ENTRY_ID, "inverter_three_phase")
    ]
