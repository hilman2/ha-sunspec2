"""The shape of a vendor profile, and the storage mode vocabulary it fills in.

A profile is device knowledge the SunSpec text does not carry: how a
manufacturer reads the signs of the storage model, which register
sequence its firmware accepts, which generic control would write the
same register in another unit. The classes here only give that
knowledge a shape. The knowledge itself lives in one module per vendor
next to this one, and ``vendors/__init__.py`` picks the profile from
the manufacturer string in common model 1.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class StorageMode(StrEnum):
    """What the battery is told to do. The values double as HA option keys."""

    AUTO = "auto"
    PV_CHARGE_LIMIT = "pv_charge_limit"
    DISCHARGE_LIMIT = "discharge_limit"
    CHARGE_AND_DISCHARGE_LIMIT = "charge_and_discharge_limit"
    CHARGE_FROM_GRID = "charge_from_grid"
    DISCHARGE_TO_GRID = "discharge_to_grid"
    BLOCK_DISCHARGING = "block_discharging"
    BLOCK_CHARGING = "block_charging"


class Rate(StrEnum):
    """Where a recipe takes the value for ``InWRte`` or ``OutWRte`` from.

    The four setpoint members double as the keys of the watt setpoints
    the storage Number entities keep on the coordinator.
    """

    #: 100 %: the cap is out of the way.
    FULL = "full"
    #: 0 %: that direction is blocked.
    ZERO = "zero"
    #: The PV charge limit setpoint, written positive.
    CHARGE_LIMIT = "charge_limit"
    #: The discharge limit setpoint, written positive.
    DISCHARGE_LIMIT = "discharge_limit"
    #: The grid charge power setpoint, written negative: a forced charge.
    GRID_CHARGE = "grid_charge"
    #: The grid discharge power setpoint, written negative: a forced discharge.
    GRID_DISCHARGE = "grid_discharge"


#: The rates an entity can set a watt value for, in the order the
#: entities are created.
SETPOINT_RATES: tuple[Rate, ...] = (
    Rate.CHARGE_LIMIT,
    Rate.DISCHARGE_LIMIT,
    Rate.GRID_CHARGE,
    Rate.GRID_DISCHARGE,
)

#: Which model 124 point each setpoint rate lands in.
POINT_OF_RATE: dict[Rate, str] = {
    Rate.CHARGE_LIMIT: "InWRte",
    Rate.GRID_DISCHARGE: "InWRte",
    Rate.DISCHARGE_LIMIT: "OutWRte",
    Rate.GRID_CHARGE: "OutWRte",
}


@dataclass(frozen=True)
class Recipe:
    """The writes one storage mode consists of.

    Args:
        ctl_mod (int): The ``StorCtl_Mod`` bits, 1 for the charge cap,
            2 for the discharge cap.
        in_rate (Rate): What ``InWRte`` is set to.
        out_rate (Rate): What ``OutWRte`` is set to.
    """

    ctl_mod: int
    in_rate: Rate
    out_rate: Rate


@dataclass(frozen=True)
class StorageModeProfile:
    """How one vendor reads model 124, for the storage mode entities.

    Args:
        modes (tuple[StorageMode, ...]): The modes offered, in menu order.
        recipes (Mapping[StorageMode, Recipe]): The writes per mode.
        infer_mode (Callable[[int, float, float], StorageMode|None]):
            Called as ``infer_mode(ctl_mod, in_pct, out_pct)`` with the
            raw ``StorCtl_Mod`` bits and both rates in percent, as the
            device reports them. Returns the mode the device is in, or
            None for a combination no recipe produces.
        grid_power_step_w (float): Step of the grid charge and discharge
            power entities, in watts.
        hidden_points (frozenset[str]): ``"model:point"`` keys of the
            generic controls that write the same registers in another
            unit. They ship disabled while this profile is active.
    """

    modes: tuple[StorageMode, ...]
    recipes: Mapping[StorageMode, Recipe]
    infer_mode: Callable[[int, float, float], StorageMode | None]
    grid_power_step_w: float
    hidden_points: frozenset[str]

    def rates_of(self, mode: StorageMode) -> frozenset[Rate]:
        """The setpoint rates ``mode`` writes; changing one of them re-writes its point."""
        recipe = self.recipes[mode]
        return frozenset({recipe.in_rate, recipe.out_rate}) & frozenset(SETPOINT_RATES)


@dataclass(frozen=True)
class VendorProfile:
    """Everything the integration does differently for one manufacturer.

    Args:
        slug (str): Short lowercase name, for the diagnostics dump.
        manufacturer_prefixes (tuple[str, ...]): Compared case-insensitively
            against the start of ``Mn`` in common model 1. Prefixes, not
            names, because vendors are not consistent about their own
            name ("Fronius", "Fronius International GmbH").
        storage (StorageModeProfile|None): The battery mode vocabulary,
            when the vendor has one.
    """

    slug: str
    manufacturer_prefixes: tuple[str, ...]
    storage: StorageModeProfile | None = None

    def matches(self, manufacturer: str | None) -> bool:
        if not manufacturer:
            return False
        name = manufacturer.strip().lower()
        return any(name.startswith(prefix.lower()) for prefix in self.manufacturer_prefixes)


def watts_to_pct(watts: float, wchamax: float) -> float:
    """Percent of ``wchamax`` that ``watts`` is, clamped to 0..100."""
    return max(0.0, min(100.0, watts / wchamax * 100.0))


def pct_to_watts(pct: float, wchamax: float) -> float:
    return pct / 100.0 * wchamax


def resolve_rate(rate: Rate, setpoints: Mapping[str, float], wchamax: float) -> float:
    """The percent to write for ``rate``, given the watt setpoints and WChaMax.

    A limit setpoint that was never set means no limit; a grid power that
    was never set means none, which for the forced modes is a window of
    zero width until the user enters a power.
    """
    if rate is Rate.FULL:
        return 100.0
    if rate is Rate.ZERO:
        return 0.0
    if rate in (Rate.CHARGE_LIMIT, Rate.DISCHARGE_LIMIT):
        return watts_to_pct(setpoints.get(rate.value, wchamax), wchamax)
    return -watts_to_pct(setpoints.get(rate.value, 0.0), wchamax)
