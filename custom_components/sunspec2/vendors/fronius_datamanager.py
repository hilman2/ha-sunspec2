"""Fronius Symo, Primo, Eco, Galvo and Symo Hybrid: the Datamanager 2.0 generation.

Sources: the Fronius Datamanager Modbus TCP & RTU operating
instructions, document 42,0410,2049 (revision 033, February 2026), its
register map "Modbus Register - SunSpec Maps, State Codes and Events",
the Datamanager and Hybridmanager firmware changelogs, and a register
dump of a Symo Hybrid 5.0-3-S on Hybridmanager 1.31.1-5
(evcc-io/evcc#19078).

What differs from a GEN24, in short. The common model tells the
generations apart: ``Opt`` is the Datamanager's (3.x) or the
Hybridmanager's (1.x) firmware and ``Vr`` the power stage's (0.3.x),
where a GEN24 leaves ``Opt`` empty and carries its own 1.x in ``Vr``;
``Md`` names the device without GEN24, Tauro or Verto. Model 160 labels
its inputs "String 1" and "String 2"; on a Symo Hybrid, String 2 is the
battery, one module for both directions with ``DCW`` unsigned, and
``ChaSt`` of the storage model says which way the energy goes. Model
124 is the same block as on a GEN24, driven with the same recipes;
only the Symo Hybrid has it. The enable edge of model 123 is the
manual's own procedure here ("restart the operating mode using
register WMaxLim_Ena"), not a firmware quirk, so the cycle is on by
default. Meters answer under unit id 240, and there is no web API the
integration could speak.

The one dump shows ``WChaMax`` at 11520 on a 5 kW inverter with an
11.5 kWh battery: the capacity in Wh, reported as a rate. The percent
recipes still work, the inverter computes its percentages against the
same number; what is off is the upper bound of the watt fields.
"""

from __future__ import annotations

from .fronius import RECIPES
from .fronius import infer_mode
from .profile import ModuleRole
from .profile import StorageMode
from .profile import StorageModeProfile
from .profile import VendorProfile

#: Words in ``Md`` that name the newer generation.
NEW_GENERATION: tuple[str, ...] = ("GEN24", "TAURO", "VERTO")

#: ``Md`` prefix of the one device of this generation with a battery.
HYBRID_MODEL_PREFIX = "SYMO HYBRID"


def is_datamanager(model: str, option: str, version: str) -> bool:
    """Whether common model 1 describes a Datamanager 2.0 or Hybridmanager device.

    A GEN24 is ruled out by name. What is left is a Datamanager device
    when ``Opt`` carries a firmware version or ``Vr`` starts with the
    power stage's 0.3; a device that reports neither stays with the
    GEN24 profile, which is what applied to every Fronius before this
    one existed.
    """
    name = model.strip().upper()
    if any(word in name for word in NEW_GENERATION):
        return False
    return version.strip().startswith("0.") or bool(option.strip())


def module_role(id_str: str, model: str) -> ModuleRole | None:
    """What a Datamanager device reports under a model 160 module label.

    The inputs are "String 1" and "String 2". On a Symo Hybrid, String
    2 is the battery ("String 1 = PV input, String 2 = Storage" in the
    manual); on every other model both are PV strings. A single-input
    inverter reports "not supported" for the second, which is no
    module at all.
    """
    label = id_str.strip().upper()
    if not label.startswith("STRING"):
        return None
    if label == "STRING 2" and model.strip().upper().startswith(HYBRID_MODEL_PREFIX):
        return ModuleRole.BATTERY
    return ModuleRole.PV


FRONIUS_DATAMANAGER = VendorProfile(
    slug="fronius_datamanager",
    manufacturer_prefixes=("Fronius",),
    identifies=is_datamanager,
    module_role=module_role,
    # The manual: "to change values when an operating mode is active:
    # enter the new value into the relevant register; restart the
    # operating mode using register WMaxLim_Ena".
    enable_edge={(123, "WMaxLimPct"): "WMaxLim_Ena", (123, "OutPFSet"): "OutPFSet_Ena"},
    rearm_by_default=True,
    storage=StorageModeProfile(
        modes=tuple(StorageMode),
        recipes=RECIPES,
        infer_mode=infer_mode,
        # The 10 W step is GEN24 lore. battsett and evcc drive this
        # generation in whole percent, and nothing suggests a step.
        grid_power_step_w=1.0,
        hidden_points=frozenset({"124:InWRte", "124:OutWRte", "124:StorCtl_Mod"}),
    ),
)
