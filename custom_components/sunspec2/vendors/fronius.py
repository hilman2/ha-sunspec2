"""Fronius: GEN24, Verto and Tauro, and the Symo Advanced with a battery.

Sources: the Fronius GEN24 Modbus TCP and RTU operating instructions,
section "Basic Storage Control Model (124)"
(https://manuals.fronius.com/html/4204102649/en-US.html), and the
callifo/fronius_modbus integration, whose users have driven these
register sequences for a year.

What the manual says, in short. Every rate is a percentage of
``WChaMax``, which the inverter reports as max(MaxChaRte, MaxDisChaRte)
and as 0 when there is no battery. ``InWRte`` caps charging, ``OutWRte``
caps discharging, ``StorCtl_Mod`` bit 0 activates the charge cap and
bit 1 the discharge cap. A negative cap forces the opposite direction:
``OutWRte`` at -40 % with the discharge cap active forces charging with
at least 40 % of WChaMax, from the grid if the roof does not deliver it,
and Solar.web then shows "Forced Recharge". ``InWRte`` at -40 % forces
discharging, into the grid if the house does not take it. ``ChaGriSet``
permits charging from the grid and is AND-linked with "battery charging
from DNO grid" in the inverter's web interface, so it cannot switch grid
charging on by itself. ``MinRsvPct`` is the state of charge the inverter
keeps back, and setting it switches Solar.web to "Energy-saving mode".
Modbus commands lose against IO control and dynamic power reduction
when those have priority in the inverter's settings.

What the manual does not say and the fork learned: the grid charge
power should be a multiple of 10 W (any other value "does odd things
like charging at 500 W" per its README), firmware 1.39.5 capped grid
charging around 500 W for some (callifo/fronius_modbus#87), and writing
the mode register before the rates can hand the inverter a window it
refuses with Modbus exception 3 (callifo/fronius_modbus#126). The rates
therefore go out first, in one frame, and the mode last.
"""

from __future__ import annotations

from .profile import ModuleRole
from .profile import Rate
from .profile import Recipe
from .profile import StorageMode
from .profile import StorageModeProfile
from .profile import VendorProfile

# One row per mode, straight from the manual's examples: example 1 is
# BLOCK_CHARGING's mirror image (discharge only), example 7 is
# CHARGE_FROM_GRID. The fork offers the same eight, which is what its
# users' automations are written against.
RECIPES: dict[StorageMode, Recipe] = {
    StorageMode.AUTO: Recipe(0, Rate.FULL, Rate.FULL),
    StorageMode.PV_CHARGE_LIMIT: Recipe(1, Rate.CHARGE_LIMIT, Rate.FULL),
    StorageMode.DISCHARGE_LIMIT: Recipe(2, Rate.FULL, Rate.DISCHARGE_LIMIT),
    StorageMode.CHARGE_AND_DISCHARGE_LIMIT: Recipe(3, Rate.CHARGE_LIMIT, Rate.DISCHARGE_LIMIT),
    StorageMode.CHARGE_FROM_GRID: Recipe(2, Rate.FULL, Rate.GRID_CHARGE),
    StorageMode.DISCHARGE_TO_GRID: Recipe(1, Rate.GRID_DISCHARGE, Rate.FULL),
    StorageMode.BLOCK_DISCHARGING: Recipe(3, Rate.CHARGE_LIMIT, Rate.ZERO),
    StorageMode.BLOCK_CHARGING: Recipe(3, Rate.ZERO, Rate.DISCHARGE_LIMIT),
}


def infer_mode(ctl_mod: int, in_pct: float, out_pct: float) -> StorageMode | None:
    """The mode the inverter is in, read back from the registers.

    Read back rather than remembered: callifo/fronius_modbus#127 is an
    inverter that stayed in a forced charge while the entity reported
    "Auto", because the entity had cached its own request.
    """
    charge_capped = bool(ctl_mod & 1)
    discharge_capped = bool(ctl_mod & 2)
    if not charge_capped and not discharge_capped:
        return StorageMode.AUTO
    if discharge_capped and out_pct < 0:
        return StorageMode.CHARGE_FROM_GRID
    if charge_capped and in_pct < 0:
        return StorageMode.DISCHARGE_TO_GRID
    if charge_capped and discharge_capped:
        if in_pct == 0:
            return StorageMode.BLOCK_CHARGING
        if out_pct == 0:
            return StorageMode.BLOCK_DISCHARGING
        return StorageMode.CHARGE_AND_DISCHARGE_LIMIT
    if charge_capped:
        return StorageMode.BLOCK_CHARGING if in_pct == 0 else StorageMode.PV_CHARGE_LIMIT
    return StorageMode.BLOCK_DISCHARGING if out_pct == 0 else StorageMode.DISCHARGE_LIMIT


def module_role(id_str: str) -> ModuleRole | None:
    """What a GEN24 reports under a model 160 module label.

    "MPPT 1" and "MPPT 2" are the PV strings. "ST CHA" and "ST DISCHA"
    are the battery, as two channels: the charge channel carries power
    while the battery takes energy, the discharge channel while it gives
    energy, and each keeps its own lifetime counter in ``DCWH``. The
    labels are what the community integration keys on; the manual lists
    the modules without naming them.
    """
    label = id_str.strip().upper()
    if label.startswith("MPPT"):
        return ModuleRole.PV
    if label == "ST CHA":
        return ModuleRole.BATTERY_CHARGE
    if label == "ST DISCHA":
        return ModuleRole.BATTERY_DISCHARGE
    return None


FRONIUS = VendorProfile(
    slug="fronius",
    manufacturer_prefixes=("Fronius",),
    module_role=module_role,
    # A GEN24 on firmware 1.41 takes a new export limit only when its
    # enable register goes from 0 to 1; with the enable on, the new
    # value sits in the register and the inverter runs on the old one
    # (#17). The community integration cycles the enable around every
    # write; here the cycle is behind CONF_REARM_ON_CHANGE.
    enable_edge={(123, "WMaxLimPct"): "WMaxLim_Ena", (123, "OutPFSet"): "OutPFSet_Ena"},
    # The web interface's local login, for what Modbus does not carry.
    web_user="customer",
    storage=StorageModeProfile(
        modes=tuple(StorageMode),
        recipes=RECIPES,
        infer_mode=infer_mode,
        grid_power_step_w=10.0,
        # The generic percent entities for the same three registers.
        # Two entities writing one register in different units would
        # disagree the moment either is used.
        hidden_points=frozenset({"124:InWRte", "124:OutWRte", "124:StorCtl_Mod"}),
    ),
)
