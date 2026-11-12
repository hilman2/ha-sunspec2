"""Tests for the Select platform's write controls.

``StorCtl_Mod`` of model 124 ships enabled only on a device without a
vendor profile; the Fronius profile hides it behind its own battery
modes. The device here is the Fronius fixture under another
manufacturer's name: models 1, 160, 123 and 124, no profile.
"""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.sunspec2.const import CONF_ENABLED_MODELS
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.entity import get_sunspec_unique_id
from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.select import SunSpecWriteSelect

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .conftest import MockFileClientDevice
from .const import MOCK_CONFIG_WRITE
from .test_number import _live_entities


@pytest.fixture
def storage_device():
    """A battery inverter no vendor profile claims."""
    client = MockFileClientDevice("./tests/test_data/inverter_fronius.json")
    client.scan()
    client.models[1][0].Mn.value = "SunSpecTest"
    with patch("custom_components.sunspec2.SunSpecApiClient.modbus_connect", return_value=client):
        yield client


async def _setup(hass):
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _storage_select(hass):
    selects = [e for e in _live_entities(hass, "select") if isinstance(e, SunSpecWriteSelect)]
    assert [e._point_name for e in selects] == ["StorCtl_Mod"]
    return selects[0]


async def test_storage_control_mode_is_one_select_with_the_four_combinations(hass, storage_device):
    entry = await _setup(hass)
    assert entry.runtime_data.vendor is None

    select = _storage_select(hass)

    assert select.options == ["off", "charge", "discharge", "both"]


@pytest.mark.parametrize(
    ("raw", "option"), [(0, "off"), (1, "charge"), (2, "discharge"), (3, "both")]
)
async def test_the_select_shows_the_combination_the_bits_stand_for(
    hass, storage_device, raw, option
):
    """Bit 0 gates the charge rate, bit 1 the discharge rate."""
    storage_device.models[124][0].StorCtl_Mod.value = raw
    await _setup(hass)

    select = _storage_select(hass)

    assert select.current_option == option
    state = hass.states.get(select.entity_id)
    assert state is not None
    assert state.state == option


@pytest.mark.parametrize(
    "raw",
    [
        # The register answers "not implemented".
        None,
        # A bit the model does not define.
        4,
    ],
)
async def test_the_select_is_unknown_for_a_value_no_option_stands_for(hass, storage_device, raw):
    entry = await _setup(hass)
    select = _storage_select(hass)
    assert select.current_option == "off"

    storage_device.models[124][0].StorCtl_Mod.value = raw
    await entry.runtime_data.async_refresh()

    assert select.current_option is None
    state = hass.states.get(select.entity_id)
    assert state is not None
    assert state.state == "unknown"


async def test_selecting_an_option_writes_its_raw_value_and_shows_what_the_device_holds(
    hass, storage_device
):
    await _setup(hass)
    select = _storage_select(hass)
    assert select.current_option == "off"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select.entity_id, "option": "both"},
        blocking=True,
    )

    assert storage_device.models[124][0].StorCtl_Mod.value == 3
    state = hass.states.get(select.entity_id)
    assert state is not None
    assert state.state == "both"


async def test_select_option_holds_the_gateway_lock_and_refreshes_after_it(hass, storage_device):
    """Same contract as the Number platform: write under the lock, refresh outside it."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    select = _storage_select(hass)

    events: list[tuple[object, ...]] = []

    async def _spy_write(model_id, points):
        events.append(("write", model_id, points, coordinator._gateway_lock.locked()))

    async def _spy_refresh():
        events.append(("refresh", coordinator._gateway_lock.locked()))

    with (
        patch.object(coordinator.api, "async_write_points", side_effect=_spy_write),
        patch.object(coordinator, "async_request_refresh", side_effect=_spy_refresh),
    ):
        await select.async_select_option("discharge")

    assert events == [("write", 124, [("StorCtl_Mod", 2)], True), ("refresh", False)]
    assert not coordinator._gateway_lock.locked()


async def test_the_select_refuses_an_option_it_does_not_offer(hass, storage_device):
    entry = await _setup(hass)
    select = _storage_select(hass)

    with (
        patch.object(entry.runtime_data.api, "async_write_points") as write,
        pytest.raises(
            HomeAssistantError, match="turbo is not an option of StorCtl_Mod on this device"
        ),
    ):
        await select.async_select_option("turbo")

    write.assert_not_called()


async def test_a_refused_write_reaches_the_user_with_the_point_and_the_value(hass, storage_device):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    select = _storage_select(hass)

    with (
        patch.object(
            coordinator.api, "async_write_points", side_effect=DeviceError("inverter said no")
        ),
        patch.object(coordinator, "async_request_refresh") as refresh,
        pytest.raises(HomeAssistantError, match="Failed to write StorCtl_Mod=1: inverter said no"),
    ):
        await select.async_select_option("charge")

    refresh.assert_not_called()
    assert not coordinator._gateway_lock.locked()


async def test_the_select_reads_nothing_once_the_device_stops_listing_the_model(
    hass, storage_device
):
    """A firmware update can take a model away; the next scan then leaves it out of the data."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    select = _storage_select(hass)
    assert select.current_option == "off"

    del storage_device.models[124]
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert 124 not in coordinator.data
    assert select.current_option is None


async def test_the_setpoint_mode_select_ships_disabled_and_works_once_enabled(hass):
    """``WSetMod`` of model 704 decides whether ``WSet`` or ``WSetPct`` is in force."""
    client = MockFileClientDevice("./tests/test_data/inverter.json")
    client.scan()
    controls = client.models[704][0]
    controls.WSetMod.value = 1
    registry = er.async_get(hass)

    with patch("custom_components.sunspec2.SunSpecApiClient.modbus_connect", return_value=client):
        entry = create_mock_sunspec_config_entry(
            hass, data={**MOCK_CONFIG_WRITE, CONF_ENABLED_MODELS: [160, 704]}
        )
        await setup_mock_sunspec_config_entry(hass, config_entry=entry)
        entity_id = registry.async_get_entity_id(
            "select", DOMAIN, get_sunspec_unique_id(entry.entry_id, "WSetMod", 704, 0)
        )
        assert entity_id is not None
        registered = registry.async_get(entity_id)
        assert registered is not None
        assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert not [e for e in _live_entities(hass, "select") if e._point_name == "WSetMod"]

        registry.async_update_entity(entity_id, disabled_by=None)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "watts"

        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": "w_max_pct"},
            blocking=True,
        )

        assert controls.WSetMod.value == 0
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "w_max_pct"
