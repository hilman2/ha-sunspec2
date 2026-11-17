"""Which translation names a SunSpec point, model by model.

A SunSpec sensor gets its name from ``entity.sensor`` in
translations/*.json through a translation key. The key depends on the
model, not only on the point: the same point name is another quantity
elsewhere, ``A`` the AC current of an inverter and the DC current of the
battery model 802, ``VA`` the apparent power of an inverter and a voltage
in 64411. The tables below are therefore one per family of models that
define their points alike, and ``POINT_NAMES`` assigns them to models.

Every point a sensor can be built for is named in the models the
integration reads without being asked: the ones ``DEFAULT_MODELS`` ticks,
the control models it polls for its write entities, the models a vendor
profile requires, and the nameplate, settings and status models 120 to
122 that most inverters serve. The options form offers any model the
device has, and a sensor of a model without a table keeps the label of
its SunSpec model definition, which is English.

A name can hold placeholders, which the table fills in: ``{phase}`` (L1,
L2 or L3), ``{quadrant}`` (Q1 to Q4) and, for a point in a repeating
group, ``{index}``, the index of its group in the flattened key of
models.py. The index starts at 0, like the label the sensor had before
it was translated.

A name change does not touch an existing entity id: Home Assistant keeps
the id an entity was registered with. A new sensor gets its id from its
name in the system language.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import NamedTuple

_NO_PLACEHOLDERS: Mapping[str, str] = MappingProxyType({})

PHASES = ("L1", "L2", "L3")


class PointName(NamedTuple):
    """The translation key that names a point, and the values of its placeholders."""

    key: str
    placeholders: Mapping[str, str] = _NO_PLACEHOLDERS


def _per_phase(key: str, *points: str) -> dict[str, PointName]:
    """Name the L1, L2 and L3 point of one quantity, in that order, with one key."""
    return {
        point: PointName(key, {"phase": phase}) for point, phase in zip(points, PHASES, strict=True)
    }


def _per_quadrant(key: str, points: str) -> dict[str, PointName]:
    """Name the four quadrant points of one quantity with one key.

    Args:
        key: The translation key.
        points: The point name with ``{q}`` where the quadrant number goes,
            e.g. ``"VArRtgQ{q}"``.
    """
    return {
        points.format(q=number): PointName(key, {"quadrant": f"Q{number}"})
        for number in range(1, 5)
    }


def _meter_reactive_energy() -> dict[str, PointName]:
    """The reactive energy counters of the meter models, per quadrant and per phase.

    A meter imports in quadrants 1 and 2 and exports in 3 and 4. The
    integer models 201 to 204 write a phase as ``PhA``, their float twins
    211 to 214 as ``phA``, so both spellings get an entry.
    """
    names: dict[str, PointName] = {}
    for direction, key, quadrants in (
        ("Imp", "reactive_energy_imported", ("Q1", "Q2")),
        ("Exp", "reactive_energy_exported", ("Q3", "Q4")),
    ):
        for quadrant in quadrants:
            names[f"TotVArh{direction}{quadrant}"] = PointName(key, {"quadrant": quadrant})
            for phase, letter in zip(PHASES, "ABC", strict=True):
                for spelling in ("Ph", "ph"):
                    names[f"TotVArh{direction}{quadrant}{spelling}{letter}"] = PointName(
                        f"{key}_phase", {"quadrant": quadrant, "phase": phase}
                    )
    return names


# Inverters 101 to 103 and 111 to 113, nameplate 120 and settings 121.
# The meters, string combiners and DER models with the same point names
# take this table too, see POINT_NAMES.
_INVERTER: dict[str, str | PointName] = {
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
    "WRtg": "rated_power",
    "VARtg": "rated_apparent_power",
    "ARtg": "rated_current",
    "WHRtg": "rated_storage_capacity",
    "WMax": "max_power_setting",
    "VRef": "voltage_reference",
}

_NAMEPLATE: dict[str, str | PointName] = {
    "DERTyp": "der_type",
    **_per_quadrant("rated_reactive_power", "VArRtgQ{q}"),
    **_per_quadrant("rated_min_power_factor", "PFRtgQ{q}"),
    "AhrRtg": "rated_usable_capacity",
    "MaxChaRte": "rated_max_charge_power",
    "MaxDisChaRte": "rated_max_discharge_power",
}

_SETTINGS: dict[str, str | PointName] = {
    "VRefOfs": "voltage_reference_offset",
    "VMax": "max_voltage_setting",
    "VMin": "min_voltage_setting",
    "VAMax": "max_apparent_power_setting",
    **_per_quadrant("max_reactive_power_setting", "VArMaxQ{q}"),
    "WGra": "active_power_ramp_rate",
    **_per_quadrant("min_power_factor_setting", "PFMinQ{q}"),
    "VArAct": "reactive_power_action",
    "ClcTotVA": "apparent_power_calculation",
    "MaxRmpRte": "max_ramp_rate",
    "ECPNomHz": "nominal_frequency",
    "ConnPh": "connected_phase",
}

_STATUS: dict[str, str | PointName] = {
    "ActWh": "lifetime_energy",
    "ActVAh": "lifetime_apparent_energy",
    **_per_quadrant("lifetime_reactive_energy", "ActVArhQ{q}"),
    "VArAval": "available_reactive_power",
    "WAval": "available_power",
    "StSetLimMsk": "setpoint_limits_reached",
    "StActCtl": "active_controls",
    "Tms": "timestamp",
    "Ris": "isolation_resistance",
}

# The points of 123 that are not a control of their own. The integration
# calls WMaxLimPct the export limit, so its timers carry that name.
_IMMEDIATE_CONTROLS: dict[str, str | PointName] = {
    "Conn_WinTms": "grid_connection_time_window",
    "Conn_RvrtTms": "grid_connection_revert_time",
    "WMaxLimPct_WinTms": "export_limit_time_window",
    "WMaxLimPct_RmpTms": "export_limit_ramp_time",
    "OutPFSet_WinTms": "power_factor_time_window",
    "OutPFSet_RvrtTms": "power_factor_revert_time",
    "OutPFSet_RmpTms": "power_factor_ramp_time",
    "VArWMaxPct": "reactive_power_limit_pct_max_power",
    "VArMaxPct": "reactive_power_limit_pct_max_reactive_power",
    "VArAvalPct": "reactive_power_limit_pct_available",
    "VArPct_WinTms": "reactive_power_limit_time_window",
    "VArPct_RvrtTms": "reactive_power_limit_revert_time",
    "VArPct_RmpTms": "reactive_power_limit_ramp_time",
    "VArPct_Mod": "reactive_power_limit_mode",
    "VArPct_Ena": "reactive_power_limit_enabled",
}

_STORAGE: dict[str, str | PointName] = {
    "WChaGra": "charge_ramp_rate",
    "WDisChaGra": "discharge_ramp_rate",
    "VAChaMax": "max_charge_apparent_power_setting",
    "ChaState": "state_of_charge",
    "StorAval": "available_capacity",
    "InBatV": "internal_battery_voltage",
    "ChaSt": "charge_status",
    "InOutWRte_WinTms": "battery_rate_time_window",
    "InOutWRte_RmpTms": "battery_rate_ramp_time",
}

_MPPT: dict[str, str | PointName] = {
    "Evt": "global_events",
    "module:DCA": "module_dc_current",
    "module:DCV": "module_dc_voltage",
    "module:DCW": "module_dc_power",
    "module:DCWH": "module_lifetime_energy",
    "module:Tms": "module_timestamp",
    "module:Tmp": "module_temperature",
    "module:DCSt": "module_operating_state",
    "module:DCEvt": "module_events",
}

_METER: dict[str, str | PointName] = {
    "PhV": "average_voltage_l_n",
    "PPV": "average_voltage_l_l",
    # 202 to 204 spell the line-to-line voltages PhVph, where 201 and
    # the float twins spell them PPVph as the inverters do.
    "PhVphAB": "phase_voltage_l1_l2",
    "PhVphBC": "phase_voltage_l2_l3",
    "PhVphCA": "phase_voltage_l3_l1",
    "VAR": "reactive_power",
    **_per_phase("watts_phase", "WphA", "WphB", "WphC"),
    **_per_phase("apparent_power_phase", "VAphA", "VAphB", "VAphC"),
    **_per_phase("reactive_power_phase", "VARphA", "VARphB", "VARphC"),
    **_per_phase("power_factor_phase", "PFphA", "PFphB", "PFphC"),
    "TotWhExp": "energy_exported",
    **_per_phase("energy_exported_phase", "TotWhExpPhA", "TotWhExpPhB", "TotWhExpPhC"),
    "TotWhImp": "energy_imported",
    **_per_phase("energy_imported_phase", "TotWhImpPhA", "TotWhImpPhB", "TotWhImpPhC"),
    "TotVAhExp": "apparent_energy_exported",
    **_per_phase("apparent_energy_exported_phase", "TotVAhExpPhA", "TotVAhExpPhB", "TotVAhExpPhC"),
    "TotVAhImp": "apparent_energy_imported",
    **_per_phase("apparent_energy_imported_phase", "TotVAhImpPhA", "TotVAhImpPhB", "TotVAhImpPhC"),
    **_meter_reactive_energy(),
    "Evt": "events",
}

_BASE_MET: dict[str, str | PointName] = {
    "TmpAmb": "ambient_temperature",
    "RH": "relative_humidity",
    "Pres": "barometric_pressure",
    "WndSpd": "wind_speed",
    "WndDir": "wind_direction",
    "Rain": "rainfall",
    "Snw": "snow_depth",
    "ElecFld": "electric_field",
    "SurWet": "surface_wetness",
    "SoilWet": "soil_moisture",
}

_MINI_MET: dict[str, str | PointName] = {
    "GHI": "global_horizontal_irradiance",
    "TmpBOM": "module_back_temperature",
    "TmpAmb": "ambient_temperature",
    "WndSpd": "wind_speed",
}

_STRING_COMBINER: dict[str, str | PointName] = {
    "DCAMax": "rated_dc_current",
    "Evt": "events",
    "EvtVnd": "vendor_events",
    "DCAhr": "amp_hours",
    "Tmp": "temperature",
    "DCPR": "performance_ratio",
    "DCWh": "dc_energy",
    "string:InEvt": "string_events",
    "string:InEvtVnd": "string_vendor_events",
    "string:EvtVnd": "string_vendor_events",
    "string:InDCA": "string_dc_current",
    "string:InDCAhr": "string_amp_hours",
    "string:InDCV": "string_dc_voltage",
    "string:InDCW": "string_dc_power",
    "string:InDCWh": "string_dc_energy",
    "string:InDCPR": "string_performance_ratio",
}

_PANEL: dict[str, str | PointName] = {
    "Stat": "status",
    "StatVend": "vendor_status",
    "Evt": "events",
    "EvtVend": "vendor_events",
    "Ctl": "control_mode",
    "Tms": "timestamp",
    "OutA": "output_current",
    "OutV": "output_voltage",
    "OutWh": "output_energy",
    "OutW": "output_power",
    "OutPw": "output_power",
    "Tmp": "temperature",
    "InA": "input_current",
    "InV": "input_voltage",
    "InWh": "input_energy",
    "InW": "input_power",
}

_TRACKER: dict[str, str | PointName] = {
    "Typ": "tracker_type",
    "DtLoc": "local_date",
    "TmLoc": "local_time",
    "GlblElCtl": "manual_elevation",
    "GlblAzCtl": "manual_azimuth",
    "GlblCtl": "global_mode",
    "tracker:ElTrgt": "tracker_target_elevation",
    "tracker:AzTrgt": "tracker_target_azimuth",
    "tracker:ElPos": "tracker_elevation",
    "tracker:AzPos": "tracker_azimuth",
    "tracker:ElCtl": "tracker_manual_elevation",
    "tracker:AzCtl": "tracker_manual_azimuth",
    "tracker:Ctl": "tracker_mode",
}

# Model 701 counts energy as injected into the grid and absorbed from it,
# where a meter imports and exports. The directions agree for active
# energy, but the quadrants of reactive energy do not: the DER counts
# injected reactive energy in quadrants 1 and 2, a meter imports there.
# So the DER keeps names of its own.
_DER_MEASUREMENT: dict[str, str | PointName] = {
    "ACType": "ac_wiring_type",
    "InvSt": "inverter_state",
    "ConnSt": "grid_connection_state",
    "Alrm": "alarms",
    "DERMode": "der_mode",
    "Var": "reactive_power",
    "LLV": "average_voltage_l_l",
    "LNV": "average_voltage_l_n",
    "TotWhInj": "energy_injected",
    "TotWhAbs": "energy_absorbed",
    "TotVarhInj": "reactive_energy_injected",
    "TotVarhAbs": "reactive_energy_absorbed",
    "TmpAmb": "ambient_temperature",
    "TmpSw": "switch_temperature",
    "ThrotPct": "throttling",
    "ThrotSrc": "throttling_source",
    **_per_phase("watts_phase", "WL1", "WL2", "WL3"),
    **_per_phase("apparent_power_phase", "VAL1", "VAL2", "VAL3"),
    **_per_phase("reactive_power_phase", "VarL1", "VarL2", "VarL3"),
    "AL1": "amps_l1",
    "AL2": "amps_l2",
    "AL3": "amps_l3",
    "VL1L2": "phase_voltage_l1_l2",
    "VL2L3": "phase_voltage_l2_l3",
    "VL3L1": "phase_voltage_l3_l1",
    "VL1": "phase_voltage_l1_n",
    "VL2": "phase_voltage_l2_n",
    "VL3": "phase_voltage_l3_n",
    **_per_phase("energy_injected_phase", "TotWhInjL1", "TotWhInjL2", "TotWhInjL3"),
    **_per_phase("energy_absorbed_phase", "TotWhAbsL1", "TotWhAbsL2", "TotWhAbsL3"),
    **_per_phase("reactive_energy_injected_phase", "TotVarhInjL1", "TotVarhInjL2", "TotVarhInjL3"),
    **_per_phase("reactive_energy_absorbed_phase", "TotVarhAbsL1", "TotVarhAbsL2", "TotVarhAbsL3"),
}

_DER_CAPACITY: dict[str, str | PointName] = {
    "WMaxRtg": "rated_power",
    "WOvrExtRtg": "rated_power_over_excited",
    "WUndExtRtg": "rated_power_under_excited",
    "VAMaxRtg": "rated_apparent_power",
    "VarMaxInjRtg": "rated_reactive_power_injected",
    "VarMaxAbsRtg": "rated_reactive_power_absorbed",
    "WChaRteMaxRtg": "rated_max_charge_power",
    "WDisChaRteMaxRtg": "rated_max_discharge_power",
    "VAChaRteMaxRtg": "rated_max_charge_apparent_power",
    "VADisChaRteMaxRtg": "rated_max_discharge_apparent_power",
    "VNomRtg": "rated_nominal_voltage",
    "VMaxRtg": "rated_max_voltage",
    "VMinRtg": "rated_min_voltage",
    "AMaxRtg": "rated_current",
    "ReactSusceptRtg": "reactive_susceptance",
    "NorOpCatRtg": "normal_operating_category",
    "AbnOpCatRtg": "abnormal_operating_category",
    "CtrlModes": "supported_control_modes",
    "WMaxOvrExt": "max_power_over_excited_setting",
    "WMaxUndExt": "max_power_under_excited_setting",
    "VAMax": "max_apparent_power_setting",
    "VarMaxInj": "max_reactive_power_injected_setting",
    "VarMaxAbs": "max_reactive_power_absorbed_setting",
    "WChaRteMax": "max_charge_power_setting",
    "WDisChaRteMax": "max_discharge_power_setting",
    "VAChaRteMax": "max_charge_apparent_power_setting",
    "VADisChaRteMax": "max_discharge_apparent_power_setting",
    "VNom": "nominal_voltage_setting",
    "VMax": "max_voltage_setting",
    "VMin": "min_voltage_setting",
    "AMax": "max_current_setting",
}

# The points of 704 that are not a control of their own. A ``Rvrt``
# point holds what its setpoint returns to when the revert timer runs
# out, an ``EnaRvrt`` point whether the function stays on then, the
# reading the export limit switch already uses.
_DER_CONTROLS: dict[str, str | PointName] = {
    "PFWInjEna": "pf_injecting_enabled",
    "PFWInjEnaRvrt": "pf_injecting_revert_enabled",
    "PFWInjRvrtTms": "pf_injecting_revert_time",
    "PFWInjRvrtRem": "pf_injecting_revert_time_remaining",
    "PFWInj:Ext": "pf_injecting_excitation",
    "PFWInjRvrt:Ext": "pf_injecting_revert_excitation",
    "PFWAbsEna": "pf_absorbing_enabled",
    "PFWAbsEnaRvrt": "pf_absorbing_revert_enabled",
    "PFWAbsRvrtTms": "pf_absorbing_revert_time",
    "PFWAbsRvrtRem": "pf_absorbing_revert_time_remaining",
    "PFWAbs:Ext": "pf_absorbing_excitation",
    "PFWAbsRvrt:Ext": "pf_absorbing_revert_excitation",
    "WMaxLimPctRvrtRem": "export_limit_revert_time_remaining",
    "WSetRvrt": "active_power_setpoint_revert_value",
    "WSetPct": "active_power_setpoint_pct",
    "WSetPctRvrt": "active_power_setpoint_pct_revert_value",
    "WSetEnaRvrt": "active_power_setpoint_revert_enabled",
    "WSetRvrtTms": "active_power_setpoint_revert_time",
    "WSetRvrtRem": "active_power_setpoint_revert_time_remaining",
    "VarSetEna": "reactive_power_setpoint_enabled",
    "VarSetMod": "reactive_power_setpoint_mode",
    "VarSetPri": "reactive_power_priority",
    "VarSet": "reactive_power_setpoint",
    "VarSetRvrt": "reactive_power_setpoint_revert_value",
    "VarSetPct": "reactive_power_setpoint_pct",
    "VarSetPctRvrt": "reactive_power_setpoint_pct_revert_value",
    "VarSetEnaRvrt": "reactive_power_setpoint_revert_enabled",
    "VarSetRvrtTms": "reactive_power_setpoint_revert_time",
    "VarSetRvrtRem": "reactive_power_setpoint_revert_time_remaining",
    "WRmp": "active_power_ramp_rate",
    "WRmpRef": "active_power_ramp_rate_reference",
    "VarRmp": "reactive_power_ramp_rate",
    "AntiIslEna": "anti_islanding_enabled",
}

_DER_STORAGE: dict[str, str | PointName] = {
    "WHRtg": "rated_storage_capacity",
    "WHAvail": "energy_available",
    "SoC": "state_of_charge",
    "SoH": "state_of_health",
    "Sta": "status",
}

_DER_DC: dict[str, str | PointName] = {
    "PrtAlrms": "port_alarms",
    "DCWhInj": "dc_energy_injected",
    "DCWhAbs": "dc_energy_absorbed",
    "Prt:PrtTyp": "port_type",
    "Prt:DCA": "port_dc_current",
    "Prt:DCV": "port_dc_voltage",
    "Prt:DCW": "port_dc_power",
    "Prt:DCWhInj": "port_dc_energy_injected",
    "Prt:DCWhAbs": "port_dc_energy_absorbed",
    "Prt:Tmp": "port_temperature",
    "Prt:DCSta": "port_status",
    "Prt:DCAlrm": "port_alarm",
}

# The cell, module and event points the battery models 802 to 805 share.
_BATTERY_COMMON: dict[str, str | PointName] = {
    "SoC": "state_of_charge",
    "SoH": "state_of_health",
    "DoD": "depth_of_discharge",
    "CellVMax": "max_cell_voltage",
    "CellVMin": "min_cell_voltage",
    "CellVAvg": "average_cell_voltage",
    "ModTmpMax": "max_module_temperature",
    "ModTmpMin": "min_module_temperature",
    "ModTmpAvg": "average_module_temperature",
    "Evt1": "events_1",
    "Evt2": "events_2",
    "EvtVnd1": "vendor_events_1",
    "EvtVnd2": "vendor_events_2",
}

_BATTERY: dict[str, str | PointName] = {
    "AHRtg": "rated_charge_capacity",
    "WHRtg": "rated_storage_capacity",
    "WChaRteMax": "rated_max_charge_power",
    "WDisChaRteMax": "rated_max_discharge_power",
    "DisChaRte": "self_discharge_rate",
    "SoCMax": "rated_max_state_of_charge",
    "SoCMin": "rated_min_state_of_charge",
    "SocRsvMax": "max_reserve",
    "SoCRsvMin": "min_reserve",
    "ChaSt": "charge_status",
    "LocRemCtl": "control_mode",
    "Typ": "battery_type",
    "State": "battery_bank_state",
    "StateVnd": "vendor_battery_bank_state",
    "V": "external_battery_voltage",
    "VMax": "max_battery_voltage",
    "VMin": "min_battery_voltage",
    "A": "dc_current",
    "AChaMax": "max_charge_current",
    "ADisChaMax": "max_discharge_current",
    "W": "dc_power",
    "ReqInvState": "inverter_state_request",
    "ReqW": "battery_power_request",
    "SetOp": "operation_command",
    "SetInvState": "inverter_state_command",
}

_BATTERY_BANK: dict[str, str | PointName] = {
    "StrVMax": "max_string_voltage",
    "StrVMin": "min_string_voltage",
    "StrVAvg": "average_string_voltage",
    "StrAMax": "max_string_current",
    "StrAMin": "min_string_current",
    "StrAAvg": "average_string_current",
    "string:StrSt": "string_status",
    "string:StrConFail": "string_connection_failure_reason",
    "string:StrSoC": "string_state_of_charge",
    "string:StrSoH": "string_state_of_health",
    "string:StrA": "string_current",
    "string:StrCellVMax": "string_max_cell_voltage",
    "string:StrCellVMin": "string_min_cell_voltage",
    "string:StrCellVAvg": "string_average_cell_voltage",
    "string:StrModTmpMax": "string_max_module_temperature",
    "string:StrModTmpMin": "string_min_module_temperature",
    "string:StrModTmpAvg": "string_average_module_temperature",
    "string:StrDisRsn": "string_disabled_reason",
    "string:StrConSt": "string_contactor_status",
    "string:StrEvt1": "string_events_1",
    "string:StrEvt2": "string_events_2",
    "string:StrEvtVnd1": "string_vendor_events_1",
    "string:StrEvtVnd2": "string_vendor_events_2",
    "string:StrSetEna": "string_enable_command",
    "string:StrSetCon": "string_connect_command",
}

_BATTERY_STRING: dict[str, str | PointName] = {
    "St": "status",
    "ConFail": "connection_failure_reason",
    "A": "dc_current",
    "V": "dc_voltage",
    "ConSt": "contactor_status",
    "SetEna": "enable_command",
    "SetCon": "connect_command",
    "lithium_ion_string_module:ModSoC": "battery_module_state_of_charge",
    "lithium_ion_string_module:ModSoH": "battery_module_state_of_health",
    "lithium_ion_string_module:ModCellVMax": "battery_module_max_cell_voltage",
    "lithium_ion_string_module:ModCellVMin": "battery_module_min_cell_voltage",
    "lithium_ion_string_module:ModCellVAvg": "battery_module_average_cell_voltage",
    "lithium_ion_string_module:ModCellTmpMax": "battery_module_max_cell_temperature",
    "lithium_ion_string_module:ModCellTmpMin": "battery_module_min_cell_temperature",
    "lithium_ion_string_module:ModCellTmpAvg": "battery_module_average_cell_temperature",
}

_BATTERY_MODULE: dict[str, str | PointName] = {
    "V": "dc_voltage",
    "CellTmpMax": "max_cell_temperature",
    "CellTmpMin": "min_cell_temperature",
    "CellTmpAvg": "average_cell_temperature",
    "lithium-ion-module-cell:CellV": "cell_voltage",
    "lithium-ion-module-cell:CellTmp": "cell_temperature",
    "lithium-ion-module-cell:CellSt": "cell_status",
}


def _table(*tables: Mapping[str, str | PointName]) -> Mapping[str, PointName]:
    """Merge ``tables`` into one, a later table winning, with every entry a PointName."""
    merged: dict[str, PointName] = {}
    for table in tables:
        for point, name in table.items():
            merged[point] = name if isinstance(name, PointName) else PointName(name)
    return MappingProxyType(merged)


#: Model id to its table: point name, or ``group:point`` for a point in a
#: group, to the name. Model 220 has only the inverter table, as it had
#: before the other tables existed.
POINT_NAMES: Mapping[int, Mapping[str, PointName]] = MappingProxyType(
    {
        **dict.fromkeys((101, 102, 103, 111, 112, 113), _table(_INVERTER)),
        120: _table(_INVERTER, _NAMEPLATE),
        121: _table(_INVERTER, _SETTINGS),
        122: _table(_STATUS),
        123: _table(_IMMEDIATE_CONTROLS),
        124: _table(_STORAGE),
        160: _table(_MPPT),
        **dict.fromkeys((201, 202, 203, 204, 211, 212, 213, 214), _table(_INVERTER, _METER)),
        220: _table(_INVERTER),
        307: _table(_BASE_MET),
        308: _table(_MINI_MET),
        **dict.fromkeys((401, 402, 403, 404), _table(_INVERTER, _STRING_COMBINER)),
        **dict.fromkeys((501, 502), _table(_PANEL)),
        601: _table(_TRACKER),
        701: _table(_INVERTER, _DER_MEASUREMENT),
        702: _table(_INVERTER, _DER_CAPACITY),
        704: _table(_DER_CONTROLS),
        713: _table(_DER_STORAGE),
        714: _table(_INVERTER, _DER_DC),
        801: _table({"DEPRECATED": "deprecated_model"}),
        802: _table(_BATTERY_COMMON, _BATTERY),
        803: _table(_BATTERY_COMMON, _BATTERY_BANK),
        804: _table(_BATTERY_COMMON, _BATTERY_STRING),
        805: _table(_BATTERY_COMMON, _BATTERY_MODULE),
    }
)


def point_name(model_id: int, key: str) -> PointName | None:
    """Return the name of a point, or None for a point that keeps its SunSpec label.

    Args:
        model_id: The SunSpec model the point belongs to.
        key: The flattened key of models.py. A point in a group arrives as
            ``group:index:point``, and its name gets the index as the
            ``index`` placeholder.
    """
    table = POINT_NAMES.get(model_id)
    if table is None:
        return None
    group, separator, rest = key.partition(":")
    if not separator:
        return table.get(key)
    index, _, point = rest.partition(":")
    name = table.get(f"{group}:{point}")
    if name is None:
        return None
    return PointName(name.key, {**name.placeholders, "index": index})
