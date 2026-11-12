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
from .coordinator import SunSpec2ConfigEntry
from .coordinator import SunSpecDataUpdateCoordinator
from .entity import SunSpecEntity
from .entity import get_sunspec_unique_id
from .franklinwh_controls import franklinwh_sensors

# The coordinator serialises all I/O through its per-gateway lock, so
# the platform needs no concurrency limit of its own.
PARALLEL_UPDATES = 0

_LOGGER: logging.Logger = logging.getLogger(__package__)

# The points with a translated name: SunSpec point name to the key under
# ``entity.sensor`` in translations/*.json. Any other point is named by
# the label of its SunSpec model definition, which is English.
#
# A point in a repeating group (``module:0:DCA``) keeps the label with
# its index in front; a translation has no place for the index.
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
    "WHRtg": "rated_storage_capacity",
    # Inverter Settings (model 121)
    "WMax": "max_power_setting",
    "VRef": "voltage_reference",
}

# The models in which the points above mean what their translation says:
# inverters, nameplate and settings, meters, string combiners and the
# DER models. Elsewhere the same name is another quantity, ``A`` a DC
# current in the battery model 802 and ``VA`` a voltage in 64411, so a
# sensor of another model keeps the label of its own definition.
TRANSLATED_NAME_MODELS = frozenset(
    {101, 102, 103, 111, 112, 113, 120, 121}
    | {201, 202, 203, 204, 211, 212, 213, 214, 220}
    | {401, 402, 403, 404, 701, 702, 714}
)

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
    """Return the upper bound for one sensor in its own unit, or ``None`` for no bound.

    The bound is the peak power times the headroom of the point, see
    :data:`MEASURED_POWER_POINT_HEADROOM`.
    """
    # The unit check is not redundant with the name lookup below: model
    # 64411 names a voltage point ``VA``, and only the unit tells that
    # point from an apparent power.
    if max_power_kw is None or unit not in _POWER_UNITS:
        return None
    headroom = measured_power_headroom(key)
    if headroom is None:
        return None
    return max_power_kw * 1000.0 * headroom


def _energy_delta_limit_in_native_unit(
    unit: str | None, max_power_kw: float | None, window_seconds: float | None
) -> float | None:
    """Return the maximum plausible energy delta over ``window_seconds``, in ``unit``.

    The delta is the peak power over the window times
    :data:`ENERGY_DELTA_SAFETY_FACTOR`. Returns ``None`` if ``unit`` is
    not Wh or kWh, or if there is no peak power or no window.
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
    # definitions.
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
    # A bitfield is not an ENUM on purpose. The reason is at the vtype
    # handling in SunSpecSensor.__init__.
    "bitfield16": (None, ICON_DEFAULT, None),
    "bitfield32": (None, ICON_DEFAULT, None),
}

# Some SunSpec units are missing from HA_META on purpose. A sensor with
# one of them gets no unit:
#
#   * Rates ("% WMax/min", "%Max/Sec", "% PF/min", "V/s", "%ARtg/%dV").
#     HA has no compound percent-per-time unit, and an invented one
#     would start a statistics series the recorder cannot convert.
#   * Quantities HA has no unit for: "VAh" (apparent energy), "Ah" /
#     "AH" (charge).
#   * Things that are not measurements: "SF" (scale factor), "Tms"
#     (a SunSpec timestamp, not a duration), "YYYYMMDD".

#: Energy points that hold an amount in a store right now, not a counter
#: that only grows: the energy available in a DER storage (model 713) moves
#: with the state of charge. A lifetime guard would hold every discharge
#: back as a counter running backwards (#120).
STORED_ENERGY_POINTS: frozenset[str] = frozenset({"WHAvail"})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunSpec2ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform and keep adding sensors as models appear.

    A coordinator listener runs after every update cycle and adds an
    entity for each (model_id, key, model_index) whose unique id it has
    not seen. A model that an inverter starts serving after a firmware
    update gets its sensors that way, without a restart
    (cjne/ha-sunspec#200). Entities of a model that vanishes from the
    device are not removed (cjne/ha-sunspec#202): the coordinator
    raises a Repairs issue once the model has stayed missing for
    ``STALE_MODEL_TOLERANCE_SECONDS``.
    """
    # The role-named model 160 sensors subclass the sensors below, so
    # their module cannot be imported before this one is complete.
    from .dc_channels import dc_channel_sensors
    from .fronius_web_entities import fronius_web_sensors
    from .lifetime_energy import CountedDailyEnergySensor
    from .lifetime_energy import CountedLifetimeEnergySensor
    from .vendor_blocks import raw_block_sensors

    coordinator = entry.runtime_data
    # device_info is the common model (model 1) the coordinator cached
    # in its update cycle. Platform setup does no Modbus I/O of its own.
    device_info = coordinator.device_info
    prefix = entry.options.get(CONF_PREFIX, entry.data.get(CONF_PREFIX, ""))

    known_unique_ids: set[str] = set()
    # Energy points that count one day, not the lifetime. They get no
    # lifetime guards: those would hold yesterday's total against every
    # morning's restart from zero and log a warning for it, while Home
    # Assistant's total_increasing already reads a drop as a new cycle.
    daily_points = coordinator.vendor.daily_energy_points if coordinator.vendor else frozenset()

    @callback
    def _async_add_new_sensors() -> None:
        """Add a sensor for every point in coordinator.data that has none yet."""
        if coordinator.data is None:
            return
        new_sensors: list[SensorEntity] = []
        for model_id, model_wrapper in coordinator.data.items():
            for key in model_wrapper.getKeys():
                if is_excluded_sensor_point(model_id, key):
                    # The point is a write control. The comment above
                    # is_excluded_sensor_point says why a sensor next
                    # to the control does harm.
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
                    if key in daily_points:
                        new_sensors.append(CountedDailyEnergySensor(coordinator, entry, data))
                    elif (
                        device_class == SensorDeviceClass.ENERGY
                        and key.split(":")[-1] not in STORED_ENERGY_POINTS
                    ):
                        new_sensors.append(SunSpecEnergySensor(coordinator, entry, data))
                    else:
                        new_sensors.append(SunSpecSensor(coordinator, entry, data))
                    if key in daily_points and device_info is not None:
                        # The total the daily counter cannot give.
                        counted = CountedLifetimeEnergySensor(
                            coordinator,
                            entry,
                            device_info,
                            model_wrapper,
                            prefix,
                            model_id,
                            model_index,
                            key,
                        )
                        counted_uid = counted.unique_id
                        if counted_uid is not None and counted_uid not in known_unique_ids:
                            known_unique_ids.add(counted_uid)
                            new_sensors.append(counted)
        mppt_wrapper = coordinator.data.get(160)
        vendor_sensors: list[SensorEntity] = []
        if mppt_wrapper is not None and device_info is not None:
            vendor_sensors.extend(
                dc_channel_sensors(coordinator, entry, device_info, mppt_wrapper, prefix)
            )
        # The vendor's registers outside the models. A block can start
        # answering later, so this runs on every cycle like the models.
        vendor_sensors.extend(raw_block_sensors(coordinator, entry, prefix))
        vendor_sensors.extend(franklinwh_sensors(coordinator, entry, prefix))
        for sensor in vendor_sensors:
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

    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_sensors))
    _async_add_new_sensors()
    # The web interface sensors are fed by their own coordinator and
    # are not part of the Modbus re-detection above. Their set is known
    # at setup.
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
        # An unknown unit resolves to no unit, not to the raw SunSpec
        # string. state_class returns MEASUREMENT for anything with a
        # unit, so a raw string would start a long-term statistics
        # series under a unit the recorder cannot convert or merge. The
        # value stays visible without a unit.
        ha_meta = HA_META.get(sunspec_unit, (None, ICON_DEFAULT, None))
        self.unit = ha_meta[0]
        self.use_icon = ha_meta[1]
        self.use_device_class = ha_meta[2]
        if self.key.split(":")[-1] in STORED_ENERGY_POINTS:
            self.use_device_class = SensorDeviceClass.ENERGY_STORAGE
        self._options: list[str] = []
        # lastKnown is the last accepted reading of an energy sensor:
        # the value reported for a read of 0 or None, and the baseline
        # of the delta plausibility filter. The value comes from
        # pysunspec2 through an untyped getValue, so the isinstance
        # checks in the filter are not redundant with this annotation.
        self.lastKnown: float | None = None
        self._assumed_state = False

        self._unique_id = get_sunspec_unique_id(
            config_entry.entry_id, self.key, self.model_id, self.model_index
        )

        vtype = self._meta["type"]
        # A bitfield is a set of flags, an enum is one of N states, and
        # only the enum is what HA's ENUM device class describes. An
        # inverter reporting two events at once renders as
        # "GROUND_FAULT,DC_OVER_VOLT", which is in no list of options,
        # and HA validates an ENUM state against its options strictly:
        # async_write_ha_state raises ValueError (cjne/ha-sunspec#370).
        # Listing the combinations is not an option either: n flags give
        # 2**n states, and Evt1 of model 103 has 32 flags.
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
            # Without a device class there are no options and no state
            # validation. The decoded flag names stay in the state
            # string, which template sensors are built on, and
            # extra_state_attributes offers them as a list.
            self.use_device_class = None

        self._device_id = config_entry.entry_id
        # The coordinator's logger puts host:port#unit_id on the warnings
        # from native_value. The module logger is the fallback for the
        # tests' stub coordinator, which has no _log attribute (see
        # tests/__init__.py:MockSunSpecDataUpdateCoordinator).
        self._log = getattr(coordinator, "_log", _LOGGER)
        # Consecutive reads the power plausibility filter rejected. The
        # count drives the log throttle and the "back inside the
        # ceiling" line in native_value.
        self._implausible_rejections = 0
        # The name for a point without a translation: the label of
        # the SunSpec model definition, with the group and index in
        # front for a point in a repeating group ("Module 0 DC Current")
        # and the model index for a second model of the same id. The
        # user's prefix is not part of it. The prefix is on the device
        # name, see SunSpecEntity.device_info.
        desc = self._meta.get("label", self.key)
        if self.unit == UnitOfElectricCurrent.AMPERE and "DC" in desc:
            self.use_icon = ICON_DC_AMPS

        name_parts: list[str] = []
        key_parts = self.key.split(":")
        if len(key_parts) > 1:
            # "module:0:DCA" gets "Module 0" in front of the label.
            group_label = key_parts[0].replace("_", " ").title()
            name_parts.append(f"{group_label} {key_parts[1]}")
        elif self.model_index > 0:
            # A second model of the same id keeps its index in the name.
            name_parts.append(str(self.model_index))
        name_parts.append(desc)
        self._name = " ".join(name_parts)

        # The translation key also picks the icon (icons.json), so a
        # point gets it in every model. Whether it also names the sensor
        # depends on the model, see TRANSLATED_NAME_MODELS.
        self._name_is_translated = False
        if ":" not in self.key:
            vendor = coordinator.vendor
            if vendor is not None and self.key in vendor.daily_energy_points:
                self._attr_translation_key = "daily_energy"
                self._name_is_translated = True
            elif (translation_key := SUNSPEC_POINT_TRANSLATION_KEYS.get(self.key)) is not None:
                self._attr_translation_key = translation_key
                self._name_is_translated = self.model_id in TRANSLATED_NAME_MODELS

        # Temperatures, state enums and event bitfields are diagnostic:
        # Home Assistant lists them under "Diagnostic" on the device
        # page, apart from power, energy, current and voltage.
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

        # The vendor event bitfields are noisy and the nameplate and
        # settings registers do not change, so these sensors start
        # disabled in the entity registry.
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

    @property
    def options(self) -> list[str] | None:
        if self.device_class != SensorDeviceClass.ENUM:
            return None
        return self._options

    @property
    def name(self) -> str:
        """Return the translated name where the point has one, else its SunSpec label.

        A second model with the same id keeps its index in front of
        either name.
        """
        translated = super().name if self._name_is_translated else None
        if not isinstance(translated, str):
            return self._name
        return f"{self.model_index} {translated}" if self.model_index > 0 else translated

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return self._unique_id

    @property
    def assumed_state(self) -> bool:
        return self._assumed_state

    @property
    def _peak_power_kw(self) -> float | None:
        """Return the peak AC power to measure plausibility against, in kW.

        The configured value wins. Without one, the nameplate the
        coordinator detected from model 120 / 121 stands in, with
        :data:`NAMEPLATE_FILTER_HEADROOM` on top.
        """
        # The peak is resolved on every read and not in ``__init__``,
        # because the nameplate arrives with the first successful cycle,
        # which can be after the entity was built.
        return effective_peak_power_kw(
            self.coordinator.entry.options.get(CONF_MAX_AC_POWER_KW),
            getattr(self.coordinator, "detected_max_ac_power_kw", None),
        )

    @property
    def _peak_power_source(self) -> str:
        """Return where the ceiling came from, for the rejection log line.

        The line has to name the source: a user who reads of a peak in
        the log and finds the options field empty cannot otherwise tell
        that the ceiling is the detected nameplate (#45).
        """
        if self.coordinator.entry.options.get(CONF_MAX_AC_POWER_KW):
            return "configured peak AC power"
        source = getattr(self.coordinator, "detected_max_ac_power_source", None) or "model 120/121"
        return f"auto-detected nameplate from {source} x {NAMEPLATE_FILTER_HEADROOM}"

    @property
    def _max_native_value(self) -> float | None:
        """Return the upper plausibility bound in this sensor's own unit, or None."""
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
        if val is None and self.translation_key == "daily_energy":
            # SunSpec reserves 0 of an acc32 for "not implemented", and
            # pysunspec2 reads it as None. A daily counter sits at exactly
            # that value from its restart in the morning until the first
            # watt-hour (#109).
            return 0
        # The plausibility filter for measured power points drops a
        # reading beyond the ceiling. Inverters at dawn and dusk
        # sometimes report MW-range garbage that poisons long-term
        # statistics.
        #
        # The comparison is on abs() because the garbage is not signed:
        # a misread scale factor or a shifted register lands anywhere,
        # and meter W, battery 802 W and DER 714 DCW are legitimately
        # bipolar (#45).
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
                if val is None:
                    # A vendor transform answers None for a value it knows
                    # to be not implemented.
                    return None
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
        """Return the device class of the sensor."""
        return self.use_device_class

    @property
    def state_class(self) -> SensorStateClass | None:
        """Return the state class: none without a unit, total_increasing for energy."""
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
            # A model can be missing from a cycle (see native_value).
            wrapper = self.coordinator.data.get(self.model_id)
            attrs["raw"] = (
                wrapper.getValue(self.key, self.model_index) if wrapper is not None else None
            )
        if self._is_bitfield:
            # The state string is awkward to parse. active_flags gives
            # templates and automations the same flags as a list.
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
        # Consecutive reads the delta plausibility filter rejected. At
        # ENERGY_DELTA_REJECT_RECOVERY_COUNT the new value is accepted,
        # for the reason given at that constant.
        self._rejected_delta_count = 0
        # When ``lastKnown`` last took a new value. The delta filter
        # measures a jump against the energy the inverter can have made
        # since that moment, not since the previous poll: a counter that
        # stands still for three polls and then moves by three polls'
        # worth reports correctly (#45, a Fronius GEN24 updates WH every
        # few minutes).
        self._counter_last_moved: datetime | None = None

    @property
    def _max_native_delta(self) -> float | None:
        """Return the maximum plausible increase since the counter last moved, in this unit.

        The window is the time since ``lastKnown`` last changed, floored
        at the scan interval. ``None`` disables the check: the sensor
        is not an energy counter, or there is no peak power to go on.
        """
        # The floor is one scan interval because the first read after a
        # change comes one poll later at the earliest. The floor is also
        # the window while nothing is known about the past, before the
        # first read of a run without restored state.
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
            # No reading this cycle: the model is missing from the
            # coordinator's data, or the point could not be computed.
            # The last value is held, as for a read of 0. Falling
            # through would set lastKnown to None, and the next good
            # read would find no baseline and be discarded as well.
            self._assumed_state = True
            return self.lastKnown
        # A 0 would reset the total_increasing statistics of an energy
        # sensor, so the last known value is reported instead.
        if val == 0:
            _LOGGER.debug(
                "Returning last known value instead of 0 for %s to avoid resetting the "
                "total_increasing counter",
                self.name,
            )
            self._assumed_state = True
            return self.lastKnown
        # The filter is active but has no baseline yet: a fresh setup,
        # or a restart whose restored state was not numeric. The first
        # read becomes the baseline and is not reported, so the next
        # read is checked against it before a value reaches the
        # statistics.
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
        # The delta check: an increase that implies a power above the
        # peak over the time since the counter last moved is garbage,
        # and the last known value is reported, as for a read of 0.
        #
        # Consecutive rejections are counted. A jump the inverter keeps
        # reporting is a legitimate discontinuity (a restored baseline
        # older than its timestamp says, a counter that was reset), and
        # after ENERGY_DELTA_REJECT_RECOVERY_COUNT rejections the value
        # is accepted. Without the count the sensor would stay on
        # lastKnown, which is not updated while the filter rejects.
        #
        # A drop is rejected the same way, and without a peak power to go
        # on: a lifetime counter has no legitimate way down. The one seen
        # in the wild is a scale factor misread on a Fronius Symo, model
        # 160, which reports the total a thousand times too small for a
        # poll or two. Passed through, HA books the drop as a meter
        # reset, and when the real value returns, the jump back is
        # rejected by the peak-power rule until the count accepts it,
        # and the whole lifetime total lands in the statistics as new
        # energy. A counter that really was reset is accepted through
        # the same count.
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
            # lastKnown is seeded as well, so the fallback for a read of
            # 0 and the delta filter work on the first read after a
            # restart.
            if isinstance(state.native_value, (int, float)):
                self.lastKnown = state.native_value
                # The plausibility window starts when that value last
                # changed, so the energy made while Home Assistant was
                # down is not measured against one scan interval. With
                # the window unseeded, the first reads after a longer
                # restart are rejected until the recovery count lets
                # them through.
                last_state = await self.async_get_last_state()
                if last_state is not None:
                    self._counter_last_moved = last_state.last_changed
        else:
            _LOGGER.debug(f"{self.name} No previous state was found")
