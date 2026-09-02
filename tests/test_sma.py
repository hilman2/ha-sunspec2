"""The SMA profile: SunSpec under unit id 126, SMA's own registers under unit id 3.

The entity tests run against tests/test_data/inverter_sma.json plus
the register images in tests/sma_registers.py, with an entry on unit
id 126 so the profile's offset of -123 lands on unit 3.
"""

from unittest.mock import call
from unittest.mock import patch

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.sunspec2 import get_sunspec_unique_id
from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.const import CONF_HOST
from custom_components.sunspec2.const import CONF_PORT
from custom_components.sunspec2.const import CONF_UNIT_ID
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.errors import DeviceError
from custom_components.sunspec2.vendor_blocks import RawBlockNumber
from custom_components.sunspec2.vendor_blocks import RawBlockSelect
from custom_components.sunspec2.vendor_blocks import RawBlockSensor
from custom_components.sunspec2.vendor_blocks import RawKeepAliveSwitch
from custom_components.sunspec2.vendors import profile_for
from custom_components.sunspec2.vendors.sma import SETPOINT_KEEPALIVE_SECONDS
from custom_components.sunspec2.vendors.sma import SMA
from custom_components.sunspec2.vendors.sma import address
from custom_components.sunspec2.vendors.sma import sma_tag

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG

SMA_CONFIG = {**MOCK_CONFIG, CONF_UNIT_ID: 126}


def test_sma_is_matched_and_its_registers_sit_123_units_below():
    assert profile_for("SMA") is SMA
    assert profile_for("Fronius") is not SMA
    blocks = {block.key: block for block in SMA.raw_blocks}
    assert blocks["sma_battery"].unit_id_offset == -123
    assert blocks["sma_battery"].address == address(30843) == 30842
    assert not blocks["sma_setpoint"].readable


def test_a_status_tag_uses_the_low_24_bits_and_has_a_not_available_marker():
    assert sma_tag(307) == 307
    assert sma_tag(0x01000133) == 307
    assert sma_tag(0xFFFFFD) is None
    assert sma_tag(None) is None


async def _entry(hass):
    entry = create_mock_sunspec_config_entry(hass, data=SMA_CONFIG)
    # The setup helper's default client sits on unit id 1; the offset
    # of -123 only lands on unit 3 from 126.
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


async def test_the_battery_and_the_status_come_from_unit_3(hass, sunspec_sma_client_mock):
    entry = await _entry(hass)
    coordinator = entry.runtime_data
    assert coordinator.vendor is SMA
    assert "sma_battery" in coordinator.raw_blocks
    assert "sma_setpoint" not in coordinator.raw_blocks

    sensors = _entities(hass, "sensor", RawBlockSensor)
    assert sensors["battery_state_of_charge"].native_value == 55
    assert sensors["battery_current"].native_value == -2.5
    assert sensors["battery_temperature"].native_value == 25.0
    assert sensors["battery_voltage"].native_value == 51.2
    assert sensors["battery_discharge_power"].native_value == 1200
    assert sensors["battery_energy_charged"].native_value == 123456
    assert sensors["battery_rated_energy"].native_value == 10240
    assert sensors["sma_operating_status"].native_value == "ok"
    assert sensors["sma_active_power_mode"].native_value == "external_setpoint"
    assert sensors["sma_setpoint_timeout"].native_value == 1800
    assert sensors["sma_device_class"].native_value == "pv_inverter"


async def test_the_setpoint_entities_exist_without_a_readable_block(hass, sunspec_sma_client_mock):
    await _entry(hass)
    control = _entities(hass, "select", RawBlockSelect)["sma_power_control"]
    setpoint = _entities(hass, "number", RawBlockNumber)["sma_power_setpoint"]
    keepalive = _entities(hass, "switch", RawKeepAliveSwitch)["sma_power_setpoint_keepalive"]
    assert control.available and setpoint.available and keepalive.available
    # Nothing to read back: unknown until Home Assistant writes.
    assert control.current_option is None
    assert setpoint.native_value is None


async def test_writes_land_on_unit_3_in_sma_word_order_and_are_not_counted(
    hass, sunspec_sma_client_mock
):
    """40151 = 802 then 40149 = -3000: big word order, so 0xFFFF 0xF448."""
    entry = await _entry(hass)
    control = _entities(hass, "select", RawBlockSelect)["sma_power_control"]
    setpoint = _entities(hass, "number", RawBlockNumber)["sma_power_setpoint"]

    await control.async_select_option("active")
    await setpoint.async_set_native_value(-3000)
    await hass.async_block_till_done()

    unit_3 = sunspec_sma_client_mock.unit_registers[3]
    assert unit_3[address(40151)] == 0 and unit_3[address(40151) + 1] == 802
    assert unit_3[address(40149)] == 0xFFFF and unit_3[address(40149) + 1] == 0xF448
    assert control.current_option == "active"
    assert setpoint.native_value == -3000.0
    assert entry.runtime_data.raw_write_count == 0


async def test_the_keepalive_writes_the_flag_then_the_setpoint_while_active(
    hass, sunspec_sma_client_mock, freezer
):
    entry = await _entry(hass)
    control = _entities(hass, "select", RawBlockSelect)["sma_power_control"]
    setpoint = _entities(hass, "number", RawBlockNumber)["sma_power_setpoint"]
    keepalive = _entities(hass, "switch", RawKeepAliveSwitch)["sma_power_setpoint_keepalive"]
    await control.async_select_option("active")
    await setpoint.async_set_native_value(-3000)
    unit_3 = sunspec_sma_client_mock.unit_registers[3]
    unit_3[address(40151) + 1] = 803
    unit_3[address(40149) + 1] = 0

    with patch("custom_components.sunspec2.vendor_blocks.asyncio.sleep") as sleep:
        await keepalive.async_turn_on()
        await hass.async_block_till_done()
    assert unit_3[address(40151) + 1] == 802
    assert unit_3[address(40149) + 1] == 0xF448
    # The patch catches every asyncio.sleep, Home Assistant's own
    # zero-length ones included; the settle pause is the 1.0.
    assert call(1.0) in sleep.await_args_list

    unit_3[address(40149) + 1] = 0
    with patch("custom_components.sunspec2.vendor_blocks.asyncio.sleep"):
        freezer.tick(SETPOINT_KEEPALIVE_SECONDS + 1)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert unit_3[address(40149) + 1] == 0xF448

    # Handed back: the rewrite stays quiet.
    await control.async_select_option("inactive")
    unit_3[address(40149) + 1] = 0
    with patch("custom_components.sunspec2.vendor_blocks.asyncio.sleep"):
        freezer.tick(SETPOINT_KEEPALIVE_SECONDS + 1)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert unit_3[address(40149) + 1] == 0
    assert entry.runtime_data.raw_write_count == 0


async def test_an_inverter_without_a_battery_gets_no_battery_sensors(hass, sunspec_sma_client_mock):
    registers = sunspec_sma_client_mock.unit_registers[3]
    for offset in range(10):
        registers.pop(address(30843) + offset, None)
    entry = await _entry(hass)
    assert "sma_battery" not in entry.runtime_data.raw_blocks
    assert "sma_battery_power" not in entry.runtime_data.raw_blocks
    sensors = _entities(hass, "sensor", RawBlockSensor)
    assert "battery_state_of_charge" not in sensors
    assert sensors["sma_operating_status"].native_value == "ok"


async def test_dhcp_discovery_of_an_sma_offers_unit_id_126(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "dhcp"},
        data=DhcpServiceInfo(
            ip="192.168.1.20", hostname="sma3012345678", macaddress="0015bb001122"
        ),
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual"
    defaults = {key.schema: key.default() for key in result["data_schema"].schema}
    assert defaults[CONF_UNIT_ID] == 126
    assert defaults[CONF_HOST] == "192.168.1.20"


async def test_the_manual_flow_points_a_wrong_unit_id_at_126(hass, sunspec_sma_client_mock):
    """Unit id 1 answers a Modbus error, 126 answers SunSpec: the form says so and is corrected."""
    from custom_components.sunspec2.models import SunSpecModelWrapper

    device_info = SunSpecModelWrapper(sunspec_sma_client_mock.models[1])

    async def probe(self):
        if self._unit_id != 126:
            raise DeviceError("Modbus exception 2")
        return device_info

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "manual"})
    with patch.object(SunSpecApiClient, "async_get_device_info", autospec=True, side_effect=probe):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_HOST: "192.168.1.20", CONF_PORT: 502, CONF_UNIT_ID: 1},
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "sma_unit_id"}
    defaults = {key.schema: key.default() for key in result["data_schema"].schema}
    assert defaults[CONF_UNIT_ID] == 126


async def test_unique_ids_of_the_sma_entities_carry_the_block(hass, sunspec_sma_client_mock):
    entry = await _entry(hass)
    sensors = _entities(hass, "sensor", RawBlockSensor)
    assert sensors["battery_state_of_charge"].unique_id == get_sunspec_unique_id(
        entry.entry_id, "raw:sma_battery:state_of_charge", 0, 0
    )
