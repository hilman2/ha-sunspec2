"""Test SunSpec sensor."""

from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import PERCENTAGE
from homeassistant.const import UnitOfApparentPower
from homeassistant.const import UnitOfElectricPotential
from homeassistant.const import UnitOfPower
from homeassistant.const import UnitOfReactiveEnergy
from homeassistant.const import UnitOfReactivePower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.sunspec2.const import CONF_MAX_AC_POWER_KW
from custom_components.sunspec2.const import DOMAIN
from custom_components.sunspec2.const import IMPLAUSIBLE_LOG_EVERY
from custom_components.sunspec2.const import measured_power_headroom
from custom_components.sunspec2.sensor import HA_META
from custom_components.sunspec2.sensor import ICON_DC_AMPS
from custom_components.sunspec2.sensor import _power_limit_in_native_unit

from . import TEST_CONFIG_ENTRY_ID
from . import TEST_INVERTER_MM_SENSOR_POWER_ENTITY_ID
from . import TEST_INVERTER_MM_SENSOR_STATE_ENTITY_ID
from . import TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID
from . import TEST_INVERTER_SENSOR_DC_ENTITY_ID
from . import TEST_INVERTER_SENSOR_DC_POWER_ENTITY_ID
from . import TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID
from . import TEST_INVERTER_SENSOR_EVENT_ENTITY_ID
from . import TEST_INVERTER_SENSOR_POWER_ENTITY_ID
from . import TEST_INVERTER_SENSOR_STATE_ENTITY_ID
from . import TEST_INVERTER_SENSOR_VA_ENTITY_ID
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


async def test_device_names_carry_model_suffix(hass: HomeAssistant, sunspec_client_mock) -> None:
    """One device per SunSpec model must be distinguishable (issue #33).

    Before v0.15.0 every device of an entry was named after the Md
    field alone, so a user with many enabled models saw a wall of
    identical names in the device list. The device name now appends
    the model's block label (curated short form for the unwieldy
    ones, e.g. model 160), and the numeric id lands in the registry's
    ``model_id`` field so the device-info card shows what the device
    actually is.
    """
    await setup_mock_sunspec_config_entry(hass)

    device_registry = dr.async_get(hass)
    inverter = device_registry.async_get_device(
        identifiers={(DOMAIN, TEST_CONFIG_ENTRY_ID, "inverter_three_phase")}
    )
    assert inverter is not None
    assert inverter.name == "Test-1547-1 Inverter (Three Phase)"
    assert inverter.model == "Test-1547-1"
    assert inverter.model_id == "SunSpec 103"

    mppt = device_registry.async_get_device(identifiers={(DOMAIN, TEST_CONFIG_ENTRY_ID, "mppt")})
    assert mppt is not None
    assert mppt.name == "Test-1547-1 MPPT"
    assert mppt.model_id == "SunSpec 160"


async def test_device_name_with_prefix_keeps_model_suffix(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """A user prefix replaces the Md base but never the model suffix.

    The prefix exists to tell two inverters apart, the suffix exists
    to tell two models of one inverter apart - dropping either brings
    back an ambiguity.
    """
    await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG_PREFIX)

    device_registry = dr.async_get(hass)
    mppt = device_registry.async_get_device(identifiers={(DOMAIN, TEST_CONFIG_ENTRY_ID, "mppt")})
    assert mppt is not None
    assert mppt.name == "test MPPT"


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


async def test_energy_counter_that_moves_in_steps_is_not_rejected(
    hass: HomeAssistant, sunspec_client_mock, freezer, caplog
) -> None:
    """#45 follow-up: a counter that stands still and then catches up is right.

    A Fronius GEN24 updates WH every few minutes. At a 30 s poll that is
    the same value for several reads and then one jump by the whole
    amount, which the filter used to measure against one scan interval
    and reject, three polls in a row, on every single step. The window
    is the time since the counter last moved.
    """
    from custom_components.sunspec2.sensor import SunSpecEnergySensor

    config_entry = create_mock_sunspec_config_entry(
        hass, data=MOCK_CONFIG, options={CONF_MAX_AC_POWER_KW: 0.5}
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)
    coordinator = config_entry.runtime_data

    energy_sensor = None
    for entity in hass.data["entity_components"]["sensor"].entities:
        if isinstance(entity, SunSpecEnergySensor) and entity.key == "WH":
            energy_sensor = entity
            break
    assert energy_sensor is not None
    assert energy_sensor.native_value == 100000

    fake_value = {"v": 100000}
    real_get_value = coordinator.data[103].getValue

    def fake_get_value(point_name, model_index=0):
        if point_name == "WH":
            return fake_value["v"]
        return real_get_value(point_name, model_index)

    # 0.5 kW peak x 2 = 1 kW: 50 Wh is three minutes' worth. One scan
    # interval (10 s) allows 2.78 Wh, so this is the jump the old filter
    # rejected.
    with patch.object(coordinator.data[103], "getValue", side_effect=fake_get_value):
        # The counter stands still for three minutes of polls.
        for _ in range(18):
            freezer.tick(10)
            assert energy_sensor.native_value == 100000
        # Then it catches up in one step.
        freezer.tick(10)
        fake_value["v"] = 100050
        assert energy_sensor.native_value == 100050
        assert "Dropping implausible energy delta" not in caplog.text

        # The same step straight away, with no time for it, is still
        # garbage.
        fake_value["v"] = 100100
        assert energy_sensor.native_value == 100050
        assert "Dropping implausible energy delta for WH" in caplog.text


@pytest.mark.parametrize(
    ("stored_age_seconds", "accepted"),
    [
        # Home Assistant was down for an hour: 200 Wh at 1 kW is fine.
        (3600, True),
        # The stored value is brand new: 200 Wh in one poll is not.
        (0, False),
    ],
)
async def test_restored_energy_baseline_brings_its_age(
    hass: HomeAssistant,
    sunspec_client_mock,
    freezer,
    caplog,
    stored_age_seconds,
    accepted,
) -> None:
    """The first read after a restart is measured against the downtime.

    RestoreSensor hands back the value and, through the state, when it
    was last written. Measuring the catch-up jump against one scan
    interval made the first read after any restart longer than a few
    minutes a guaranteed rejection.
    """
    from datetime import timedelta

    from homeassistant.core import State
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

    from custom_components.sunspec2.sensor import SunSpecEnergySensor

    written = dt_util.utcnow() - timedelta(seconds=stored_age_seconds)
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(
                    TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID,
                    "99800",
                    last_changed=written,
                    last_updated=written,
                ),
                {"native_value": 99800, "native_unit_of_measurement": "Wh"},
            ),
        ),
    )

    config_entry = create_mock_sunspec_config_entry(
        hass, data=MOCK_CONFIG, options={CONF_MAX_AC_POWER_KW: 0.5}
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)

    energy_sensor = None
    for entity in hass.data["entity_components"]["sensor"].entities:
        if isinstance(entity, SunSpecEnergySensor) and entity.key == "WH":
            energy_sensor = entity
            break
    assert energy_sensor is not None

    # The fixture reports 100000, 200 Wh above the restored baseline.
    state = hass.states.get(TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID)
    assert state is not None
    if accepted:
        assert state.state == "100000"
        assert "Dropping implausible energy delta" not in caplog.text
    else:
        assert state.state == "99800"
        assert "Dropping implausible energy delta for WH" in caplog.text


# ---------- unit mapping (#17) ----------------------------------------------


@pytest.mark.parametrize(
    ("sunspec_unit", "expected_unit", "expected_device_class"),
    [
        # Percentages of a reference quantity: the reference belongs in
        # the name, HA has exactly one percent.
        ("% WMax", PERCENTAGE, None),
        ("% VArMax", PERCENTAGE, None),
        ("% VRef", PERCENTAGE, None),
        ("Pct", PERCENTAGE, None),
        # Reactive power, all four spellings SunSpec uses.
        ("VAr", UnitOfReactivePower.VOLT_AMPERE_REACTIVE, None),
        ("var", UnitOfReactivePower.VOLT_AMPERE_REACTIVE, None),
        ("Var", UnitOfReactivePower.VOLT_AMPERE_REACTIVE, None),
        ("varh", UnitOfReactiveEnergy.VOLT_AMPERE_REACTIVE_HOUR, SensorDeviceClass.REACTIVE_ENERGY),
        # Dimensionless, and specifically not a percentage.
        ("cos()", None, SensorDeviceClass.POWER_FACTOR),
        ("PF", None, SensorDeviceClass.POWER_FACTOR),
    ],
)
def test_known_units_map_to_ha_units(sunspec_unit, expected_unit, expected_device_class):
    unit, _icon, device_class = HA_META[sunspec_unit]

    assert unit == expected_unit
    assert device_class == expected_device_class


@pytest.mark.parametrize(
    "sunspec_unit",
    [
        # Rates: HA has no compound percent-per-time unit.
        "% WMax/min",
        "%Max/Sec",
        "V/s",
        # Quantities HA has no unit for.
        "VAh",
        "Ah",
        # Not measurements at all.
        "SF",
        "YYYYMMDD",
        "something the spec has not invented yet",
    ],
)
def test_unknown_units_do_not_become_ha_units(sunspec_unit):
    """The fallback must drop the unit, not pass the raw string through.

    Passing it through made it the entity's native_unit_of_measurement,
    and state_class returns MEASUREMENT for anything with a unit, so
    every unmapped unit started a long-term statistics series under
    something the recorder can never convert.
    """
    unit, _icon, device_class = HA_META.get(sunspec_unit, [None, None, None])

    assert unit is None
    assert device_class is None


# ---------- bitfield sensors (cjne/ha-sunspec#370) --------------------------


async def test_multi_bit_event_survives_a_refresh(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Two events at once must not break the entity on every poll.

    tests/test_data/inverter.json sets model 103 Evt1 to 3, so two bits
    are on and the state renders "GROUND_FAULT,DC_OVER_VOLT". While
    bitfields carried the ENUM device class, that string was not in
    ``options`` and HA raised ValueError from async_write_ha_state.
    entity_platform swallows it during setup, which is why this looked
    harmless, so the refresh below is the part that matters: it re-fires
    the listener and the exception escapes.

    The rest of the suite used to avoid MOCK_CONFIG for exactly this
    reason. Using it here is the point of the test.
    """
    config_entry = await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG)

    state = hass.states.get(TEST_INVERTER_SENSOR_EVENT_ENTITY_ID)
    assert state is not None
    assert state.state == "GROUND_FAULT,DC_OVER_VOLT"

    # Would raise before the fix.
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(TEST_INVERTER_SENSOR_EVENT_ENTITY_ID).state == (
        "GROUND_FAULT,DC_OVER_VOLT"
    )


async def test_bitfield_has_no_enum_device_class(hass: HomeAssistant, sunspec_client_mock) -> None:
    """A set of flags is not one-of-N, so it must not claim to be.

    Enumerating combinations is not the alternative: 32 flags would be
    4 billion options.
    """
    await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG)

    state = hass.states.get(TEST_INVERTER_SENSOR_EVENT_ENTITY_ID)

    assert state.attributes.get("device_class") is None
    assert "options" not in state.attributes


async def test_bitfield_exposes_active_flags_as_a_list(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """The comma string is readable; the list is what templates want."""
    await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG)

    state = hass.states.get(TEST_INVERTER_SENSOR_EVENT_ENTITY_ID)

    assert state.attributes["active_flags"] == ["GROUND_FAULT", "DC_OVER_VOLT"]
    assert state.attributes["raw"] == 3


async def test_enum_sensor_still_has_its_device_class(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """enum16 is genuinely one-of-N and keeps ENUM."""
    await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG)

    state = hass.states.get(TEST_INVERTER_SENSOR_STATE_ENTITY_ID)

    assert state.attributes.get("device_class") == "enum"
    assert state.state in state.attributes["options"]


async def test_power_filter_falls_back_to_the_detected_nameplate(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """A user who never filled in the peak still gets a filter.

    Both plausibility filters used to be off entirely unless
    CONF_MAX_AC_POWER_KW was set in the options, which is exactly the
    user who does not know they need it: #42 reported a 10 kW spike on
    an inverter that never approaches 10 kW. The nameplate the
    coordinator already reads from model 120 is a better default than no
    filter, with headroom on top because a real inverter does overshoot
    its continuous rating.
    """
    config_entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG, options={})
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)
    coordinator = config_entry.runtime_data

    power_sensor = None
    for entity in hass.data["entity_components"]["sensor"].entities:
        if entity.entity_id == TEST_INVERTER_SENSOR_POWER_ENTITY_ID:
            power_sensor = entity
            break
    assert power_sensor is not None

    # No nameplate, no filter: exactly the behaviour before this change.
    coordinator.detected_max_ac_power_kw = None
    with patch.object(coordinator.data[103], "getValue", return_value=10000.0):
        assert power_sensor.native_value == 10000.0

    # Nameplate known: the same reading is now recognised as garbage.
    coordinator.detected_max_ac_power_kw = 0.5
    with patch.object(coordinator.data[103], "getValue", return_value=10000.0):
        assert power_sensor.native_value is None

    # And a plausible reading still passes, headroom included.
    with patch.object(coordinator.data[103], "getValue", return_value=550.0):
        assert power_sensor.native_value == 550.0


async def test_user_peak_wins_over_the_detected_nameplate(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """A configured peak is a deliberate statement about the site.

    So it is used as given, without the headroom the auto-detected
    nameplate gets.
    """
    config_entry = create_mock_sunspec_config_entry(
        hass,
        data=MOCK_CONFIG,
        options={CONF_MAX_AC_POWER_KW: 10.0},
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)
    coordinator = config_entry.runtime_data
    coordinator.detected_max_ac_power_kw = 0.5

    power_sensor = None
    for entity in hass.data["entity_components"]["sensor"].entities:
        if entity.entity_id == TEST_INVERTER_SENSOR_POWER_ENTITY_ID:
            power_sensor = entity
            break
    assert power_sensor is not None

    with patch.object(coordinator.data[103], "getValue", return_value=9000.0):
        assert power_sensor.native_value == 9000.0


async def test_energy_sensor_keeps_its_baseline_across_a_gap(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """One missing reading must not cost two, plus the delta baseline.

    A None reading used to fall through to the assignment at the bottom
    of native_value and set lastKnown to None. The next good read then
    found no baseline, took the "establishing baseline" branch, and was
    discarded as well. So a single gap became two, and the delta filter
    lost the reference point it needs to reject a garbage jump.
    """
    from custom_components.sunspec2.sensor import SunSpecEnergySensor

    config_entry = create_mock_sunspec_config_entry(
        hass,
        data=MOCK_CONFIG,
        options={CONF_MAX_AC_POWER_KW: 10.0},
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)
    coordinator = config_entry.runtime_data

    energy_sensor = None
    for entity in hass.data["entity_components"]["sensor"].entities:
        if isinstance(entity, SunSpecEnergySensor) and entity.key == "WH":
            energy_sensor = entity
            break
    assert energy_sensor is not None

    baseline = energy_sensor.native_value
    assert baseline is not None

    # The model is missing from this cycle's data, so native_value on the
    # base class returns None.
    with patch.object(coordinator.data[103], "getValue", return_value=None):
        assert energy_sensor.native_value == baseline
    assert energy_sensor.lastKnown == baseline

    # The next good read is served straight away, not swallowed by the
    # baseline branch.
    with patch.object(coordinator.data[103], "getValue", return_value=baseline + 10):
        assert energy_sensor.native_value == baseline + 10


def _find_sensor(hass, entity_id):
    for entity in hass.data["entity_components"]["sensor"].entities:
        if entity.entity_id == entity_id:
            return entity
    raise AssertionError(f"{entity_id} was not built")


def test_only_measured_power_points_are_gated() -> None:
    """The filter must never touch a rating, a setpoint or a curve point.

    Before #45 it keyed on the UNIT alone, so every static register that
    happens to be measured in watts was gated by a ceiling derived from
    that same register. On a device whose VARtg exceeds its watt rating,
    the rated-apparent-power sensor simply read "unknown".
    """
    for measured in ("W", "WphA", "VA", "VAr", "VAR", "DCW", "InDCW", "module:0:DCW"):
        assert measured_power_headroom(measured) is not None, measured

    for static in (
        "WRtg",
        "VARtg",
        "WMax",
        "VAMax",
        "VArMaxQ1",
        "VarSet",
        "VarSetRvrt",
        "WSetRvrt",
        "PMaxLim",
        "LifeTimeMaxOut",
        "MaxChaRte",
        "WChaMax",
    ):
        assert measured_power_headroom(static) is None, static


def test_headroom_ranks_dc_above_apparent_above_active() -> None:
    """The ceiling is an ACTIVE power number; the rest are bounded elsewhere.

    VA >= W by definition and DCW = W / efficiency, so a single watt
    ceiling applied flat to all three takes out VA and DCW before it ever
    takes out W. That ordering is the whole of #45.
    """
    active = measured_power_headroom("W")
    apparent = measured_power_headroom("VA")
    reactive = measured_power_headroom("VAr")
    dc = measured_power_headroom("DCW")
    assert active == 1.0
    assert apparent == reactive > active
    assert dc > apparent


def test_power_limit_still_needs_a_power_unit() -> None:
    """Model 64411 names a VOLTAGE point ``VA``. Only the unit separates them."""
    assert _power_limit_in_native_unit(UnitOfElectricPotential.VOLT, "VA", 5.0) is None
    assert _power_limit_in_native_unit(UnitOfApparentPower.VOLT_AMPERE, "VA", 5.0) == 6250.0
    assert _power_limit_in_native_unit(UnitOfPower.WATT, "W", 5.0) == 5000.0
    assert _power_limit_in_native_unit(UnitOfPower.WATT, "DCW", 5.0) == 15000.0
    # No ceiling configured and none detected: no filtering at all.
    assert _power_limit_in_native_unit(UnitOfPower.WATT, "W", None) is None


async def test_dc_power_is_not_gated_by_the_ac_ceiling(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """#45: DC Watts read "unknown" all day on a ceiling sized for AC.

    DCW is measured before conversion losses, so it always sits above AC
    output, and on a hybrid the DC side carries AC output plus battery
    charge power at once. A reading 30 percent above the AC ceiling is
    normal for DC and garbage for AC, and the filter now tells them apart.
    """
    config_entry = create_mock_sunspec_config_entry(
        hass, data=MOCK_CONFIG, options={CONF_MAX_AC_POWER_KW: 1.0}
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)
    coordinator = config_entry.runtime_data

    ac = _find_sensor(hass, TEST_INVERTER_SENSOR_POWER_ENTITY_ID)
    dc = _find_sensor(hass, TEST_INVERTER_SENSOR_DC_POWER_ENTITY_ID)

    with patch.object(coordinator.data[103], "getValue", return_value=1300.0):
        assert ac.native_value is None
        assert dc.native_value == 1300.0

    # Order-of-magnitude garbage is still caught on the DC side.
    with patch.object(coordinator.data[103], "getValue", return_value=100000.0):
        assert dc.native_value is None


async def test_apparent_power_gets_its_own_headroom(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """VA >= W by definition, so it cannot share the active-power ceiling.

    Grid codes require operation down to cos phi 0.80, which puts
    apparent power legitimately above active power.
    """
    config_entry = create_mock_sunspec_config_entry(
        hass, data=MOCK_CONFIG, options={CONF_MAX_AC_POWER_KW: 1.0}
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)
    coordinator = config_entry.runtime_data

    va = _find_sensor(hass, TEST_INVERTER_SENSOR_VA_ENTITY_ID)

    with patch.object(coordinator.data[103], "getValue", return_value=1200.0):
        assert va.native_value == 1200.0
    with patch.object(coordinator.data[103], "getValue", return_value=1300.0):
        assert va.native_value is None


async def test_power_filter_catches_negative_garbage(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """The filter compares abs(), because the garbage it catches is unsigned.

    pysunspec2 packs the Modbus TCP transaction id as a literal 0 and
    never validates it on read, so a late reply shifts every register
    after it and the damage lands wherever it lands. The old one-sided
    ``val > limit`` left half of that unguarded, while still clipping the
    legitimately bipolar points (meter W, battery 802 W) in the other
    direction.
    """
    config_entry = create_mock_sunspec_config_entry(
        hass, data=MOCK_CONFIG, options={CONF_MAX_AC_POWER_KW: 1.0}
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)
    coordinator = config_entry.runtime_data

    power = _find_sensor(hass, TEST_INVERTER_SENSOR_POWER_ENTITY_ID)

    with patch.object(coordinator.data[103], "getValue", return_value=-50000.0):
        assert power.native_value is None
    # A legitimate negative reading inside the ceiling still passes: a
    # meter importing, or a battery charging.
    with patch.object(coordinator.data[103], "getValue", return_value=-800.0):
        assert power.native_value == -800.0


async def test_rejection_logging_is_throttled_and_reports_recovery(
    hass: HomeAssistant, sunspec_client_mock, caplog
) -> None:
    """One WARNING per sensor per poll, forever, buried #45's own evidence.

    The reporter had the answer in his log all along. Log the first
    rejection of a run, then every IMPLAUSIBLE_LOG_EVERY-th, then one
    line when the sensor comes back.
    """
    config_entry = create_mock_sunspec_config_entry(
        hass, data=MOCK_CONFIG, options={CONF_MAX_AC_POWER_KW: 1.0}
    )
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)
    coordinator = config_entry.runtime_data
    power = _find_sensor(hass, TEST_INVERTER_SENSOR_POWER_ENTITY_ID)

    caplog.clear()
    with patch.object(coordinator.data[103], "getValue", return_value=99000.0):
        for _ in range(IMPLAUSIBLE_LOG_EVERY + 1):
            assert power.native_value is None
    dropped = [r for r in caplog.records if "Dropping implausible value" in r.getMessage()]
    # First rejection plus the one at IMPLAUSIBLE_LOG_EVERY, not one per read.
    assert len(dropped) == 2
    # The message must say where the ceiling came from. "configured peak"
    # on a value nobody configured sent the reporter looking in the UI
    # for a field that was empty.
    assert "configured peak AC power" in dropped[0].getMessage()

    caplog.clear()
    with patch.object(coordinator.data[103], "getValue", return_value=800.0):
        assert power.native_value == 800.0
    assert any("back inside the plausibility ceiling" in r.getMessage() for r in caplog.records)


async def test_rejection_log_names_the_autodetected_nameplate(
    hass: HomeAssistant, sunspec_client_mock, caplog
) -> None:
    """A ceiling nobody typed must not be reported as "configured"."""
    config_entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG, options={})
    await setup_mock_sunspec_config_entry(hass, config_entry=config_entry)
    coordinator = config_entry.runtime_data
    coordinator.detected_max_ac_power_kw = 1.0
    coordinator.detected_max_ac_power_source = "model 120 WRtg"
    power = _find_sensor(hass, TEST_INVERTER_SENSOR_POWER_ENTITY_ID)

    caplog.clear()
    with patch.object(coordinator.data[103], "getValue", return_value=99000.0):
        assert power.native_value is None

    dropped = [r for r in caplog.records if "Dropping implausible value" in r.getMessage()]
    assert dropped
    assert "auto-detected nameplate from model 120 WRtg" in dropped[0].getMessage()
