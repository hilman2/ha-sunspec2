"""The entities over a vendor's own registers when the device refuses, goes quiet or restarts.

tests/test_kostal.py, tests/test_sma.py and tests/test_solaredge.py have
the working path of each profile. These use two of the same devices for
what is common to all of them: the Kostal PLENTICORE, whose blocks are
read back, and the SMA Tripower, whose setpoint block is write-only.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

from unittest.mock import patch

import pytest
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.const import CONF_ENABLED_MODELS
from custom_components.sunspec2.const import CONF_UNIT_ID
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.entity import get_sunspec_unique_id
from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.raw_blocks import RETRY_CYCLES
from custom_components.sunspec2.raw_blocks import decode_field
from custom_components.sunspec2.vendor_blocks import RawBlockNumber
from custom_components.sunspec2.vendor_blocks import RawBlockSelect
from custom_components.sunspec2.vendor_blocks import RawBlockSensor
from custom_components.sunspec2.vendor_blocks import RawKeepAliveSwitch
from custom_components.sunspec2.vendors.kostal import KOSTAL
from custom_components.sunspec2.vendors.kostal import LIMITATION_REASSERT_SECONDS
from custom_components.sunspec2.vendors.profile import RawField
from custom_components.sunspec2.vendors.sma import address

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG
from .kostal_registers import at
from .kostal_registers import f32
from .kostal_registers import registers_written

#: 1280 to 1289, Kostal's battery limitation block.
LIMITATION_REGISTERS = range(1280, 1290)


async def _kostal(hass, **data):
    entry = create_mock_sunspec_config_entry(hass, data={**MOCK_CONFIG, **data})
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    return entry


async def _sma(hass):
    entry = create_mock_sunspec_config_entry(hass, data={**MOCK_CONFIG, CONF_UNIT_ID: 126})
    # SMA's own registers sit 123 unit ids below the SunSpec ones, on unit 3.
    client = SunSpecApiClient(host="test", port=123, unit_id=126, hass=hass)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry, client=client)
    return entry


def _entities(hass, platform, cls):
    component = hass.data.get("entity_components", {}).get(platform)
    return (
        {e.translation_key: e for e in component.entities if isinstance(e, cls)}
        if component
        else {}
    )


def _as_watts(written):
    """A float32 register pair as Kostal orders it, low word first."""
    return decode_field(RawField("watts", 0, "float32"), written, "little")


# ---------- reading -----------------------------------------------------------


async def test_a_reading_the_inverter_marks_as_not_available_is_unknown(
    hass, sunspec_kostal_client_mock
):
    """NaN in a float register is how a raw block says "not implemented"."""
    entry = await _kostal(hass)
    temperature = _entities(hass, "sensor", RawBlockSensor)["battery_temperature"]
    assert temperature.native_value == 21.5

    sunspec_kostal_client_mock.registers.update(at(214, f32(float("nan"))))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert temperature.native_value is None
    assert hass.states.get(temperature.entity_id).state == "unknown"


async def test_an_entry_that_polls_only_the_common_model_gets_no_vendor_entities(
    hass, sunspec_kostal_client_mock, caplog
):
    """There is no model device to put them on, and the blocks are read all the same."""
    entry = await _kostal(hass, **{CONF_ENABLED_MODELS: [1]})
    assert entry.runtime_data.vendor is KOSTAL
    assert "inverter" in entry.runtime_data.raw_blocks
    assert _entities(hass, "sensor", RawBlockSensor) == {}
    assert _entities(hass, "number", RawBlockNumber) == {}
    assert _entities(hass, "switch", RawKeepAliveSwitch) == {}
    # A platform that fails to set up has no entities either, and says so in the log.
    assert [record.getMessage() for record in caplog.records if record.exc_info] == []


# ---------- writing -----------------------------------------------------------


async def test_a_write_the_device_refuses_names_the_register_and_the_value(
    hass, sunspec_kostal_client_mock
):
    entry = await _kostal(hass)
    number = _entities(hass, "number", RawBlockNumber)["battery_min_soc"]
    # The test device answers a Modbus exception for a register it does not hold.
    del sunspec_kostal_client_mock.registers[1042]

    with pytest.raises(HomeAssistantError) as refused:
        await number.async_set_native_value(20.0)

    assert refused.value.translation_key == "write_failed"
    assert refused.value.translation_placeholders == {
        "target": "battery_control.min_soc=20.0",
        "error": "Modbus exception writing register 1042: Modbus exception 2",
    }
    assert entry.runtime_data.raw_write_count == 0


async def test_an_option_the_select_does_not_offer_is_refused_before_anything_is_written(
    hass, sunspec_sma_client_mock
):
    await _sma(hass)
    control = _entities(hass, "select", RawBlockSelect)["sma_power_control"]

    with pytest.raises(HomeAssistantError) as refused:
        await control.async_select_option("full_power")

    assert refused.value.translation_key == "invalid_option"
    assert refused.value.translation_placeholders == {
        "option": "full_power",
        "target": "sma_setpoint.control",
    }
    assert address(40151) not in sunspec_sma_client_mock.unit_registers[3]
    assert control.current_option is None


# ---------- the timed rewrite ---------------------------------------------------


async def test_a_rewrite_that_was_on_before_a_restart_is_on_after_it(
    hass, sunspec_kostal_client_mock, freezer
):
    """The inverter falls back to its own limits whether or not Home Assistant restarted."""
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG)
    registered = er.async_get(hass).async_get_or_create(
        "switch",
        DOMAIN,
        get_sunspec_unique_id(
            entry.entry_id, "raw:battery_limitation:battery_limitation_hold", 0, 0
        ),
        config_entry=entry,
        suggested_object_id="battery_limit_hold",
    )
    mock_restore_cache(hass, [State(registered.entity_id, "on")])
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)

    switch = _entities(hass, "switch", RawKeepAliveSwitch)["battery_limitation_hold"]
    assert switch.is_on
    assert hass.states.get(registered.entity_id).state == "on"

    with patch.object(entry.runtime_data.api, "async_write_block") as write:
        freezer.tick(LIMITATION_REASSERT_SECONDS + 1)
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    # Nothing was written by Home Assistant yet, so the device's own
    # 4000 W go back into both registers.
    assert [(call.args[0], _as_watts(call.args[1])) for call in write.call_args_list] == [
        (1280, 4000.0),
        (1282, 4000.0),
    ]


async def test_a_block_that_stopped_answering_is_not_rewritten(
    hass, sunspec_kostal_client_mock, freezer
):
    """What Home Assistant last wrote is no reason to write into registers the device took away."""
    entry = await _kostal(hass)
    switch = _entities(hass, "switch", RawKeepAliveSwitch)["battery_limitation_hold"]
    number = _entities(hass, "number", RawBlockNumber)["battery_limitation_charge_power"]
    await number.async_set_native_value(2500.0)
    await switch.async_turn_on()
    await hass.async_block_till_done()
    assert _as_watts(registers_written(sunspec_kostal_client_mock, 1280, 2)) == 2500.0

    for register in LIMITATION_REGISTERS:
        del sunspec_kostal_client_mock.registers[register]
    # A block that answered before is given a few cycles to come back.
    for _ in range(RETRY_CYCLES):
        await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert "battery_limitation" not in entry.runtime_data.raw_blocks

    with patch.object(entry.runtime_data.api, "async_write_block") as write:
        freezer.tick(LIMITATION_REASSERT_SECONDS + 1)
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert switch.is_on
    write.assert_not_called()


async def test_a_setpoint_never_written_is_left_out_of_the_rewrite(hass, sunspec_sma_client_mock):
    """SMA's setpoint block reads back nothing, so only what was written can be written again."""
    await _sma(hass)
    control = _entities(hass, "select", RawBlockSelect)["sma_power_control"]
    keepalive = _entities(hass, "switch", RawKeepAliveSwitch)["sma_power_setpoint_keepalive"]
    await control.async_select_option("active")
    unit_3 = sunspec_sma_client_mock.unit_registers[3]
    unit_3[address(40151) + 1] = 803

    with patch("custom_components.sunspec2.vendor_blocks.asyncio.sleep"):
        await keepalive.async_turn_on()
        await hass.async_block_till_done()

    assert unit_3[address(40151) + 1] == 802
    assert address(40149) not in unit_3


async def test_a_rewrite_the_device_refuses_is_logged_and_stops_there(
    hass, sunspec_kostal_client_mock, caplog
):
    """The second register is not written after the first was refused; the next round tries again."""
    entry = await _kostal(hass)
    switch = _entities(hass, "switch", RawKeepAliveSwitch)["battery_limitation_hold"]

    with patch.object(
        entry.runtime_data.api, "async_write_block", side_effect=DeviceError("Modbus exception 4")
    ) as write:
        await switch.async_turn_on()
        await hass.async_block_till_done()

    assert (
        "Rewrite of battery_limitation.max_charge_power failed: Modbus exception 4" in caplog.text
    )
    assert [call.args[0] for call in write.call_args_list] == [1280]
    assert switch.is_on
