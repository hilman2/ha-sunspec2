"""Test SunSpec sensor."""

from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.sunspec2.const import CONF_MAX_AC_POWER_KW
from custom_components.sunspec2.sensor import ICON_DC_AMPS

from . import TEST_INVERTER_MM_SENSOR_POWER_ENTITY_ID
from . import TEST_INVERTER_MM_SENSOR_STATE_ENTITY_ID
from . import TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID
from . import TEST_INVERTER_SENSOR_DC_ENTITY_ID
from . import TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID
from . import TEST_INVERTER_SENSOR_POWER_ENTITY_ID
from . import TEST_INVERTER_SENSOR_STATE_ENTITY_ID
from . import TEST_INVERTER_SENSOR_VAR_ID
from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG
from .const import MOCK_CONFIG_MM
from .const import MOCK_CONFIG_PREFIX


async def test_sensor_overflow_error(
    hass: HomeAssistant, sunspec_client_mock, overflow_error_dca
) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_DC_ENTITY_ID)
    assert entity_state


async def test_sensor_dc(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_DC_ENTITY_ID)
    assert entity_state
    assert entity_state.attributes["icon"] == ICON_DC_AMPS


async def test_sensor_var(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_VAR_ID)
    assert entity_state


async def test_sensor_with_prefix(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG_PREFIX)

    entity_state = hass.states.get(TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID)
    assert entity_state


async def test_sensor_state(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_STATE_ENTITY_ID)
    assert entity_state
    assert entity_state.state == "MPPT"


async def test_sensor_power(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_POWER_ENTITY_ID)
    assert entity_state
    assert entity_state.state == "800"


async def test_sensor_energy(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID)
    assert entity_state
    assert entity_state.state == "100000"


async def test_sensor_state_mm(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG_MM)

    entity_state = hass.states.get(TEST_INVERTER_MM_SENSOR_STATE_ENTITY_ID)
    assert entity_state
    assert entity_state.state == "OFF"


async def test_sensor_power_mm(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG_MM)

    entity_state = hass.states.get(TEST_INVERTER_MM_SENSOR_POWER_ENTITY_ID)
    assert entity_state
    assert entity_state.state == "9700"


async def test_sensor_power_filtered_by_peak_limit(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """Power readings above the configured peak should be dropped.

    The mock inverter reports 800 W on the model 103 power sensor. With a
    configured peak of 0.5 kW (= 500 W) the reading is implausible, so the
    sensor's native_value returns None and the entity ends up in
    'unknown' / 'unavailable' state.
    """
    config_entry = create_mock_sunspec_config_entry(
        hass,
        data=MOCK_CONFIG,
        options={CONF_MAX_AC_POWER_KW: 0.5},
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_POWER_ENTITY_ID)
    assert entity_state
    assert entity_state.state in ("unknown", "unavailable")


async def test_sensor_power_passes_through_when_below_limit(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """Power readings below the configured peak should pass through unchanged."""
    config_entry = create_mock_sunspec_config_entry(
        hass,
        data=MOCK_CONFIG,
        options={CONF_MAX_AC_POWER_KW: 10.0},
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_POWER_ENTITY_ID)
    assert entity_state
    assert entity_state.state == "800"


async def test_energy_sensor_updates_across_refreshes(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """Lifetime energy must follow the inverter's WH register across refreshes.

    Reproducer for: 'der sensor fuer die erfassung der erzeugten Wh zeigt
    immer den gleichen Wert an'. We poke ``native_value`` directly with a
    stubbed coordinator value so the inverter appears to climb (100000 ->
    100050 -> 100100) and assert that ``SunSpecEnergySensor.native_value``
    returns the new value each time. If the energy sensor latches onto an
    earlier value (e.g. via the plausibility filter forgetting to update
    ``lastKnown``), the assertion fails.
    """
    from custom_components.sunspec2.sensor import SunSpecEnergySensor

    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = config_entry.runtime_data

    # Locate the WH energy sensor instance bound to model 103.
    energy_sensor = None
    for entity in hass.data["entity_components"]["sensor"].entities:
        if isinstance(entity, SunSpecEnergySensor) and entity.key == "WH":
            energy_sensor = entity
            break
    assert energy_sensor is not None, "WH energy sensor not registered"

    # Establish baseline: the fixture WH is 100000.
    assert energy_sensor.native_value == 100000

    fake_value = {"v": 100050}
    real_get_value = coordinator.data[103].getValue

    def fake_get_value(point_name, model_index=0):
        if point_name == "WH":
            return fake_value["v"]
        return real_get_value(point_name, model_index)

    with patch.object(coordinator.data[103], "getValue", side_effect=fake_get_value):
        # First simulated refresh: WH climbs to 100050.
        assert energy_sensor.native_value == 100050, (
            f"after first WH change expected 100050, got {energy_sensor.native_value!r}"
        )
        # Second simulated refresh: WH climbs to 100100.
        fake_value["v"] = 100100
        assert energy_sensor.native_value == 100100, (
            f"after second WH change expected 100100, got {energy_sensor.native_value!r}"
        )


async def test_energy_sensor_stuck_when_inverter_reports_zero(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """If WH is 0 the sensor returns ``lastKnown`` and never updates it.

    This is the val==0 path in SunSpecEnergySensor.native_value: it is
    designed to avoid resetting the TOTAL_INCREASING counter. But it does
    not refresh ``lastKnown``, so once the inverter starts reporting 0
    (e.g. the WH register is unimplemented and reads as zero), the sensor
    is frozen on its initial value forever, exactly matching the user's
    'zeigt immer den gleichen Wert' symptom.
    """
    from custom_components.sunspec2.sensor import SunSpecEnergySensor

    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = config_entry.runtime_data

    energy_sensor = None
    for entity in hass.data["entity_components"]["sensor"].entities:
        if isinstance(entity, SunSpecEnergySensor) and entity.key == "WH":
            energy_sensor = entity
            break
    assert energy_sensor is not None

    # Baseline: 100000.
    assert energy_sensor.native_value == 100000

    # From now on the inverter always reports 0 for WH.
    real_get_value = coordinator.data[103].getValue

    def zero_get_value(point_name, model_index=0):
        if point_name == "WH":
            return 0
        return real_get_value(point_name, model_index)

    with patch.object(coordinator.data[103], "getValue", side_effect=zero_get_value):
        for _ in range(10):
            assert energy_sensor.native_value == 100000, (
                f"sensor unexpectedly moved to {energy_sensor.native_value!r}"
            )

    # Symptom captured: the sensor stays glued to 100000 across every poll.


async def test_energy_sensor_recovers_after_repeated_rejected_deltas(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """After ENERGY_DELTA_REJECT_RECOVERY_COUNT consecutive rejected reads,
    the energy sensor must accept the new value and resync ``lastKnown``.

    Reproducer for the original bug: a KACO Powador 7.8 TL3 user with
    Spitzen-AC-Leistung=6.9 kW and a 30 s scan interval saw the WH sensor
    glued to the same value because the inverter bumped the lifetime
    counter in coarser steps than the per-cycle plausible delta of 115 Wh.
    Every read was rejected and ``lastKnown`` was never updated.

    The fix counts consecutive rejections and treats N rejections in a row
    as a legitimate counter discontinuity, accepting the new value as the
    new baseline. This test drives the rejection branch repeatedly and
    asserts that the sensor unblocks within the recovery window.
    """
    from custom_components.sunspec2.const import CONF_MAX_AC_POWER_KW
    from custom_components.sunspec2.const import ENERGY_DELTA_REJECT_RECOVERY_COUNT
    from custom_components.sunspec2.sensor import SunSpecEnergySensor

    config_entry = create_mock_sunspec_config_entry(
        hass,
        data=MOCK_CONFIG,
        options={CONF_MAX_AC_POWER_KW: 0.5},
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)
    coordinator = config_entry.runtime_data

    energy_sensor = None
    for entity in hass.data["entity_components"]["sensor"].entities:
        if isinstance(entity, SunSpecEnergySensor) and entity.key == "WH":
            energy_sensor = entity
            break
    assert energy_sensor is not None

    # Climb the inverter WH register by 50 Wh on every read. With a 0.5 kW
    # peak and 10 s scan interval the per-cycle plausible delta is ~2.78 Wh,
    # so each 50 Wh step is over the limit.
    fake_value = {"v": 100050}
    real_get_value = coordinator.data[103].getValue

    def fake_get_value(point_name, model_index=0):
        if point_name == "WH":
            return fake_value["v"]
        return real_get_value(point_name, model_index)

    with patch.object(coordinator.data[103], "getValue", side_effect=fake_get_value):
        readings = []
        # Read once per simulated poll cycle.
        for _ in range(ENERGY_DELTA_REJECT_RECOVERY_COUNT + 2):
            readings.append(energy_sensor.native_value)
            fake_value["v"] += 50

    # The first (RECOVERY_COUNT - 1) readings are rejected and stay glued
    # to the baseline (100000). On the RECOVERY_COUNT-th read we accept,
    # so the value moves. Subsequent reads continue to track the inverter.
    assert readings[0] == 100000
    # Sensor must have unblocked at some point in the window.
    assert any(r != 100000 for r in readings), (
        f"sensor did not recover within {len(readings)} reads: {readings}"
    )
    # And the final reading must reflect the actual inverter value, not
    # the stale baseline.
    assert readings[-1] != 100000, f"sensor still glued to baseline on last read: {readings}"
