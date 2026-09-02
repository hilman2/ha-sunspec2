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
from dataclasses import field
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


class ModuleRole(StrEnum):
    """What a model 160 module carries, where the vendor's label says so.

    The values double as the unique id keys of the role-named sensors.
    """

    PV = "pv"
    BATTERY_CHARGE = "battery_charge"
    BATTERY_DISCHARGE = "battery_discharge"


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
        module_role (Callable[[str], ModuleRole|None]|None): Called as
            ``module_role(id_str)`` with the ``IDStr`` label of a model
            160 module. Returns what the module carries, or None for a
            label the vendor does not use that way. None when the vendor
            does not label its modules.
        enable_edge (Mapping[tuple[int, str], str]): Points the device
            takes a new value for only on the rising edge of an enable
            point, keyed ``(model_id, point)``, mapped to that enable
            point. Empty when the vendor applies a value as written.
        enable_edge_settle_seconds (float): The pause between writing
            such a value and raising its enable point again.
        web_user (str|None): The local login of the device's web
            interface, when the integration speaks it (see
            ``fronius_web.py``). None for a vendor without one.
    """

    slug: str
    manufacturer_prefixes: tuple[str, ...]
    storage: StorageModeProfile | None = None
    module_role: Callable[[str], ModuleRole | None] | None = None
    enable_edge: Mapping[tuple[int, str], str] = field(default_factory=dict)
    enable_edge_settle_seconds: float = 1.0
    web_user: str | None = None

    def matches(self, manufacturer: str | None) -> bool:
        if not manufacturer:
            return False
        name = manufacturer.strip().lower()
        return any(name.startswith(prefix.lower()) for prefix in self.manufacturer_prefixes)


@dataclass(frozen=True)
class WriteStep:
    """One Modbus write of a sequence, and the pause after it.

    Args:
        points (list[tuple[str, object]]): The points written together.
        settle_seconds (float): How long to wait after this write before
            the next step. 0 for no wait.
    """

    points: list[tuple[str, object]]
    settle_seconds: float = 0.0


def plan_write(
    profile: VendorProfile | None,
    model_id: int,
    points: list[tuple[str, object]],
    is_on: Callable[[int, str], bool],
    rearm: bool,
) -> list[WriteStep]:
    """The write steps for ``points``, with the enable edge where the vendor needs one.

    Args:
        profile (VendorProfile|None): The device's profile, or None.
        model_id (int): The model the points belong to.
        points (list[tuple[str, object]]): Point names and values, as
            the caller wants them written.
        is_on (Callable[[int, str], bool]): Called as
            ``is_on(model_id, point)`` with an enable point. Returns
            whether the device last reported it on.
        rearm (bool): Whether the user opted into the off/on cycle.

    Returns:
        list[WriteStep]: One step with the points as given, or three:
        the enable off, the values, the enable on again.
    """
    plain = [WriteStep(points)]
    if profile is None or not rearm or not profile.enable_edge:
        return plain
    requested = dict(points)
    enables: list[str] = []
    for name, _ in points:
        enable = profile.enable_edge.get((model_id, name))
        if enable is not None and enable not in enables:
            enables.append(enable)
    # The cycle is for an enable that is on after this write, whether
    # the write says so or it is on already and the write leaves it
    # alone. An enable the write turns off needs no edge, and one that
    # is off now and goes on with this write gets its edge from the
    # write itself, the value going out first.
    ends_on: list[str] = []
    for enable in enables:
        if enable in requested:
            if requested[enable]:
                ends_on.append(enable)
        elif is_on(model_id, enable):
            ends_on.append(enable)
    off: list[tuple[str, object]] = [(enable, 0) for enable in ends_on if is_on(model_id, enable)]
    if not off:
        return plain
    values = [(name, value) for name, value in points if name not in ends_on]
    on: list[tuple[str, object]] = [(enable, 1) for enable in ends_on]
    return [
        WriteStep(off),
        WriteStep(values, profile.enable_edge_settle_seconds),
        WriteStep(on),
    ]


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
