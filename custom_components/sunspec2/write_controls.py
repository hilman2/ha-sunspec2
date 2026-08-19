"""Which SunSpec points become write entities, and what kind.

v0.19.0. Before this the write platforms carried one hand-written class
per point and covered six points of model 123.

The tempting generalisation is "expose every RW point". The bundled
SunSpec definitions contain 1586 of them across 67 models, and that
number is the argument against it, not for it:

* Models 707..710 are the over/under voltage and frequency trip curves,
  and SunSpec marks them RW. They are type-approved equipment settings
  under most grid codes (VDE-AR-N 4105 in Germany among them). Writing
  there does not misconfigure an inverter, it disables the protection
  that disconnects it from a faulted grid. Same class: ``AntiIslEna``
  (islanding detection, which is what stops an inverter energising a
  segment line workers believe is dead) and model 703's enter-service
  envelope.
* Model 121 reads like a settings model and is not one. ``VMax`` /
  ``VMin`` / ``ECPNomHz`` are protection limits, and ``WMax`` is the
  reference every percentage in the device is measured against, so
  changing it silently redefines every export limit ever written.
* Models 126 and 129..142 are curves in repeating groups. A curve is
  not a set of independent settings: writing one point without the
  others produces a shape the device never agreed to.

What is left is what people actually automate: cap the export, steer
the battery. This module is the curated list for that, and it is a list
rather than a rule because every entry deserved a decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

# Model ids that carry controls, in the order the selection logic
# prefers them.
IMMEDIATE_CONTROLS_MODEL = 123
DER_AC_CONTROLS_MODEL = 704
STORAGE_CONTROL_MODEL = 124

#: Every model this module can build controls for. The coordinator
#: polls these while the write beta is on, so a user does not have to
#: find and tick them in the model list first.
WRITE_CAPABLE_MODELS: frozenset[int] = frozenset(
    {IMMEDIATE_CONTROLS_MODEL, DER_AC_CONTROLS_MODEL, STORAGE_CONTROL_MODEL}
)

PLATFORM_NUMBER = "number"
PLATFORM_SWITCH = "switch"
PLATFORM_SELECT = "select"


@dataclass(frozen=True)
class WriteControlSpec:
    """One control entity: which point, which platform, how to render it."""

    model_id: int
    point_name: str
    platform: str
    translation_key: str
    icon: str
    #: Number only. ``None`` means "derive from the point's scale factor".
    native_min: float | None = None
    native_max: float | None = None
    native_step: float | None = None
    #: Number only. ``None`` means "take the unit from the model definition".
    unit: str | None = None
    #: Select only: option name -> raw value written to the point.
    options: dict[str, int] = field(default_factory=dict)
    #: Switch only: SunSpec spells booleans as enums, and not always
    #: with 1 meaning on.
    on_value: int = 1
    off_value: int = 0
    enabled_by_default: bool = True

    @property
    def unique_key(self) -> str:
        return f"{self.model_id}:{self.point_name}"


# --------------------------------------------------------------------------
# Model 123, Immediate Controls
#
# The original beta. Kept whole rather than trimmed to the daily-use
# points, because these entities have been shipping since v0.12.0 and
# people have automations pointing at them.
# --------------------------------------------------------------------------
_MODEL_123: tuple[WriteControlSpec, ...] = (
    WriteControlSpec(
        model_id=123,
        point_name="WMaxLimPct",
        platform=PLATFORM_NUMBER,
        translation_key="export_limit_pct",
        icon="mdi:transmission-tower-export",
        native_min=0,
        # Above 100 is allowed: some firmware uses e.g. 110 to mean "no
        # limit", and the KACO in #17 shipped that way with an owner who
        # could not put it back.
        native_max=200,
    ),
    WriteControlSpec(
        model_id=123,
        point_name="WMaxLim_Ena",
        platform=PLATFORM_SWITCH,
        translation_key="export_limit_enabled",
        icon="mdi:transmission-tower-export",
    ),
    WriteControlSpec(
        model_id=123,
        point_name="WMaxLimPct_RvrtTms",
        platform=PLATFORM_NUMBER,
        translation_key="export_limit_revert_time",
        icon="mdi:timer-sand",
        native_min=0,
        native_max=65535,
        native_step=1,
        unit="s",
    ),
    WriteControlSpec(
        model_id=123,
        point_name="OutPFSet",
        platform=PLATFORM_NUMBER,
        translation_key="power_factor_set",
        icon="mdi:angle-acute",
        native_min=-1.0,
        native_max=1.0,
        native_step=0.01,
    ),
    WriteControlSpec(
        model_id=123,
        point_name="OutPFSet_Ena",
        platform=PLATFORM_SWITCH,
        translation_key="power_factor_enabled",
        icon="mdi:angle-acute",
    ),
    WriteControlSpec(
        model_id=123,
        point_name="Conn",
        platform=PLATFORM_SWITCH,
        translation_key="inverter_grid_connection",
        icon="mdi:transmission-tower",
    ),
)

# --------------------------------------------------------------------------
# Model 704, DER AC Controls
#
# The modern equivalent of 123, and better in two ways that matter.
# On @tisoft's KACO in #17 it reports the truth when a limit has
# lapsed (RvrtTmsRem counts down and the enable flag clears) while 123
# on the same device still shows the limit as active. And it can take
# an absolute setpoint in watts, which is what a zero-export control
# loop actually wants: percent-of-nameplate needs the automation to
# know the nameplate.
# --------------------------------------------------------------------------
_MODEL_704: tuple[WriteControlSpec, ...] = (
    WriteControlSpec(
        model_id=704,
        point_name="WMaxLimPct",
        platform=PLATFORM_NUMBER,
        translation_key="export_limit_pct",
        icon="mdi:transmission-tower-export",
        native_min=0,
        native_max=200,
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WMaxLimPctEna",
        platform=PLATFORM_SWITCH,
        translation_key="export_limit_enabled",
        icon="mdi:transmission-tower-export",
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WMaxLimPctRvrtTms",
        platform=PLATFORM_NUMBER,
        translation_key="export_limit_revert_time",
        icon="mdi:timer-sand",
        native_min=0,
        # uint32 here, unlike 123's uint16.
        native_max=4294967295,
        native_step=1,
        unit="s",
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WSet",
        platform=PLATFORM_NUMBER,
        translation_key="active_power_setpoint",
        icon="mdi:flash",
        # int32 watts. Negative means import on a device that supports
        # it, so the range is not clamped at zero here; the inverter
        # rejects or clamps what it will not do.
        native_min=-1000000,
        native_max=1000000,
        native_step=1,
        unit="W",
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WSetEna",
        platform=PLATFORM_SWITCH,
        translation_key="active_power_setpoint_enabled",
        icon="mdi:flash",
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WSetMod",
        platform=PLATFORM_SELECT,
        translation_key="active_power_setpoint_mode",
        icon="mdi:flash-outline",
        # Decides whether WSet (watts) or WSetPct (percent) is the
        # setpoint in force, so an absolute setpoint that appears to do
        # nothing is usually this being on the wrong one.
        options={"W_MAX_PCT": 0, "WATTS": 1},
        enabled_by_default=False,
    ),
)

# --------------------------------------------------------------------------
# Model 124, Basic Storage Control
#
# Requested by @Newsbeyss2 in #32: charge the battery at something
# other than full power. InWRte / OutWRte are the dynamic pair (rate as
# a percentage, meant to be moved from an automation), WChaMax is the
# ceiling they are a percentage of.
# --------------------------------------------------------------------------
_MODEL_124: tuple[WriteControlSpec, ...] = (
    WriteControlSpec(
        model_id=124,
        point_name="InWRte",
        platform=PLATFORM_NUMBER,
        translation_key="battery_charge_rate",
        icon="mdi:battery-charging-medium",
        native_min=0,
        native_max=100,
    ),
    WriteControlSpec(
        model_id=124,
        point_name="OutWRte",
        platform=PLATFORM_NUMBER,
        translation_key="battery_discharge_rate",
        icon="mdi:battery-arrow-down",
        native_min=0,
        native_max=100,
    ),
    WriteControlSpec(
        model_id=124,
        point_name="WChaMax",
        platform=PLATFORM_NUMBER,
        translation_key="battery_max_charge_power",
        icon="mdi:battery-charging",
        native_min=0,
        native_max=1000000,
        unit="W",
    ),
    WriteControlSpec(
        model_id=124,
        point_name="StorCtl_Mod",
        platform=PLATFORM_SELECT,
        translation_key="battery_control_mode",
        icon="mdi:battery-sync",
        # A bitfield16, not an enum: bit 0 gates InWRte, bit 1 gates
        # OutWRte. Exposed as one entity with the four combinations
        # rather than as two switches on purpose. Two switches would
        # each read their read-modify-write base from coordinator.data,
        # which is at most one scan interval fresh, so flipping both in
        # the same automation would silently clobber the first write.
        options={"NONE": 0, "CHARGE": 1, "DISCHARGE": 2, "BOTH": 3},
    ),
    WriteControlSpec(
        model_id=124,
        point_name="InOutWRte_RvrtTms",
        platform=PLATFORM_NUMBER,
        translation_key="battery_rate_revert_time",
        icon="mdi:timer-sand",
        native_min=0,
        native_max=65535,
        native_step=1,
        unit="s",
    ),
    WriteControlSpec(
        model_id=124,
        point_name="MinRsvPct",
        platform=PLATFORM_NUMBER,
        translation_key="battery_min_reserve",
        icon="mdi:battery-lock",
        native_min=0,
        native_max=100,
        # Set once for a backup reserve rather than automated, so it
        # ships disabled and stays out of the way of the daily controls.
        enabled_by_default=False,
    ),
)

_SPECS_BY_MODEL: dict[int, tuple[WriteControlSpec, ...]] = {
    123: _MODEL_123,
    704: _MODEL_704,
    124: _MODEL_124,
}


def active_specs(detected_models: set[int] | frozenset[int]) -> list[WriteControlSpec]:
    """Return the controls to build for a device exposing these models.

    Model 704 wins over model 123 where a device has both, and 123 then
    contributes nothing. Two "export limit" entities pointing at the
    same physical setting is a support question with advance notice,
    and on the one device we have evidence from they do not even agree:
    @tisoft's KACO exposes both, and only 704 reports a lapsed limit
    honestly.

    Storage control is orthogonal and is added whenever it is present.
    """
    specs: list[WriteControlSpec] = []
    if DER_AC_CONTROLS_MODEL in detected_models:
        specs.extend(_MODEL_704)
    elif IMMEDIATE_CONTROLS_MODEL in detected_models:
        specs.extend(_MODEL_123)
    if STORAGE_CONTROL_MODEL in detected_models:
        specs.extend(_MODEL_124)
    return specs


def active_specs_for_platform(
    detected_models: set[int] | frozenset[int], platform: str
) -> list[WriteControlSpec]:
    return [spec for spec in active_specs(detected_models) if spec.platform == platform]


def models_in_use(detected_models: set[int] | frozenset[int]) -> set[int]:
    """Model ids the active specs need polled."""
    return {spec.model_id for spec in active_specs(detected_models)}


def specs_for_model(model_id: int) -> tuple[WriteControlSpec, ...]:
    """All specs defined for a model, regardless of what a device exposes.

    Used by the sensor platform to keep a control point from also
    becoming a read-only sensor that duplicates it.
    """
    return _SPECS_BY_MODEL.get(model_id, ())


def export_limit_points(
    detected_models: set[int] | frozenset[int],
) -> tuple[int, str, str] | None:
    """Return ``(model_id, percent_point, enable_point)`` for the export limit.

    The ``set_export_limit`` service action needs the same 704-over-123
    choice the entities make. Without this it would keep writing model
    123 while the Number entity showed model 704, so the service and the
    UI would drive two different registers on a device that has both and
    only agree by luck.

    Returns ``None`` if the device exposes neither model.
    """
    if DER_AC_CONTROLS_MODEL in detected_models:
        return (DER_AC_CONTROLS_MODEL, "WMaxLimPct", "WMaxLimPctEna")
    if IMMEDIATE_CONTROLS_MODEL in detected_models:
        return (IMMEDIATE_CONTROLS_MODEL, "WMaxLimPct", "WMaxLim_Ena")
    return None
