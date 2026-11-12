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
from typing import Any


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
        ctl_mod: The ``StorCtl_Mod`` bits, 1 for the charge cap, 2 for the
            discharge cap.
        in_rate: What ``InWRte`` is set to.
        out_rate: What ``OutWRte`` is set to.
    """

    ctl_mod: int
    in_rate: Rate
    out_rate: Rate


@dataclass(frozen=True)
class StorageModeProfile:
    """How one vendor reads model 124, for the storage mode entities.

    Args:
        modes: The modes offered, in menu order.
        recipes: The writes per mode.
        infer_mode: Called as ``infer_mode(ctl_mod, in_pct, out_pct)`` with
            the raw ``StorCtl_Mod`` bits and both rates in percent, as the
            device reports them. Returns the mode the device is in, or None
            for a combination no recipe produces.
        grid_power_step_w: Step of the grid charge and discharge power
            entities, in watts.
        hidden_points: ``"model:point"`` keys of the generic controls that
            write the same registers in another unit. They ship disabled
            while this profile is active.
    """

    modes: tuple[StorageMode, ...]
    recipes: Mapping[StorageMode, Recipe]
    infer_mode: Callable[[int, float, float], StorageMode | None]
    grid_power_step_w: float
    hidden_points: frozenset[str]

    def rates_of(self, mode: StorageMode) -> frozenset[Rate]:
        """The setpoint rates ``mode`` writes.

        A change to one of them while the device is in ``mode`` has to be
        written to its point again.
        """
        recipe = self.recipes[mode]
        return frozenset({recipe.in_rate, recipe.out_rate}) & frozenset(SETPOINT_RATES)


class ModuleRole(StrEnum):
    """What a model 160 module carries, where the vendor's label says so.

    The values double as the unique id keys of the role-named sensors.
    """

    PV = "pv"
    BATTERY_CHARGE = "battery_charge"
    BATTERY_DISCHARGE = "battery_discharge"
    #: One module for both directions, its power unsigned; the storage
    #: model's charge state says which way. A Symo Hybrid's "String 2".
    BATTERY = "battery"


@dataclass(frozen=True)
class RawField:
    """One value in a raw register block.

    Args:
        name: The key in the decoded block.
        offset: Register offset from the block's start.
        kind: ``"uint16"``, ``"int16"``, ``"uint32"``, ``"int32"``,
            ``"float32"``, ``"uint64"`` or ``"string"``.
        length: Registers, for ``"string"``. Ignored otherwise.
        scale: What a decoded integer is multiplied by. SMA's fixed-point
            registers carry a value in 0.001 A or 0.01 V.
    """

    name: str
    offset: int
    kind: str
    length: int = 1
    scale: float = 1.0


@dataclass(frozen=True)
class RawBlock:
    """A run of registers outside the SunSpec models, read in one request.

    Args:
        key: Names the block in ``coordinator.raw_blocks``.
        address: The first register.
        count: Registers to read.
        fields: What the registers hold.
        word_order: ``"big"`` or ``"little"``: which 16-bit word of a 32- or
            64-bit value comes first. SunSpec is big; SolarEdge's own blocks
            are little.
        gate: ``(block key, field)`` of another block. This block is read
            only while that field is a number above zero: a battery slot
            whose nameplate says whether a battery is there.
        unit_id_offset: Added to the entry's unit id for this block. SMA
            serves its own registers 123 unit ids below the SunSpec ones.
        readable: False for a block of write-only setpoints that reads back
            nothing useful. It is never polled; its entities show what Home
            Assistant last wrote.
    """

    key: str
    address: int
    count: int
    fields: tuple[RawField, ...]
    word_order: str = "big"
    gate: tuple[str, str] | None = None
    unit_id_offset: int = 0
    readable: bool = True


@dataclass(frozen=True)
class RawDevice:
    """A device the vendor adds next to the SunSpec model devices, a battery for instance.

    Args:
        key: Names the device; the entities on it carry it in their unique
            id.
        info_block: The raw block with the identity strings, or None for a
            device that is the SunSpec device itself, such as a gateway
            whose own registers sit beside its models. Its identity fields
            then name points of common model 1.
        name: What follows the inverter's name in the device name.
        manufacturer: Field of ``info_block`` with the manufacturer.
        model: Field with the model.
        serial: Field with the serial number.
        version: Field with the firmware version, if any.
        requires: A block that has to answer for the device to exist at all.
            A SolarEdge battery slot has an identity block with strings even
            when no battery is there; the data block, gated on the rated
            energy, is what says there is one.
        model_options: Names for a model field the vendor numbers instead of
            spelling out. Kostal reports which battery is fitted as a type
            number, and 0x0004 is the BYD. None where the field holds the
            name itself.
    """

    key: str
    info_block: str | None
    name: str
    manufacturer: str
    model: str
    serial: str
    version: str | None = None
    requires: str | None = None
    model_options: Mapping[int, str] | None = None


@dataclass(frozen=True)
class RawSensor:
    """A sensor over one field of a raw block.

    Args:
        block: The raw block.
        field: The field in it.
        key: The translation key.
        device: ``"inverter"`` for the inverter's own device, or the key of
            a ``RawDevice``.
        unit: Home Assistant unit string.
        device_class: Home Assistant sensor device class value.
        state_class: ``"measurement"``, ``"total_increasing"`` or None.
        diagnostic: Filed under Diagnostic on the device card.
        options: For an enum: raw value to option name. A value not in the
            map reads as None.
        transform: Called as ``transform(value)`` with the decoded value.
            Returns what the sensor shows.
        enabled_by_default: False for a sensor the user turns on in the
            entity registry, for a field only some devices have.
    """

    block: str
    field: str
    key: str
    device: str = "inverter"
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    diagnostic: bool = False
    options: Mapping[int, str] | None = None
    transform: Callable[[Any], Any] | None = None
    enabled_by_default: bool = True


@dataclass(frozen=True)
class RawNumber:
    """A writable number over one field of a raw block.

    Args:
        block: The raw block.
        field: The field in it; its kind decides the encoding.
        key: The translation key.
        min: Smallest value the entity offers.
        max: Largest value.
        step: The entity's step.
        device: ``"inverter"`` or the key of a ``RawDevice``.
        unit: Home Assistant unit string.
        device_class: Home Assistant number device class value.
    """

    block: str
    field: str
    key: str
    min: float
    max: float
    step: float = 1.0
    device: str = "inverter"
    unit: str | None = None
    device_class: str | None = None


@dataclass(frozen=True)
class RawSelect:
    """A writable choice over one field of a raw block.

    Args:
        block: The raw block.
        field: The field in it.
        key: The translation key.
        options: Raw value to option name.
        device: ``"inverter"`` or the key of a ``RawDevice``.
        read: Called as ``read(raw)`` with the decoded field. Returns the
            raw value to look up in ``options``, for a field that carries
            more than the choice.
        write: Called as ``write(current, chosen)`` with the decoded field
            and the raw value of the chosen option. Returns what to write,
            so the other bits of the field survive.
    """

    block: str
    field: str
    key: str
    options: Mapping[int, str]
    device: str = "inverter"
    read: Callable[[Any], Any] | None = None
    write: Callable[[Any, int], int] | None = None


@dataclass(frozen=True)
class RawKeepAlive:
    """Fields to write again on a timer, behind a switch.

    For a device that forgets a setting: a remote command that lapses
    when its timeout runs out, a power limit that reverts on its own.
    While the switch is on, the fields are written again every
    ``interval_seconds`` with the value Home Assistant last wrote, or
    the value the device shows if it never did.

    Args:
        key: The switch's translation key.
        block: The raw block.
        fields: The fields written, in this order.
        interval_seconds: How often.
        only_while: Called as ``only_while(block_data)``. Returns whether
            the rewrite applies now, for a command that only means anything
            in a certain mode.
        settle_seconds: The pause between the fields' writes. SMA wants its
            control flag a moment before the setpoint.
        device: ``"inverter"`` or the key of a ``RawDevice``.
    """

    key: str
    block: str
    fields: tuple[str, ...]
    interval_seconds: float
    only_while: Callable[[Mapping[str, Any]], bool] | None = None
    settle_seconds: float = 0.0
    device: str = "inverter"


@dataclass(frozen=True)
class ModelRelabel:
    """A model header the vendor's firmware writes wrongly, and what it stands for.

    The scan walks the chain by the ids and lengths in the headers and
    keeps what it finds. The relabel runs afterwards, on a device the
    profile identified, and hands the registers behind the header to
    the model they are laid out as. Nothing is rewritten on the device.

    Args:
        found_id: The model id the header reads.
        found_len: The length the header declares.
        as_id: The model whose layout the registers follow.
        as_len: That model's length, for the body that is there.
    """

    found_id: int
    found_len: int
    as_id: int
    as_len: int


@dataclass(frozen=True)
class VendorProfile:
    """Everything the integration does differently for one manufacturer.

    Args:
        slug: Short lowercase name, for the diagnostics dump.
        manufacturer_prefixes: Compared case-insensitively against the start
            of ``Mn`` in common model 1. Prefixes, not names, because
            vendors are not consistent about their own name ("Fronius",
            "Fronius International GmbH").
        identifies: Called as ``identifies(model, option, version)`` with
            ``Md``, ``Opt`` and ``Vr`` of common model 1, empty where the
            device reports nothing. Returns whether this profile is the one
            for the device, for a manufacturer with more than one profile.
            None when the manufacturer name decides alone.
        storage: The battery mode vocabulary, when the vendor has one.
        module_role: Called as ``module_role(id_str, model)`` with the
            ``IDStr`` label of a model 160 module and ``Md`` of common model
            1. Returns what the module carries, or None for a label the
            vendor does not use that way. None when the vendor does not
            label its modules.
        enable_edge: Points the device takes a new value for only on the
            rising edge of an enable point, keyed ``(model_id, point)``,
            mapped to that enable point. Empty when the vendor applies a
            value as written.
        enable_edge_settle_seconds: The pause between writing such a value
            and raising its enable point again.
        rearm_by_default: Whether the off/on cycle runs unless the user
            switches it off. True where the manual gives the cycle as the
            procedure for a new value, False where it is a firmware quirk
            the user opts into.
        web_user: The local login of the device's web interface, when the
            integration speaks it (see ``fronius_web.py``). None for a
            vendor without one.
        raw_blocks: Registers outside the SunSpec models the vendor keeps
            device data in, read every cycle after the models. See
            ``raw_blocks.py``.
        raw_devices: Devices those blocks describe.
        raw_sensors: Sensors over those blocks.
        raw_numbers: Writable numbers over them.
        raw_selects: Writable choices over them.
        raw_keepalives: Fields rewritten on a timer, each behind a switch.
        volatile_registers: Addresses the device keeps in RAM. Writes there
            are not counted as flash writes.
        model_relabels: Headers this vendor's firmware labels with the wrong
            model, read as the model they stand for.
        unsupported_controls: ``"model:point"`` keys of controls the model
            defines and the device does not implement. No entity is built
            for them.
        ascii_flag_points: ``"model:point"`` keys of on/off points the
            firmware reads back as the ASCII code of the digit, 49 for "1"
            and 48 for "0".
        control_steps: ``"model:point"`` to the step of its Number entity,
            for a device that takes whole units where the scale factor would
            offer tenths.
        daily_energy_points: Names of energy points that count one day and
            start again at zero when the device first produces on the next,
            where SunSpec says lifetime. They are sensors without the guards
            of a lifetime counter.
        required_models: Models a profile's controls and sensors need,
            polled when the device exposes them.
        point_transforms: Read corrections keyed by model id and point name.
            Called with the SunSpec-scaled value, returning the corrected
            value. Raw registers and write encoding retain their SunSpec
            meaning.
        signed_nan_points: Unsigned points, keyed by model id and point
            name, that the firmware fills with 0x8000 when it has no
            reading. SunSpec reserves 0x8000 for "not implemented" only in
            signed registers; in a uint16 it is the number 32768. These
            points read as no value instead.
        request_timeout: Seconds the device may take to answer one request,
            for a device known to need more than the link's default of 10.
            None for the default.
        collection_round_seconds: The period of the rounds in which a
            gateway collects new values from its devices, starting on the
            clock. Set for a gateway that works through one request at a
            time and goes on working on one it left unanswered. After such a
            request the coordinator rests the gateway for every entry behind
            it, see ``GATEWAY_REST_MIN_SECONDS`` in const.py. None for a
            device that can be asked again at once.
    """

    slug: str
    manufacturer_prefixes: tuple[str, ...]
    identifies: Callable[[str, str, str], bool] | None = None
    storage: StorageModeProfile | None = None
    module_role: Callable[[str, str], ModuleRole | None] | None = None
    enable_edge: Mapping[tuple[int, str], str] = field(default_factory=dict)
    enable_edge_settle_seconds: float = 1.0
    rearm_by_default: bool = False
    web_user: str | None = None
    raw_blocks: tuple[RawBlock, ...] = ()
    raw_devices: tuple[RawDevice, ...] = ()
    raw_sensors: tuple[RawSensor, ...] = ()
    raw_numbers: tuple[RawNumber, ...] = ()
    raw_selects: tuple[RawSelect, ...] = ()
    raw_keepalives: tuple[RawKeepAlive, ...] = ()
    volatile_registers: frozenset[int] = frozenset()
    model_relabels: tuple[ModelRelabel, ...] = ()
    unsupported_controls: frozenset[str] = frozenset()
    ascii_flag_points: frozenset[str] = frozenset()
    control_steps: Mapping[str, float] = field(default_factory=dict)
    daily_energy_points: frozenset[str] = frozenset()
    required_models: frozenset[int] = frozenset()
    point_transforms: Mapping[tuple[int, str], Callable[[Any], Any]] = field(default_factory=dict)
    signed_nan_points: frozenset[tuple[int, str]] = frozenset()
    request_timeout: float | None = None
    collection_round_seconds: int | None = None

    def raw_block(self, key: str) -> RawBlock | None:
        """The raw block named ``key``, or None."""
        for block in self.raw_blocks:
            if block.key == key:
                return block
        return None

    def matches(self, manufacturer: str | None) -> bool:
        if not manufacturer:
            return False
        name = manufacturer.strip().lower()
        return any(name.startswith(prefix.lower()) for prefix in self.manufacturer_prefixes)


@dataclass(frozen=True)
class WriteStep:
    """One Modbus write of a sequence, and the pause after it.

    Args:
        points: The points written together.
        settle_seconds: How long to wait after this write before the next
            step. 0 for no wait.
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
        profile: The device's profile, or None.
        model_id: The model the points belong to.
        points: Point names and values, as the caller wants them written.
        is_on: Called as ``is_on(model_id, point)`` with an enable point.
            Returns whether the device last reported it on.
        rearm: Whether the off/on cycle is switched on for this entry.

    Returns:
        One step with the points as given, or three: the enable off, the
        values, the enable on again.
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
