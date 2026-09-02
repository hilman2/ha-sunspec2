"""Sensor platform for SunSpec."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import RestoreSensor
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import DEGREE
from homeassistant.const import PERCENTAGE
from homeassistant.const import EntityCategory
from homeassistant.const import UnitOfApparentPower
from homeassistant.const import UnitOfDataRate
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.const import UnitOfElectricPotential
from homeassistant.const import UnitOfEnergy
from homeassistant.const import UnitOfFrequency
from homeassistant.const import UnitOfIrradiance
from homeassistant.const import UnitOfLength
from homeassistant.const import UnitOfPower
from homeassistant.const import UnitOfPressure
from homeassistant.const import UnitOfReactiveEnergy
from homeassistant.const import UnitOfReactivePower
from homeassistant.const import UnitOfSpeed
from homeassistant.const import UnitOfTemperature
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SunSpec2ConfigEntry
from . import SunSpecDataUpdateCoordinator
from . import get_sunspec_unique_id
from .const import CONF_MAX_AC_POWER_KW
from .const import CONF_PREFIX
from .const import CONF_SCAN_INTERVAL
from .const import DOMAIN
from .const import ENERGY_DELTA_REJECT_RECOVERY_COUNT
from .const import ENERGY_DELTA_SAFETY_FACTOR
from .const import IMPLAUSIBLE_LOG_EVERY
from .const import NAMEPLATE_FILTER_HEADROOM
from .const import effective_peak_power_kw
from .const import is_excluded_sensor_point
from .const import measured_power_headroom
from .entity import SunSpecEntity

# Bronze rule parallel-updates: the coordinator already serialises all
# I/O via its per-gateway asyncio.Lock, so platform entities never
# need their own concurrency limit. ``0`` means HA will not attempt
# to throttle entity updates from this platform - the coordinator
# does the throttling for us.
PARALLEL_UPDATES = 0

_LOGGER: logging.Logger = logging.getLogger(__package__)

# Gold rule entity-translations: curated map of common SunSpec point
# keys to ``translation_key`` slugs. The matching entries live under
# ``entity.sensor.<slug>`` in translations/en.json and translations/
# de.json. Points NOT in this map fall back to the per-entity name
# the SunSpec model definition supplies via pysunspec2 - that name
# is already a human-readable English string from the SunSpec spec
# itself, so the field is never empty even for the long tail of
# vendor-specific or rarely-used points.
#
# Entries in repeating groups (e.g. mppt module 0 / 1) deliberately
# keep their hand-rolled name with the index in front instead of a
# translation_key, because translation_key + repeating index +
# device-name composition becomes confusing fast.
SUNSPEC_POINT_TRANSLATION_KEYS: dict[str, str] = {
    # Inverter common (model 101 / 102 / 103)
    "A": "amps",
    "AphA": "amps_l1",
    "AphB": "amps_l2",
    "AphC": "amps_l3",
    "PPVphAB": "phase_voltage_l1_l2",
    "PPVphBC": "phase_voltage_l2_l3",
    "PPVphCA": "phase_voltage_l3_l1",
    "PhVphA": "phase_voltage_l1_n",
    "PhVphB": "phase_voltage_l2_n",
    "PhVphC": "phase_voltage_l3_n",
    "W": "watts",
    "Hz": "frequency",
    "VA": "apparent_power",
    "VAr": "reactive_power",
    "PF": "power_factor",
    "WH": "lifetime_energy",
    "DCA": "dc_current",
    "DCV": "dc_voltage",
    "DCW": "dc_power",
    "TmpCab": "cabinet_temperature",
    "TmpSnk": "heat_sink_temperature",
    "TmpTrns": "transformer_temperature",
    "TmpOt": "other_temperature",
    "St": "operating_state",
    "StVnd": "vendor_operating_state",
    "Evt1": "events_1",
    "Evt2": "events_2",
    "EvtVnd1": "vendor_events_1",
    "EvtVnd2": "vendor_events_2",
    "EvtVnd3": "vendor_events_3",
    "EvtVnd4": "vendor_events_4",
    # Inverter Nameplate (model 120)
    "WRtg": "rated_power",
    "VARtg": "rated_apparent_power",
    "ARtg": "rated_current",
    "WHRtg": "rated_lifetime_energy",
    # Inverter Settings (model 121)
    "WMax": "max_power_setting",
    "VRef": "voltage_reference",
}

ICON_DEFAULT = "mdi:information-outline"
ICON_AC_AMPS = "mdi:current-ac"
ICON_DC_AMPS = "mdi:current-dc"
ICON_VOLT = "mdi:lightning-bolt"
ICON_POWER = "mdi:solar-power"
ICON_FREQ = "mdi:sine-wave"
ICON_ENERGY = "mdi:solar-panel"
ICON_TEMP = "mdi:thermometer"

_POWER_UNITS = (
    UnitOfPower.WATT,
    UnitOfApparentPower.VOLT_AMPERE,
    UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
)


def _power_limit_in_native_unit(
    unit: str | None, key: str, max_power_kw: float | None
) -> float | None:
    """Upper bound for one sensor, in its own unit, or ``None`` for no bound.

    All SunSpec power-like units (W, VA, VAr) are 1:1 with watts in HA, so
    the same kW-to-W conversion applies to each; what differs is how far
    above the peak AC power the quantity may legitimately sit.

    The unit check stays in front of the name lookup and is not redundant:
    model 64411 names a *voltage* point ``VA``, and only the unit tells
    the two apart.
    """
    if max_power_kw is None or unit not in _POWER_UNITS:
        return None
    headroom = measured_power_headroom(key)
    if headroom is None:
        return None
    return max_power_kw * 1000.0 * headroom


def _energy_delta_limit_in_native_unit(
    unit: str | None, max_power_kw: float | None, window_seconds: float | None
) -> float | None:
    """Compute the maximum plausible energy delta over ``window_seconds``.

    Derived from the configured peak power and the time the energy can
    have accumulated over, with the safety factor in
    :data:`ENERGY_DELTA_SAFETY_FACTOR`. Returns ``None`` if the sensor is
    not a known energy unit or no peak power is configured.
    """
    if max_power_kw is None or window_seconds is None:
        return None
    max_delta_kwh = max_power_kw * (window_seconds / 3600.0) * ENERGY_DELTA_SAFETY_FACTOR
    if unit == UnitOfEnergy.WATT_HOUR:
        return max_delta_kwh * 1000.0
    if unit == UnitOfEnergy.KILO_WATT_HOUR:
        return max_delta_kwh
    return None


HA_META: dict[str, tuple[str | None, str, SensorDeviceClass | None]] = {
    "A": (UnitOfElectricCurrent.AMPERE, ICON_AC_AMPS, SensorDeviceClass.CURRENT),
    "HPa": (UnitOfPressure.HPA, ICON_DEFAULT, None),
    "Hz": (UnitOfFrequency.HERTZ, ICON_FREQ, None),
    "Mbps": (UnitOfDataRate.MEGABITS_PER_SECOND, ICON_DEFAULT, None),
    "V": (UnitOfElectricPotential.VOLT, ICON_VOLT, SensorDeviceClass.VOLTAGE),
    "VA": (UnitOfApparentPower.VOLT_AMPERE, ICON_POWER, None),
    "VAr": (UnitOfReactivePower.VOLT_AMPERE_REACTIVE, ICON_POWER, None),
    "W": (UnitOfPower.WATT, ICON_POWER, SensorDeviceClass.POWER),
    "W/m2": (UnitOfIrradiance.WATTS_PER_SQUARE_METER, ICON_DEFAULT, None),
    "Wh": (UnitOfEnergy.WATT_HOUR, ICON_ENERGY, SensorDeviceClass.ENERGY),
    "WH": (UnitOfEnergy.WATT_HOUR, ICON_ENERGY, SensorDeviceClass.ENERGY),
    "bps": (UnitOfDataRate.BITS_PER_SECOND, ICON_DEFAULT, None),
    "deg": (DEGREE, ICON_TEMP, SensorDeviceClass.TEMPERATURE),
    "Degrees": (DEGREE, ICON_TEMP, SensorDeviceClass.TEMPERATURE),
    "C": (UnitOfTemperature.CELSIUS, ICON_TEMP, SensorDeviceClass.TEMPERATURE),
    "kWh": (UnitOfEnergy.KILO_WATT_HOUR, ICON_ENERGY, SensorDeviceClass.ENERGY),
    "m/s": (UnitOfSpeed.METERS_PER_SECOND, ICON_DEFAULT, None),
    "mSecs": (UnitOfTime.MILLISECONDS, ICON_DEFAULT, None),
    "meters": (UnitOfLength.METERS, ICON_DEFAULT, None),
    "mm": (UnitOfLength.MILLIMETERS, ICON_DEFAULT, None),
    "%": (PERCENTAGE, ICON_DEFAULT, None),
    "Secs": (UnitOfTime.SECONDS, ICON_DEFAULT, None),
    "Sec": (UnitOfTime.SECONDS, ICON_DEFAULT, None),
    # SunSpec spells reactive power four ways across its model
    # definitions and we only had one of them, so "var" and "Var" points
    # (49 and 10 respectively) fell through to the raw-string fallback.
    "var": (UnitOfReactivePower.VOLT_AMPERE_REACTIVE, ICON_POWER, None),
    "Var": (UnitOfReactivePower.VOLT_AMPERE_REACTIVE, ICON_POWER, None),
    "varh": (
        UnitOfReactiveEnergy.VOLT_AMPERE_REACTIVE_HOUR,
        ICON_ENERGY,
        SensorDeviceClass.REACTIVE_ENERGY,
    ),
    "Varh": (
        UnitOfReactiveEnergy.VOLT_AMPERE_REACTIVE_HOUR,
        ICON_ENERGY,
        SensorDeviceClass.REACTIVE_ENERGY,
    ),
    "VArh": (
        UnitOfReactiveEnergy.VOLT_AMPERE_REACTIVE_HOUR,
        ICON_ENERGY,
        SensorDeviceClass.REACTIVE_ENERGY,
    ),
    # Percentages of some reference quantity. The reference belongs in
    # the entity name, not in the unit: HA has one percent and the
    # recorder can only merge series that agree on it.
    "Pct": (PERCENTAGE, ICON_DEFAULT, None),
    "% VRef": (PERCENTAGE, ICON_DEFAULT, None),
    "% WMax": (PERCENTAGE, ICON_DEFAULT, None),
    "% WRef": (PERCENTAGE, ICON_DEFAULT, None),
    "% VArMax": (PERCENTAGE, ICON_DEFAULT, None),
    "% VArAval": (PERCENTAGE, ICON_DEFAULT, None),
    "VNomPct": (PERCENTAGE, ICON_DEFAULT, None),
    "%WHRtg": (PERCENTAGE, ICON_DEFAULT, None),
    # Power factor is dimensionless in SunSpec (cosine of the phase
    # angle, -1..1). HA's POWER_FACTOR device class accepts exactly no
    # unit or percent, and the value is not a percentage.
    "cos()": (None, ICON_DEFAULT, SensorDeviceClass.POWER_FACTOR),
    "PF": (None, ICON_DEFAULT, SensorDeviceClass.POWER_FACTOR),
    "W/m^2": (UnitOfIrradiance.WATTS_PER_SQUARE_METER, ICON_DEFAULT, None),
    "enum16": (None, ICON_DEFAULT, SensorDeviceClass.ENUM),
    # Bitfields are deliberately NOT the ENUM device class. See the
    # vtype handling in SunSpecSensor.__init__ for why.
    "bitfield16": (None, ICON_DEFAULT, None),
    "bitfield32": (None, ICON_DEFAULT, None),
}

# Deliberately absent, and this is the point of the None fallback rather
# than a growing table:
#
#   * Rates ("% WMax/min", "%Max/Sec", "% PF/min", "V/s", "%ARtg/%dV").
#     HA has no compound percent-per-time unit and inventing one would
#     put the series right back under something unconvertible.
#   * Quantities HA has no unit for at all: "VAh" (apparent energy, 67
#     points), "Ah" / "AH" (charge).
#   * Things that are not measurements: "SF" (scale factor), "Tms"
#     (a SunSpec timestamp, not a duration), "YYYYMMDD".


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform with dynamic SunSpec model re-detection.

    v0.13.0 (cjne issue #200): the entity list is no longer fixed at
    setup time. Instead we register a coordinator listener that runs
    on every successful update cycle and adds entities for any
    (model_id, key, model_index) triple we have not seen before.
    That way an inverter that exposes a new SunSpec model after a
    firmware update gets its sensors picked up automatically on the
    next refresh, without an HA restart.

    The "have we seen this entity before" check uses the SunSpec
    unique_id as the dedup key. Entities for points that vanish from
    the inverter (the cjne #202 case) are NOT removed - HA's
    stale-data tolerance keeps them on their last good value and
    the user can decide via the device-info page whether to delete
    the device.
    """
    # The role-named model 160 sensors subclass the sensors below, so
    # their module cannot be imported before this one is complete.
    from .dc_channels import dc_channel_sensors
    from .fronius_web_entities import fronius_web_sensors

    coordinator = entry.runtime_data
    # Read the cached common-model (model 1) from the coordinator. The
    # coordinator populates this during its locked update cycle, so we
    # do NOT open a second Modbus TCP connection here - that would
    # deadlock single-slot inverters such as KACO Powador and time out
    # the platform setup after 60s.
    device_info = coordinator.device_info
    prefix = entry.options.get(CONF_PREFIX, entry.data.get(CONF_PREFIX, ""))

    known_unique_ids: set[str] = set()

    @callback
    def _async_add_new_sensors() -> None:
        """Walk coordinator.data and add any sensor we haven't seen yet."""
        if coordinator.data is None:
            return
        new_sensors: list[SensorEntity] = []
        for model_id, model_wrapper in coordinator.data.items():
            for key in model_wrapper.getKeys():
                if is_excluded_sensor_point(model_id, key):
                    # Either a control the user operates through a
                    # Number / Switch, or a unit HA cannot map. See
                    # SENSOR_EXCLUDED_POINTS for why a sensor here is
                    # actively harmful rather than merely redundant.
                    continue
                for model_index in range(model_wrapper.num_models):
                    uid = get_sunspec_unique_id(entry.entry_id, key, model_id, model_index)
                    if uid in known_unique_ids:
                        continue
                    known_unique_ids.add(uid)
                    data = {
                        "device_info": device_info,
                        "key": key,
                        "model_id": model_id,
                        "model_index": model_index,
                        "model": model_wrapper,
                        "prefix": prefix,
                    }
                    meta = model_wrapper.getMeta(key)
                    sunspec_unit = meta.get("units", "")
                    ha_meta = HA_META.get(sunspec_unit, (sunspec_unit, None, None))
                    device_class = ha_meta[2]
                    if device_class == SensorDeviceClass.ENERGY:
                        new_sensors.append(SunSpecEnergySensor(coordinator, entry, data))
                    else:
                        new_sensors.append(SunSpecSensor(coordinator, entry, data))
        mppt_wrapper = coordinator.data.get(160)
        if mppt_wrapper is not None and device_info is not None:
            for sensor in dc_channel_sensors(coordinator, entry, device_info, mppt_wrapper, prefix):
                role_uid = sensor.unique_id
                if role_uid is None or role_uid in known_unique_ids:
                    continue
                known_unique_ids.add(role_uid)
                new_sensors.append(sensor)
        if new_sensors:
            _LOGGER.debug(
                "Adding %d sensor(s) (total tracked: %d)",
                len(new_sensors),
                len(known_unique_ids),
            )
            async_add_devices(new_sensors)

    # Register the listener so subsequent coordinator refreshes can
    # also pick up newly-discovered models, then run it once
    # synchronously to add the initial set.
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_sensors))
    _async_add_new_sensors()
    # Fed by the web interface's own coordinator, so not part of the
    # Modbus re-detection above; the set is known at setup.
    async_add_devices(fronius_web_sensors(coordinator, entry, prefix))


class SunSpecSensor(SunSpecEntity, SensorEntity):
    """sunspec Sensor class."""

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        data: dict[str, Any],
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            data["device_info"],
            data["model"].getGroupMeta(),
            prefix=data["prefix"],
            model_id=data["model_id"],
        )
        self.model_id = data["model_id"]
        self.model_index = data["model_index"]
        self.model_wrapper = data["model"]
        self.key = data["key"]
        self._meta = self.model_wrapper.getMeta(self.key)
        self._group_meta = self.model_wrapper.getGroupMeta()
        self._point_meta = self.model_wrapper.getPoint(self.key).pdef
        sunspec_unit = self._meta.get("units", self._meta.get("type", ""))
        # Unknown units resolve to no unit at all, NOT to the raw SunSpec
        # string. Passing the string through made it the entity's
        # native_unit_of_measurement, and state_class returns MEASUREMENT
        # for anything with a unit, so every unmapped unit started a
        # long-term statistics series under a unit the recorder can never
        # convert or merge. There are 29 such units across the bundled
        # model definitions ("% VRef" alone appears on 184 points), so
        # this was never specific to model 123. The value stays visible,
        # it just stops pretending to be a measurable quantity.
        ha_meta = HA_META.get(sunspec_unit, (None, ICON_DEFAULT, None))
        self.unit = ha_meta[0]
        self.use_icon = ha_meta[1]
        self.use_device_class = ha_meta[2]
        self._options: list[str] = []
        # Used if this is an energy sensor and the read value is 0
        # Updated whenever the value read is not 0
        # The baseline the delta plausibility filter measures against.
        # Annotated as a number because that is what an energy counter
        # reports, but the value travels here from pysunspec2 through an
        # untyped getValue, so the filter below still checks it with
        # isinstance before doing arithmetic on it. Those checks are not
        # redundant with this annotation.
        self.lastKnown: float | None = None
        self._assumed_state = False

        self._unique_id = get_sunspec_unique_id(
            config_entry.entry_id, self.key, self.model_id, self.model_index
        )

        vtype = self._meta["type"]
        # A bitfield is a SET of flags, an enum is one of N states, and
        # only the second is what HA's ENUM device class describes.
        #
        # Both used to be ENUM here, with ``options`` listing the
        # individual symbol names. An inverter reporting two events at
        # once renders as "GROUND_FAULT,DC_OVER_VOLT", which is not in
        # that list, and HA validates ENUM states against it strictly:
        # async_write_ha_state raises ValueError. entity_platform
        # swallows it during the initial setup, so it looked harmless,
        # but every later coordinator refresh re-fires the listener and
        # the exception escapes. cjne/ha-sunspec#370 is the same bug,
        # and our own test suite had been steering around it by using a
        # fixture without a multi-bit event value.
        #
        # Enumerating the combinations instead is not an option: n flags
        # give 2**n states, and model 103's Evt1 alone has 32.
        self._is_bitfield = vtype in ("bitfield16", "bitfield32")
        if vtype == "enum16":
            symbols = self._point_meta.get("symbols", None)
            if symbols is None:
                self.use_device_class = None
            else:
                self.use_device_class = SensorDeviceClass.ENUM
                self._options = [item["name"] for item in symbols]
                # The empty string is a real state: an enum point whose
                # value matches no symbol renders as "".
                self._options.append("")
        elif self._is_bitfield:
            # No device class, so no options and no state validation.
            # The decoded flag names stay in the state string, which is
            # what people build template sensors on, and land in an
            # attribute for anything that wants them as a list.
            self.use_device_class = None

        self._device_id = config_entry.entry_id
        # Use the coordinator's context-bound logger when available so warnings
        # from native_value carry host:port#unit_id automatically. Fallback to
        # the module logger for tests that supply a stub coordinator without
        # an _log attribute (see tests/__init__.py:MockSunSpecDataUpdateCoordinator).
        self._log = getattr(coordinator, "_log", _LOGGER)
        # Consecutive reads rejected by the power plausibility filter.
        # Drives the log throttle and the "back inside the ceiling"
        # recovery line; see the filter branch in native_value.
        self._implausible_rejections = 0
        # has_entity_name = True (set on the SunSpecEntity base class)
        # means HA composes the device name in front of the entity
        # name automatically, so the name property here only carries
        # the per-entity bit. The user sees
        # "<device name from Md> <label>" in the UI - e.g.
        # "Powador 7.8 TL3 Watts" instead of the old hand-rolled
        # "Inverter Three Phase Watts".
        #
        # Repeating-group entries (model 160 mppt module 0/1, etc.)
        # still need an index in the name to disambiguate. The user
        # prefix from CONF_PREFIX is NOT included here - it lives on
        # the device name instead (see SunSpecEntity.device_info) so
        # multi-inverter setups disambiguate at the device level.
        desc = self._meta.get("label", self.key)
        if self.unit == UnitOfElectricCurrent.AMPERE and "DC" in desc:
            self.use_icon = ICON_DC_AMPS

        name_parts: list[str] = []
        key_parts = self.key.split(":")
        if len(key_parts) > 1:
            # e.g. "module:0:DCA" -> prepend "Module 0" before the label
            group_label = key_parts[0].replace("_", " ").title()
            name_parts.append(f"{group_label} {key_parts[1]}")
        elif self.model_index > 0:
            # Multiple models of the same id - keep an index in the name
            name_parts.append(str(self.model_index))
        name_parts.append(desc)
        self._name = " ".join(name_parts)

        # Gold rule entity-translations: set translation_key for the
        # common SunSpec point keys we have curated translations for.
        # Repeating-group entries (key contains ":") deliberately keep
        # the hand-rolled name with the index because translation_key
        # plus a dynamic index plus device-name composition gets
        # confusing in the UI. Points without a curated translation
        # fall back to ``_attr_name`` (the SunSpec spec label, which
        # is already English).
        if ":" not in self.key:
            translation_key = SUNSPEC_POINT_TRANSLATION_KEYS.get(self.key)
            if translation_key:
                self._attr_translation_key = translation_key

        # Gold rule entity-category: temperatures, state enums and
        # event bitfields are diagnostic information, not the primary
        # data the user cares about. Tagging them lets HA group them
        # under "Diagnostic" in the device card so the main entity
        # list stays focused on power / energy / current / voltage.
        diagnostic_keys = {
            "TmpCab",
            "TmpSnk",
            "TmpTrns",
            "TmpOt",
            "Tmp",
            "St",
            "StVnd",
            "Evt1",
            "Evt2",
            "EvtVnd1",
            "EvtVnd2",
            "EvtVnd3",
            "EvtVnd4",
            "DCSt",
            "DCEvt",
            "GlbEvt",
            "Tms",
        }
        if (
            self.key in diagnostic_keys
            or self.use_device_class == SensorDeviceClass.ENUM
            or self._is_bitfield
        ):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

        # Gold rule entity-disabled-by-default: vendor-specific event
        # bitfields and the static nameplate / settings registers are
        # noisy or never-changing respectively. Disabling them by
        # default keeps the device card focused on the values that
        # actually move; users who care can enable them in the entity
        # registry.
        disabled_by_default_keys = {
            "EvtVnd1",
            "EvtVnd2",
            "EvtVnd3",
            "EvtVnd4",
            "StVnd",
            "WRtg",
            "VARtg",
            "ARtg",
            "WHRtg",
            "WMax",
            "VRef",
        }
        if self.key in disabled_by_default_keys:
            self._attr_entity_registry_enabled_default = False
        _LOGGER.debug(
            "Created sensor for %s in model %s using prefix %s: %s uid %s, device class %s unit %s",
            self.key,
            self.model_id,
            data["prefix"],
            self._name,
            self._unique_id,
            self.use_device_class,
            self.unit,
        )
        if self.device_class == SensorDeviceClass.ENUM:
            _LOGGER.debug("Valid options for ENUM: %s", self._options)

    # def async_will_remove_from_hass(self):
    #    _LOGGER.debug(f"Will remove sensor {self._unique_id}")

    @property
    def options(self) -> list[str] | None:
        if self.device_class != SensorDeviceClass.ENUM:
            return None
        return self._options

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return self._name

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return self._unique_id

    @property
    def assumed_state(self) -> bool:
        return self._assumed_state

    @property
    def _peak_power_kw(self) -> float | None:
        """Peak AC power to measure plausibility against, in kW.

        The user's configured value wins. Where there is none, the
        nameplate the coordinator auto-detected from model 120 / 121
        stands in, with :data:`NAMEPLATE_FILTER_HEADROOM` on top.

        Resolved per read rather than in ``__init__`` on purpose: the
        nameplate arrives on the first successful cycle, which can be
        after the entity was built, and an options change should take
        effect without a reload.
        """
        return effective_peak_power_kw(
            self.coordinator.entry.options.get(CONF_MAX_AC_POWER_KW),
            getattr(self.coordinator, "detected_max_ac_power_kw", None),
        )

    @property
    def _peak_power_source(self) -> str:
        """Where the ceiling came from, for the rejection log line.

        The old message said "configured peak" unconditionally. A user
        who read it, opened the options form and found the field empty
        concluded the log was lying, and the real provenance was only in
        a one-shot INFO line emitted on the first cycle after startup,
        long scrolled away by the time anything gets dropped (#45).
        """
        if self.coordinator.entry.options.get(CONF_MAX_AC_POWER_KW):
            return "configured peak AC power"
        source = getattr(self.coordinator, "detected_max_ac_power_source", None) or "model 120/121"
        return f"auto-detected nameplate from {source} x {NAMEPLATE_FILTER_HEADROOM}"

    @property
    def _max_native_value(self) -> float | None:
        """Upper plausibility bound in this sensor's own unit, or None."""
        return _power_limit_in_native_unit(self.unit, self.key, self._peak_power_kw)

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        try:
            val = self.coordinator.data[self.model_id].getValue(self.key, self.model_index)
        except KeyError:
            self._log.warning("Model %s not found", self.model_id)
            return None
        except OverflowError:
            self._log.warning(
                "Math overflow error when retrieving calculated value for %s", self.key
            )
            return None
        # Plausibility filter for measured power points: drop readings
        # beyond the ceiling. Inverters at dawn / dusk sometimes report
        # MW-range garbage that poisons long-term statistics.
        #
        # Compared on abs() since #45. The garbage this catches is not
        # signed: a misread scale factor or a shifted register lands
        # wherever it lands. (Until v0.31.0 a late Modbus reply was the
        # usual source of the shift; the embedded transport checks the
        # transaction id now, the other sources remain.) The one-sided
        # test left half of that unguarded while still clipping the
        # legitimately bipolar points - meter W, battery 802 W, DER
        # 714 DCW - in the other direction.
        limit = self._max_native_value
        if limit is not None and isinstance(val, (int, float)) and abs(val) > limit:
            self._implausible_rejections += 1
            count = self._implausible_rejections
            if count == 1 or count % IMPLAUSIBLE_LOG_EVERY == 0:
                self._log.warning(
                    "Dropping implausible value for %s: %s %s is beyond the %s %s ceiling "
                    "from %s (rejection %d). If this is a real reading, raise or clear "
                    "'Peak AC power' in the integration options.",
                    self.key,
                    val,
                    self.unit,
                    limit,
                    self.unit,
                    self._peak_power_source,
                    count,
                )
            return None
        if self._implausible_rejections:
            self._log.warning(
                "%s is back inside the plausibility ceiling after %d dropped read(s)",
                self.key,
                self._implausible_rejections,
            )
            self._implausible_rejections = 0
        vtype = self._meta["type"]
        if vtype in ("enum16", "bitfield32"):
            symbols = self._point_meta.get("symbols", None)
            if symbols is None:
                return val
            if vtype == "enum16":
                symbol = list(filter(lambda s: s["value"] == val, symbols))
                if len(symbol) == 1:
                    return symbol[0]["name"][:255]
                else:
                    return None
            else:
                symbols = list(filter(lambda s: (val >> int(s["value"])) & 1 == 1, symbols))
                if len(symbols) > 0:
                    return ",".join(map(lambda s: s["name"], symbols))[:255]
                return ""
        return val

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement."""
        return self.unit

    @property
    def icon(self) -> str | None:
        """Return the icon of the sensor."""
        return self.use_icon

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """Return de device class of the sensor."""
        return self.use_device_class

    @property
    def state_class(self) -> SensorStateClass | None:
        """Return de device class of the sensor."""
        if self.unit == "" or self.unit is None:
            return None
        if self.device_class == SensorDeviceClass.ENERGY:
            return SensorStateClass.TOTAL_INCREASING
        return SensorStateClass.MEASUREMENT

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        attrs: dict[str, Any] = {
            "integration": DOMAIN,
            "sunspec_key": self.key,
        }
        label = self._meta.get("label", None)
        if label is not None:
            attrs["label"] = label

        vtype = self._meta["type"]
        if vtype in ("enum16", "bitfield32", "bitfield16"):
            attrs["raw"] = self.coordinator.data[self.model_id].getValue(self.key, self.model_index)
        if self._is_bitfield:
            # The state string is convenient to read and awkward to
            # parse. Templates and automations get the same information
            # as a list here rather than splitting on commas.
            state = self.native_value
            attrs["active_flags"] = state.split(",") if state else []
        return attrs


class SunSpecEnergySensor(SunSpecSensor, RestoreSensor):
    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        data: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, config_entry, data)
        self.last_known_value: Any = None
        # Counter for consecutive rejected reads in the delta plausibility
        # filter. Once it crosses ENERGY_DELTA_REJECT_RECOVERY_COUNT we
        # accept the new value, otherwise a legitimate large jump (coarse
        # WH register granularity on KACO Powador, post-restore baseline
        # mismatch, etc.) would freeze the sensor permanently because the
        # filter never updates lastKnown while it rejects.
        self._rejected_delta_count = 0
        # When ``lastKnown`` last took a NEW value. The delta filter
        # measures a jump against the energy the inverter can have made
        # since then, not since the previous poll: a counter that stands
        # still for three polls and then moves by three polls' worth is
        # reporting correctly, and the filter used to reject exactly
        # that (#45, Fronius GEN24, WH updated every few minutes).
        self._counter_last_moved: datetime | None = None

    @property
    def _max_native_delta(self) -> float | None:
        """Maximum plausible increase since the counter last moved, in this unit.

        The window is the time since ``lastKnown`` last changed, floored
        at the scan interval, times the peak power. Floored, because the
        first read after a change is one poll later at the earliest; the
        floor is also what applies when nothing is known about the past,
        before the first read of a run with no restored state.

        ``None`` disables the check, which is what a sensor that is not
        an energy counter, or an install with no peak to go on, gets.
        """
        entry = self.coordinator.entry
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL))
        window = scan_interval
        if scan_interval is not None and self._counter_last_moved is not None:
            elapsed = (dt_util.utcnow() - self._counter_last_moved).total_seconds()
            window = max(float(scan_interval), elapsed)
        return _energy_delta_limit_in_native_unit(self.unit, self._peak_power_kw, window)

    def _counter_moved_to(self, val: Any) -> None:
        """Adopt ``val`` as the baseline and restart the plausibility window."""
        if val != self.lastKnown:
            self._counter_last_moved = dt_util.utcnow()
        self.lastKnown = val

    @property
    def native_value(self) -> Any:
        val = super().native_value
        if val is None:
            # No reading this cycle: the model was missing from the
            # coordinator's data, or the point could not be computed.
            #
            # Falling through would reach the assignment at the bottom
            # and set lastKnown to None, which costs more than the one
            # missing reading: the next good read then finds no baseline,
            # takes the "establishing baseline" branch, and is discarded
            # too. One gap becomes two and the delta filter loses its
            # reference point. Hold the last value instead, the same way
            # the val == 0 branch below does, so total_increasing keeps
            # its continuity.
            self._assumed_state = True
            return self.lastKnown
        # For an energy sensor a value of 0 woulld mess up long term stats because of how total_increasing works
        if val == 0:
            _LOGGER.debug(
                "Returning last known value instead of 0 for %s to avoid resetting the "
                "total_increasing counter",
                self.name,
            )
            self._assumed_state = True
            return self.lastKnown
        # Plausibility filter active but no baseline yet (e.g. fresh setup,
        # or restart where the restored state was not numeric): discard
        # this read so a potential garbage value never becomes the baseline.
        # The next poll will have a valid lastKnown to compare against.
        max_delta = self._max_native_delta
        if (
            val is not None
            and max_delta is not None
            and self.lastKnown is None
            and isinstance(val, (int, float))
        ):
            _LOGGER.info(
                "Establishing energy baseline for %s, discarding first read %s %s",
                self.key,
                val,
                self.unit,
            )
            self._counter_moved_to(val)
            self._assumed_state = True
            return None
        # Delta-based plausibility check: if the increase since the last
        # known value would imply a power above the configured peak over
        # the time since the counter last moved, treat the read as
        # garbage and fall back to the last known value (same mechanism
        # as the val == 0 path, so total_increasing stats stay intact).
        #
        # Recovery escape hatch: count consecutive rejections. If the
        # inverter keeps reporting the same large jump for several reads
        # in a row it is almost certainly not a transient spike but a
        # legitimate counter discontinuity (a restored baseline from a
        # state file older than its timestamp says, a counter that was
        # reset, ...). Without this hatch the sensor would freeze on
        # lastKnown forever because lastKnown is never updated while the
        # filter rejects. Counters that move in coarse steps used to
        # depend on this hatch too, three rejections per step; the
        # time window above handles them without a single rejection.
        #
        # A drop is rejected the same way, and without a peak power to go
        # on: a lifetime counter has no legitimate way down. The one seen
        # in the wild is a scale factor misread on a Fronius Symo, model
        # 160, which reports the total a thousand times too small for a
        # poll or two. Passed through, HA books it as a meter reset, and
        # when the real value returns, the jump back is what the
        # peak-power rule rejects, until the hatch accepts it and the
        # whole lifetime total lands in the statistics as new energy. A
        # counter that really was reset comes through the same hatch.
        implausible: str | None = None
        if (
            self.lastKnown is not None
            and isinstance(val, (int, float))
            and isinstance(self.lastKnown, (int, float))
        ):
            if val < self.lastKnown:
                implausible = "drop"
            elif max_delta is not None and (val - self.lastKnown) > max_delta:
                implausible = "jump"
        if implausible is not None:
            self._rejected_delta_count += 1
            if self._rejected_delta_count < ENERGY_DELTA_REJECT_RECOVERY_COUNT:
                if implausible == "drop":
                    _LOGGER.warning(
                        "Holding %s at %s %s: the inverter reported %s, and a lifetime "
                        "counter does not go down (rejection %d/%d)",
                        self.key,
                        self.lastKnown,
                        self.unit,
                        val,
                        self._rejected_delta_count,
                        ENERGY_DELTA_REJECT_RECOVERY_COUNT,
                    )
                else:
                    _LOGGER.warning(
                        "Dropping implausible energy delta for %s: %s -> %s %s is more than the "
                        "%.1f %s the peak power allows since the counter last moved "
                        "(rejection %d/%d)",
                        self.key,
                        self.lastKnown,
                        val,
                        self.unit,
                        max_delta,
                        self.unit,
                        self._rejected_delta_count,
                        ENERGY_DELTA_REJECT_RECOVERY_COUNT,
                    )
                self._assumed_state = True
                return self.lastKnown
            _LOGGER.warning(
                "Accepting energy value for %s after %d consecutive rejections: %s -> %s %s. The filter will reset to track this as the new baseline.",
                self.key,
                self._rejected_delta_count,
                self.lastKnown,
                val,
                self.unit,
            )
            self._rejected_delta_count = 0
            self._counter_moved_to(val)
            self._assumed_state = False
            return val
        self._rejected_delta_count = 0
        self._counter_moved_to(val)
        self._assumed_state = False
        return val

    async def async_added_to_hass(self) -> None:
        """Call when entity about to be added to hass."""
        await super().async_added_to_hass()
        _LOGGER.debug(f"{self.name} Fetch last known state")
        state = await self.async_get_last_sensor_data()
        if state:
            _LOGGER.debug(f"{self.name} Got last known value from state: {state.native_value}")
            self.last_known_value = state.native_value
            # Also seed lastKnown so the val == 0 fallback and the
            # delta-based plausibility filter work on the very first read
            # after a restart, not only after the second poll.
            if isinstance(state.native_value, (int, float)):
                self.lastKnown = state.native_value
                # And seed the plausibility window from when that value
                # was last written, so the energy made while Home
                # Assistant was down is not measured against one scan
                # interval. Without this the first read after a restart
                # of more than a few minutes was a guaranteed rejection,
                # three polls in a row, until the recovery hatch let it
                # through.
                last_state = await self.async_get_last_state()
                if last_state is not None:
                    self._counter_last_moved = last_state.last_changed
        else:
            _LOGGER.debug(f"{self.name} No previous state was found")
