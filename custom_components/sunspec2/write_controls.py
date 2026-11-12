"""Which SunSpec points become write entities, and what kind.

The list is curated on purpose. The bundled SunSpec definitions mark
more than 1500 points in 67 models as RW, and exposing all of them
would do harm:

* Models 707..710 are the over/under voltage and frequency trip curves.
  They are type-approved equipment settings under most grid codes
  (VDE-AR-N 4105 in Germany among them). A write there disables the
  protection that disconnects the inverter from a faulted grid. The
  same holds for ``AntiIslEna`` (islanding detection, which stops an
  inverter from energising a line that workers believe is dead) and for
  the enter-service envelope of model 703.
* Model 121 reads like a settings model and is not one. ``VMax`` /
  ``VMin`` / ``ECPNomHz`` are protection limits, and ``WMax`` is the
  reference every percentage in the device is measured against, so a
  change of ``WMax`` silently redefines every export limit ever written.
* Models 126 and 129..142 are curves in repeating groups. A curve is
  not a set of independent settings: one point written without the
  others produces a shape the device never agreed to.

What is left is what people automate: cap the export, steer the
battery. Every entry in this module is a decision of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

# The model ids that carry controls.
IMMEDIATE_CONTROLS_MODEL = 123
DER_AC_CONTROLS_MODEL = 704
STORAGE_CONTROL_MODEL = 124

#: Every model this module can build controls for. The coordinator
#: polls these on every device that has them, so a user does not have to
#: find and tick them in the model list first.
WRITE_CAPABLE_MODELS: frozenset[int] = frozenset(
    {IMMEDIATE_CONTROLS_MODEL, DER_AC_CONTROLS_MODEL, STORAGE_CONTROL_MODEL}
)

# Upper bound for an export limit (WMaxLimPct), entity and action, in
# percent of WMax. The obvious reading of the SunSpec definition is
# 0..100, but real firmware uses values above 100 as a "no limit"
# sentinel: a KACO in #17 ships with 110. With a bound of 100 the user
# cannot restore that value, because HA's number component rejects an
# out-of-range service call and clamps the native value.
#
# The bound is flat and not derived from the current value. A ceiling
# that follows what the device reports falls back to 100 as soon as the
# user writes 50, and 110 is out of reach again.
EXPORT_LIMIT_HARD_MAX_PCT = 200

PLATFORM_NUMBER = "number"
PLATFORM_SWITCH = "switch"
PLATFORM_SELECT = "select"


@dataclass(frozen=True)
class WriteControlSpec:
    """One control entity: which point, which platform, how to render it.

    ``model_id`` and ``point_name`` are part of the entity's unique id
    and must not change for a shipped control.
    """

    model_id: int
    point_name: str
    platform: str
    translation_key: str
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
    #: Keep a sensor that shipped before this point became writable.
    retain_sensor: bool = False

    @property
    def unique_key(self) -> str:
        return f"{self.model_id}:{self.point_name}"


# --------------------------------------------------------------------------
# Model 123, Immediate Controls
#
# All six points stay, also the ones outside daily use: installations
# have automations on these entities.
# --------------------------------------------------------------------------
_MODEL_123: tuple[WriteControlSpec, ...] = (
    WriteControlSpec(
        model_id=123,
        point_name="WMaxLimPct",
        platform=PLATFORM_NUMBER,
        translation_key="export_limit_pct",
        native_min=0,
        native_max=EXPORT_LIMIT_HARD_MAX_PCT,
    ),
    WriteControlSpec(
        model_id=123,
        point_name="WMaxLim_Ena",
        platform=PLATFORM_SWITCH,
        translation_key="export_limit_enabled",
    ),
    WriteControlSpec(
        model_id=123,
        point_name="WMaxLimPct_RvrtTms",
        platform=PLATFORM_NUMBER,
        translation_key="export_limit_revert_time",
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
        native_min=-1.0,
        native_max=1.0,
        native_step=0.01,
    ),
    WriteControlSpec(
        model_id=123,
        point_name="OutPFSet_Ena",
        platform=PLATFORM_SWITCH,
        translation_key="power_factor_enabled",
    ),
    WriteControlSpec(
        model_id=123,
        point_name="Conn",
        platform=PLATFORM_SWITCH,
        translation_key="inverter_grid_connection",
        # Off disconnects the inverter from the grid, so the switch is
        # there for whoever turns it on in the entity list.
        enabled_by_default=False,
    ),
)

# --------------------------------------------------------------------------
# Model 704, DER AC Controls
#
# The successor of 123, and better in two ways. On the KACO of #17
# model 704 reports a lapsed limit as lapsed (RvrtTmsRem counts down
# and the enable flag clears), while model 123 on the same device
# still shows the limit as active. And 704 takes an absolute setpoint
# in watts, which is what a zero-export control loop wants: a percent
# of the nameplate needs the automation to know the nameplate.
# --------------------------------------------------------------------------
_MODEL_704: tuple[WriteControlSpec, ...] = (
    WriteControlSpec(
        model_id=704,
        point_name="WMaxLimPct",
        platform=PLATFORM_NUMBER,
        translation_key="export_limit_pct",
        native_min=0,
        native_max=EXPORT_LIMIT_HARD_MAX_PCT,
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WMaxLimPctEna",
        platform=PLATFORM_SWITCH,
        translation_key="export_limit_enabled",
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WMaxLimPctRvrtTms",
        platform=PLATFORM_NUMBER,
        translation_key="export_limit_revert_time",
        native_min=0,
        # The point is a uint32 here, a uint16 in model 123.
        native_max=4294967295,
        native_step=1,
        unit="s",
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WMaxLimPctRvrt",
        platform=PLATFORM_NUMBER,
        translation_key="export_limit_revert_value",
        native_min=0,
        native_max=EXPORT_LIMIT_HARD_MAX_PCT,
        # The percentage the inverter falls back to when the revert
        # timer expires. Set to the limit just written, it makes the
        # expiry change nothing, which is the alternative to disabling
        # the timeout: the dead-man switch still fires, but lands
        # somewhere harmless. Model 123 has no such point, one more
        # reason 704 is preferred where both exist.
        enabled_by_default=False,
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WMaxLimPctEnaRvrt",
        platform=PLATFORM_SWITCH,
        translation_key="export_limit_revert_enabled",
        # Whether the limit stays enabled after the timer expires. With
        # this switch on and the revert value set, a lapse changes
        # nothing.
        enabled_by_default=False,
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WSet",
        platform=PLATFORM_NUMBER,
        translation_key="active_power_setpoint",
        # The point is an int32 in watts. Negative means import on a
        # device that supports it, so the range does not stop at zero.
        # The inverter rejects or clamps what it will not do.
        native_min=-1000000,
        native_max=1000000,
        native_step=1,
        unit="W",
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WSetPct",
        platform=PLATFORM_NUMBER,
        translation_key="active_power_setpoint_pct",
        native_min=-100,
        native_max=100,
        unit="%",
        retain_sensor=True,
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WSetEna",
        platform=PLATFORM_SWITCH,
        translation_key="active_power_setpoint_enabled",
    ),
    WriteControlSpec(
        model_id=704,
        point_name="WSetMod",
        platform=PLATFORM_SELECT,
        translation_key="active_power_setpoint_mode",
        # WSetMod decides whether WSet (watts) or WSetPct (percent) is
        # the setpoint in force. An absolute setpoint that appears to do
        # nothing usually means WSetMod is on the other one.
        # The option names are the SunSpec symbols W_MAX_PCT / WATTS in
        # lowercase, because HA translation keys must match
        # [a-z0-9-_]+. The select maps the raw value to the option name
        # and never sees the symbols.
        options={"w_max_pct": 0, "watts": 1},
        enabled_by_default=False,
    ),
)

# --------------------------------------------------------------------------
# Model 124, Basic Storage Control
#
# These controls charge and discharge the battery at something other
# than full power (#32). InWRte / OutWRte are the dynamic pair (a rate
# as a percentage, meant to be moved from an automation), and WChaMax
# is the ceiling both are a percentage of.
# --------------------------------------------------------------------------
_MODEL_124: tuple[WriteControlSpec, ...] = (
    WriteControlSpec(
        model_id=124,
        point_name="InWRte",
        platform=PLATFORM_NUMBER,
        translation_key="battery_charge_rate",
        native_min=0,
        native_max=100,
    ),
    WriteControlSpec(
        model_id=124,
        point_name="OutWRte",
        platform=PLATFORM_NUMBER,
        translation_key="battery_discharge_rate",
        native_min=0,
        native_max=100,
    ),
    WriteControlSpec(
        model_id=124,
        point_name="WChaMax",
        platform=PLATFORM_NUMBER,
        translation_key="battery_max_charge_power",
        native_min=0,
        native_max=1000000,
        unit="W",
    ),
    WriteControlSpec(
        model_id=124,
        point_name="StorCtl_Mod",
        platform=PLATFORM_SELECT,
        translation_key="battery_control_mode",
        # StorCtl_Mod is a bitfield16, not an enum: bit 0 gates InWRte,
        # bit 1 gates OutWRte. It is one select with the four
        # combinations and not two switches on purpose. Two switches
        # would each take the base of their read-modify-write from
        # coordinator.data, which can be one scan interval old, so an
        # automation flipping both at once would silently overwrite the
        # first write with the second.
        options={"off": 0, "charge": 1, "discharge": 2, "both": 3},
    ),
    WriteControlSpec(
        model_id=124,
        point_name="InOutWRte_RvrtTms",
        platform=PLATFORM_NUMBER,
        translation_key="battery_rate_revert_time",
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
        native_min=0,
        native_max=100,
        # A backup reserve is set once and not automated, so the
        # entity starts disabled.
        enabled_by_default=False,
    ),
    WriteControlSpec(
        model_id=124,
        point_name="ChaGriSet",
        platform=PLATFORM_SWITCH,
        translation_key="battery_grid_charging",
        # ChaGriSet is an enum16, PV = 0 and GRID = 1: whether the
        # battery may take energy from the grid at all. On Fronius the
        # point is AND-linked with the same setting in the inverter's
        # web interface, so the switch alone does nothing there
        # (docs/fronius.md).
        on_value=1,
        off_value=0,
    ),
)


_SPECS_BY_MODEL: dict[int, tuple[WriteControlSpec, ...]] = {
    123: _MODEL_123,
    704: _MODEL_704,
    124: _MODEL_124,
}


def active_specs(
    detected_models: set[int] | frozenset[int],
    unsupported: frozenset[str] = frozenset(),
) -> list[WriteControlSpec]:
    """Return the controls to build for a device exposing these models.

    Model 704 wins over model 123 where a device has both, and 123 then
    contributes nothing. The storage controls of model 124 are added
    whenever the model is present.

    ``unsupported`` holds the ``unique_key`` of the controls the vendor
    profile says the device does not implement. They are left out.
    """
    specs: list[WriteControlSpec] = []
    # Only one of 704 and 123: two "export limit" entities for the same
    # physical setting confuse, and on the KACO of #17, which serves
    # both, they do not agree. Only 704 reports a lapsed limit as lapsed.
    if DER_AC_CONTROLS_MODEL in detected_models:
        specs.extend(_MODEL_704)
    elif IMMEDIATE_CONTROLS_MODEL in detected_models:
        specs.extend(_MODEL_123)
    if STORAGE_CONTROL_MODEL in detected_models:
        specs.extend(_MODEL_124)
    return [spec for spec in specs if spec.unique_key not in unsupported]


def active_specs_for_platform(
    detected_models: set[int] | frozenset[int],
    platform: str,
    unsupported: frozenset[str] = frozenset(),
) -> list[WriteControlSpec]:
    return [
        spec for spec in active_specs(detected_models, unsupported) if spec.platform == platform
    ]


def specs_for_model(model_id: int) -> tuple[WriteControlSpec, ...]:
    """Return all specs defined for a model, whatever a device exposes."""
    return _SPECS_BY_MODEL.get(model_id, ())


def export_limit_points(
    detected_models: set[int] | frozenset[int],
) -> tuple[int, str, str] | None:
    """Return ``(model_id, percent_point, enable_point)`` for the export limit.

    The ``set_export_limit`` action uses this function to make the same
    704-over-123 choice as the entities, so the action and the Number
    write the same register on a device that has both models.

    Returns ``None`` if the device exposes neither model.
    """
    if DER_AC_CONTROLS_MODEL in detected_models:
        return (DER_AC_CONTROLS_MODEL, "WMaxLimPct", "WMaxLimPctEna")
    if IMMEDIATE_CONTROLS_MODEL in detected_models:
        return (IMMEDIATE_CONTROLS_MODEL, "WMaxLimPct", "WMaxLim_Ena")
    return None
