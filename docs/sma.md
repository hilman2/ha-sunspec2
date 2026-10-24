# SMA

This page covers SMA inverters: Sunny Boy, Sunny Boy Storage, Sunny
Island, Sunny Tripower, Tripower X, Tripower Smart Energy and Sunny Boy
Smart Energy. It tells you what to switch on in the inverter, which
sensors and controls you get, and how to steer an SMA battery from Home
Assistant.

The integration recognises an SMA inverter by the manufacturer name it
reports. You configure nothing for that.

## Before it works

1. Switch on Modbus in the inverter. Modbus is the language Home
   Assistant uses to talk to it. SMA has two families of inverters, and
   the menus differ:
   - **Speedwire family** (Sunny Boy -41, Tripower -40, Core1): log in
     as installer, then *Device parameters -> External communication ->
     Modbus -> TCP server*.
   - **ennexOS family** (Tripower X, Smart Energy, Sunny Island X,
     Core2): *Configuration -> External communication -> Modbus
     server*.

   Modbus is off from the factory and off again after a factory reset.
2. **Enter unit ID 126.** The unit ID is the address of the device on
   the inverter's network connection. The standard data answers on 126.
   If you enter 1 or 3 instead, the inverter answers with an error or
   the integration reports "not a SunSpec device". If the unit ID you
   entered fails, the integration tries 126 on its own and tells you so.
3. Use port 502. The port is the "door" on the inverter's network
   address that it listens on.

The inverter accepts four Modbus connections at most, and it drops one
that has been idle for two hours. Keep that in mind if other programs
also talk to it.

## What you get

**The standard inverter values.** The inverter's AC readings, the
readings of the individual PV strings (the groups of solar panels wired
to the inverter), and the battery where there is one. The exact set
depends on the family and the firmware.

**Extra entities from SMA's own interface.** The integration reads
these itself, from a second address (unit ID 3) on the same inverter.
You do not enter anything for it. They appear on the inverter's device:

| Entity | What it shows |
|---|---|
| Operating status | Ok, warning, fault or off, the way Sunny Portal shows it |
| Battery state of charge, capacity, temperature, voltage, current | The battery on a Sunny Boy Storage, Sunny Island or Tripower Smart Energy. Capacity is the battery's health in percent |
| Battery charge power, discharge power, energy charged, energy discharged | Watts each way and the lifetime counters, for the Energy dashboard |
| Rated energy | The size of the battery as printed on its nameplate. Diagnostic |
| Active power operating mode | Off, watts, percent or **external setpoint**. The setpoint control below needs the last one. Diagnostic |
| External setpoint timeout | Seconds until the inverter drops a setpoint it has not heard about. Diagnostic |
| Device class, device type code | What the inverter says it is. Diagnostic |

**Controls.** Sunny Boy Storage and Sunny Island also get the generic
battery controls: charge and discharge rates and the control mode. They
are described in [write-controls.md](write-controls.md). The export
limit works on current firmware without the installer's Grid Guard
code (a code that unlocks protected grid settings), see Technical
details. SMA names those export limits, and only those, as safe to
write on a timer. See
[Battery and export control](../README.md#battery-and-export-control).

## How to use it

### Steering the battery from an automation

> **This can bite.** The inverter stores some of its settings in a
> memory that wears out when it is written too often. SMA warns about
> this for its settings in general. The two controls below are the ones
> SMA lets a controller write on a timer. The *Register writes* sensor
> stays at zero for them and counts every other write.

This is the same method that evcc and the community's controllers use
on Sunny Island, Sunny Boy Storage and Tripower Smart Energy. Positive
watts discharge the battery. Negative watts charge it, from the grid if
the roof does not cover it.

| Entity | What it does |
|---|---|
| **External power control** | *Active* hands the inverter's power to the setpoint below, *Inactive* hands it back |
| **External power setpoint** | The watts, negative to charge |
| **Keep external power setpoint** | While on, repeats the control and the setpoint every 30 seconds, so the inverter does not drop them when its timeout runs out |

Do this once in the inverter's web interface first:

1. Log in as installer and set *Operating mode active power* to
   *External setpoint*. The sensor **Active power operating mode** then
   shows `external_setpoint`.
2. Enter the Grid Guard code if the inverter asks for it. That
   parameter needs it after the first ten operating hours.
3. If you use a Sunny Home Manager with forecast-based charging
   enabled in Sunny Portal, switch that off. It overrides the setpoint.

Then, in Home Assistant:

1. Set **External power control** to *Active*.
2. A second later, set **External power setpoint**, for example
   -2000 for charging at 2000 W. Written the other way round, the
   inverter ignores the value.
3. Turn on **Keep external power setpoint**. Without it, the inverter
   forgets the setpoint after its timeout, ten or thirty minutes from
   the factory, and goes back to its own logic.

Do not write to these more often than every two seconds. That has hung
a Tripower Smart Energy until it was power cycled (switched off at the
AC and DC side, then on again).

The controls cannot read back what the inverter holds. They show what
Home Assistant last wrote.

**Sunny Boy Smart Energy** accepts the writes and ignores them on the
firmware seen so far. Its control is not built in.

## Things to know

- **Night.** Tripower X powers its CPU down after production stops. Its
  data vanishes and comes back at dawn. Older Sunny Boys refuse the
  connection at night. The integration keeps the last values and does
  not raise the "unreachable" repair for a device that said it was going
  to sleep.
- **Some data comes and goes** on Tripower X after firmware updates.
  The integration notices when it disappears and picks it up again.
- **Energy meters.** The SMA Energy Meter and the Sunny Home Manager
  do not use Modbus. Home Assistant gets grid data only through an
  inverter that has one of them attached.
- **Hybrid inverters report AC power including the battery.** PV power
  on a Smart Energy is the sum of its DC strings.
- **An inverter that stops answering** after weeks and only comes back
  after a power cycle has been seen on several models. SMA restarts some
  of them once a week at night for that reason.
- **Firmware updates** have changed what the inverter reports. One
  (03.02.11.R) broke the standard data on Tripower X until a power
  cycle.

## Reporting back

Nobody on the project has an SMA. If you run one,
[open an issue](https://github.com/hilman2/ha-sunspec2/issues) with the
inverter model, its firmware version, which entities appeared, and
the `raw_blocks` part of the diagnostics download.

---

## Technical details

You do not need this section to use the integration.

### Two Modbus profiles

An SMA inverter speaks SunSpec for what it measures and keeps the rest
in a Modbus profile of its own, on the same port under another unit id.
SunSpec answers on SMA's unit id plus 123, and SMA's factory unit id is
3. That is why SunSpec lives on 126 and the extra entities are read from
unit id 3.

### Sources

The registers and their meaning come from SMA's technical information
"SunSpec Modbus" and "SMA Modbus", from SMA's per-device register lists,
and from what the users of the evcc templates and the photovoltaikforum
threads found out on their own hardware. Their findings are folded into
the integration.

### SunSpec models

Speedwire generation: models 1, 101 to 103, 120 to 123 and 160, all
integer with scale factors.

ennexOS generation: 1, 123 and 701 to 714, with 713 (the storage) only
on battery inverters and 714 only on Tripower X. There is no 101 to 103
and no 160 on older firmware, both are appended behind the 700 series
on newer.

Sunny Boy Storage and Sunny Island add model 124, the basic storage
control, so their charge and discharge rates and the control mode are
the generic battery controls of [write-controls.md](write-controls.md).

Model 123 export limits work from profile 1.1 on without the Grid Guard
code. Model 704 carries them on ennexOS. SMA names those, and only
those, as safe to write on a timer.

Model 714 comes and goes on Tripower X after firmware updates. The
integration tracks vanished models and picks them up again.

PV power on a Smart Energy is the sum of the DC strings in model 160.
Hybrids report AC power including the battery.

The SMA Energy Meter and the Sunny Home Manager speak Speedwire
multicast, not Modbus.

Tripower X powers its CPU down after production stops: the SunSpec
models vanish and come back at dawn. Firmware 03.02.11.R broke SunSpec
on Tripower X until a power cycle.

### The external setpoint

The setpoint registers read back nothing; the entities show what
Home Assistant last wrote.

The *Register writes* sensor counts writes to SMA's RW parameters,
which wear the flash when written on a timer. The external control flag
and setpoint are the two registers SMA lets a controller write
cyclically, so they are not counted.

The parameter *Operating mode active power* needs the installer login
and, after the first ten operating hours, the Grid Guard code. The
*Active power operating mode* sensor shows its value as
`external_setpoint`.

