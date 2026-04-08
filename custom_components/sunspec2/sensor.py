"""Sensor platform for SunSpec."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import RestoreSensor
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import DEGREE
from homeassistant.const import PERCENTAGE
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
from homeassistant.const import UnitOfReactivePower
from homeassistant.const import UnitOfSpeed
from homeassistant.const import UnitOfTemperature
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SunSpec2ConfigEntry
from . import get_sunspec_unique_id
from .const import CONF_MAX_AC_POWER_KW
from .const import CONF_PREFIX
from .const import CONF_SCAN_INTERVAL
from .const import DOMAIN
from .const import ENERGY_DELTA_SAFETY_FACTOR
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


def _power_limit_in_native_unit(unit, max_power_kw: float | None) -> float | None:
    """Convert the configured peak power (kW) to the sensor's native unit.

    All SunSpec power-like units (W, VA, VAr) are 1:1 with watts in HA, so
    the same kW-to-W conversion applies. Returns ``None`` if the sensor is
    not power-like, which disables the filter for that sensor instance.
    """
    if max_power_kw is None or unit not in _POWER_UNITS:
        return None
    return max_power_kw * 1000.0


def _energy_delta_limit_in_native_unit(
    unit, max_power_kw: float | None, scan_interval_seconds: float | None
) -> float | None:
    """Compute the maximum plausible energy delta between two consecutive reads.

    Derived from the configured peak power and the scan interval, with the
    safety factor in :data:`ENERGY_DELTA_SAFETY_FACTOR`. Returns ``None``
    if the sensor is not a known energy unit or no peak power is configured.
    """
    if max_power_kw is None or scan_interval_seconds is None:
        return None
    max_delta_kwh = max_power_kw * (scan_interval_seconds / 3600.0) * ENERGY_DELTA_SAFETY_FACTOR
    if unit == UnitOfEnergy.WATT_HOUR:
        return max_delta_kwh * 1000.0
    if unit == UnitOfEnergy.KILO_WATT_HOUR:
        return max_delta_kwh
    return None


HA_META = {
    "A": [UnitOfElectricCurrent.AMPERE, ICON_AC_AMPS, SensorDeviceClass.CURRENT],
    "HPa": [UnitOfPressure.HPA, ICON_DEFAULT, None],
    "Hz": [UnitOfFrequency.HERTZ, ICON_FREQ, None],
    "Mbps": [UnitOfDataRate.MEGABITS_PER_SECOND, ICON_DEFAULT, None],
    "V": [UnitOfElectricPotential.VOLT, ICON_VOLT, SensorDeviceClass.VOLTAGE],
    "VA": [UnitOfApparentPower.VOLT_AMPERE, ICON_POWER, None],
    "VAr": [UnitOfReactivePower.VOLT_AMPERE_REACTIVE, ICON_POWER, None],
    "W": [UnitOfPower.WATT, ICON_POWER, SensorDeviceClass.POWER],
    "W/m2": [UnitOfIrradiance.WATTS_PER_SQUARE_METER, ICON_DEFAULT, None],
    "Wh": [UnitOfEnergy.WATT_HOUR, ICON_ENERGY, SensorDeviceClass.ENERGY],
    "WH": [UnitOfEnergy.WATT_HOUR, ICON_ENERGY, SensorDeviceClass.ENERGY],
    "bps": [UnitOfDataRate.BITS_PER_SECOND, ICON_DEFAULT, None],
    "deg": [DEGREE, ICON_TEMP, SensorDeviceClass.TEMPERATURE],
    "Degrees": [DEGREE, ICON_TEMP, SensorDeviceClass.TEMPERATURE],
    "C": [UnitOfTemperature.CELSIUS, ICON_TEMP, SensorDeviceClass.TEMPERATURE],
    "kWh": [UnitOfEnergy.KILO_WATT_HOUR, ICON_ENERGY, SensorDeviceClass.ENERGY],
    "m/s": [UnitOfSpeed.METERS_PER_SECOND, ICON_DEFAULT, None],
    "mSecs": [UnitOfTime.MILLISECONDS, ICON_DEFAULT, None],
    "meters": [UnitOfLength.METERS, ICON_DEFAULT, None],
    "mm": [UnitOfLength.MILLIMETERS, ICON_DEFAULT, None],
    "%": [PERCENTAGE, ICON_DEFAULT, None],
    "Secs": [UnitOfTime.SECONDS, ICON_DEFAULT, None],
    "enum16": [None, ICON_DEFAULT, SensorDeviceClass.ENUM],
    "bitfield32": [None, ICON_DEFAULT, SensorDeviceClass.ENUM],
}


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
        new_sensors: list[SunSpecSensor] = []
        for model_id, model_wrapper in coordinator.data.items():
            for key in model_wrapper.getKeys():
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
                    ha_meta = HA_META.get(sunspec_unit, [sunspec_unit, None, None])
                    device_class = ha_meta[2]
                    if device_class == SensorDeviceClass.ENERGY:
                        new_sensors.append(SunSpecEnergySensor(coordinator, entry, data))
                    else:
                        new_sensors.append(SunSpecSensor(coordinator, entry, data))
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


class SunSpecSensor(SunSpecEntity, SensorEntity):
    """sunspec Sensor class."""

    def __init__(
        self,
        coordinator,
        config_entry: ConfigEntry,
        data: dict[str, Any],
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            data["device_info"],
            data["model"].getGroupMeta(),
            prefix=data["prefix"],
        )
        self.model_id = data["model_id"]
        self.model_index = data["model_index"]
        self.model_wrapper = data["model"]
        self.key = data["key"]
        self._meta = self.model_wrapper.getMeta(self.key)
        self._group_meta = self.model_wrapper.getGroupMeta()
        self._point_meta = self.model_wrapper.getPoint(self.key).pdef
        sunspec_unit = self._meta.get("units", self._meta.get("type", ""))
        ha_meta = HA_META.get(sunspec_unit, [sunspec_unit, ICON_DEFAULT, None])
        self.unit = ha_meta[0]
        self.use_icon = ha_meta[1]
        self.use_device_class = ha_meta[2]
        self._options = []
        # Used if this is an energy sensor and the read value is 0
        # Updated whenever the value read is not 0
        self.lastKnown = None
        self._assumed_state = False

        # Plausibility filter: if the user configured a peak AC power, drop
        # power-like readings that exceed it. Stored in the sensor's native
        # unit so the per-update check is a single comparison.
        max_power_kw = config_entry.options.get(CONF_MAX_AC_POWER_KW)
        self._max_native_value = _power_limit_in_native_unit(self.unit, max_power_kw)

        self._unique_id = get_sunspec_unique_id(
            config_entry.entry_id, self.key, self.model_id, self.model_index
        )

        vtype = self._meta["type"]
        if vtype in ("enum16", "bitfield32"):
            self._options = self._point_meta.get("symbols", None)
            if self._options is None:
                self.use_device_class = None
            else:
                self.use_device_class = SensorDeviceClass.ENUM
                self._options = [item["name"] for item in self._options]
                self._options.append("")

        self._device_id = config_entry.entry_id
        # Use the coordinator's context-bound logger when available so warnings
        # from native_value carry host:port#unit_id automatically. Fallback to
        # the module logger for tests that supply a stub coordinator without
        # an _log attribute (see tests/__init__.py:MockSunSpecDataUpdateCoordinator).
        self._log = getattr(coordinator, "_log", _LOGGER)
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
        if self.key in diagnostic_keys or self.use_device_class == SensorDeviceClass.ENUM:
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
        # Plausibility filter for power-like sensors: drop readings above
        # the configured peak. Inverters at dawn / dusk sometimes report
        # MW-range garbage that poisons long-term statistics.
        if (
            self._max_native_value is not None
            and isinstance(val, (int, float))
            and val > self._max_native_value
        ):
            self._log.warning(
                "Dropping implausible value for %s: %s %s exceeds configured peak %s %s",
                self.key,
                val,
                self.unit,
                self._max_native_value,
                self.unit,
            )
            return None
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
    def state_class(self):
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
        if vtype in ("enum16", "bitfield32"):
            attrs["raw"] = self.coordinator.data[self.model_id].getValue(self.key, self.model_index)
        return attrs


class SunSpecEnergySensor(SunSpecSensor, RestoreSensor):
    def __init__(self, coordinator, config_entry: ConfigEntry, data: dict[str, Any]) -> None:
        super().__init__(coordinator, config_entry, data)
        self.last_known_value: Any = None

        # Plausibility filter: derive the maximum plausible delta between
        # two consecutive reads from the configured peak power and the
        # scan interval. ``None`` disables the check.
        max_power_kw = config_entry.options.get(CONF_MAX_AC_POWER_KW)
        scan_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, config_entry.data.get(CONF_SCAN_INTERVAL)
        )
        self._max_native_delta = _energy_delta_limit_in_native_unit(
            self.unit, max_power_kw, scan_interval
        )

    @property
    def native_value(self) -> Any:
        val = super().native_value
        # For an energy sensor a value of 0 woulld mess up long term stats because of how total_increasing works
        if val == 0:
            _LOGGER.debug(
                "Returning last known value instead of 0 for {self.name) to avoid resetting total_increasing counter"
            )
            self._assumed_state = True
            return self.lastKnown
        # Plausibility filter active but no baseline yet (e.g. fresh setup,
        # or restart where the restored state was not numeric): discard
        # this read so a potential garbage value never becomes the baseline.
        # The next poll will have a valid lastKnown to compare against.
        if (
            val is not None
            and self._max_native_delta is not None
            and self.lastKnown is None
            and isinstance(val, (int, float))
        ):
            _LOGGER.info(
                "Establishing energy baseline for %s, discarding first read %s %s",
                self.key,
                val,
                self.unit,
            )
            self.lastKnown = val
            self._assumed_state = True
            return None
        # Delta-based plausibility check: if the increase since the last
        # known value would imply a power above the configured peak, treat
        # the read as garbage and fall back to the last known value (same
        # mechanism as the val == 0 path, so total_increasing stats stay
        # intact).
        if (
            val is not None
            and self._max_native_delta is not None
            and self.lastKnown is not None
            and isinstance(val, (int, float))
            and isinstance(self.lastKnown, (int, float))
            and (val - self.lastKnown) > self._max_native_delta
        ):
            _LOGGER.warning(
                "Dropping implausible energy delta for %s: %s -> %s %s exceeds max plausible delta %s %s",
                self.key,
                self.lastKnown,
                val,
                self.unit,
                self._max_native_delta,
                self.unit,
            )
            self._assumed_state = True
            return self.lastKnown
        self.lastKnown = val
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
        else:
            _LOGGER.debug(f"{self.name} No previous state was found")
