# SMA

An SMA inverter speaks SunSpec for what it measures and keeps the
rest in a Modbus profile of its own, on the same port under another
unit id. This page is about both: where SunSpec answers, and what the
integration reads and writes in SMA's own registers on top. The
integration recognises the inverter by the manufacturer name it
reports, nothing to configure.

The registers and their meaning come from SMA's technical information
"SunSpec Modbus" and "SMA Modbus", from SMA's per-device register
lists, and from what the users of the evcc templates and the
photovoltaikforum threads found out on their own hardware. Their
findings are folded in below.

## Before it works

1. Switch on Modbus in the inverter. Speedwire generation (Sunny Boy
   -41, Tripower -40, Core1): installer login, *Device parameters ->
   External communication -> Modbus -> TCP server*. ennexOS generation
   (Tripower X, Smart Energy, Sunny Island X, Core2): *Configuration ->
   External communication -> Modbus server*. It is off from the
   factory and off again after a factory reset.
2. **Enter unit ID 126.** SunSpec answers on SMA's unit id plus 123,
   and SMA's factory unit id is 3. Unit id 1 or 3 answer with a Modbus
   error or with "not a SunSpec device". The integration tries 126 on
   its own when the id you entered fails, and tells you so.
3. Port 502.

Four Modbus connections at most; one that goes idle is dropped after
two hours. An inverter that stops answering after weeks and only
comes back after an AC and DC power cycle has been seen on several
models; SMA restarts some of them once a week at night for the same
reason.

## What SunSpec has

Speedwire generation: models 1, 101 to 103, 120 to 123 and 160, all
integer with scale factors. ennexOS generation: 1, 123 and 701 to 714,
with 713 (the storage) only on battery inverters and 714 only on
Tripower X; no 101 to 103 and no 160 on older firmware, both appended
behind the 700 series on newer. Sunny Boy Storage and Sunny Island
add model 124, the basic storage control, so their charge and
discharge rates and the control mode are the generic battery
controls of [write-controls.md](write-controls.md).

Model 123 export limits work from profile 1.1 on without the Grid
Guard code; model 704 carries them on ennexOS. SMA names those, and
only those, as safe to write on a timer.

## What SMA's own registers add

On the inverter's device, read from unit id 3:

| Entity | What it shows |
|---|---|
| Operating status | Ok, warning, fault or off, the way Sunny Portal shows it |
| Battery state of charge, capacity, temperature, voltage, current | The battery on a Sunny Boy Storage, Sunny Island or Tripower Smart Energy. Capacity is the battery's health in percent |
| Battery charge power, discharge power, energy charged, energy discharged | Watts each way and the lifetime counters, for the Energy Dashboard |
| Battery rated energy | The nameplate. Diagnostic |
| Active power operating mode | Off, watts, percent or **external setpoint**. The setpoint below needs the last one. Diagnostic |
| External setpoint timeout | Seconds until the inverter drops a setpoint it has not heard about. Diagnostic |
| Device class, device type code | What the inverter says it is. Diagnostic |

## Steering the battery

> **This can bite.** SMA warns that its RW parameters wear the flash
> when written on a timer. The two registers below are the ones SMA
> lets a controller write cyclically; the *Register writes* sensor
> stays at zero for them and counts everything else.

The external active power setpoint is what evcc and the community's
controllers use on Sunny Island, Sunny Boy Storage and Tripower Smart
Energy. Positive watts discharge, negative watts charge, from the
grid if the roof does not cover it.

| Entity | What it does |
|---|---|
| External power control | *Active* hands the inverter's power to the setpoint below, *Inactive* hands it back |
| External power setpoint | The watts, negative to charge |
| Keep external power setpoint | Writes the control flag and the setpoint again every 30 seconds while on, so the inverter does not drop them at its timeout |

What the inverter needs first, once, in its web interface:
*Operating mode active power* set to *External setpoint*, which the
*Active power operating mode* sensor shows as `external_setpoint`.
That parameter needs the installer login and, after the first ten
operating hours, the Grid Guard code; a Sunny Home Manager with
forecast-based charging enabled in Sunny Portal overrides the
setpoint, so switch that off too.

The order that works: control to *Active*, a second later the
setpoint. Written the other way round the inverter ignores the value.
The inverter forgets the setpoint after its timeout, ten or thirty
minutes from the factory, and falls back to its own logic; *Keep
external power setpoint* renews both every 30 seconds while it is on.
Writing more often than every two seconds has hung a Tripower Smart
Energy until it was power cycled.

The registers read back nothing; the entities show what Home
Assistant last wrote. Sunny Boy Smart Energy accepts the writes and
ignores them on the firmware seen so far; its control is not built in.

## Things to know

- **Night.** Tripower X powers its CPU down after production stops:
  the SunSpec models vanish and come back at dawn. Older Sunny Boys
  refuse the connection at night. The integration keeps the last
  values and does not raise the "unreachable" repair for a device
  that said it was going to sleep.
- **Model 714 comes and goes** on Tripower X after firmware updates;
  the integration tracks vanished models and picks them up again.
- **Meters.** The SMA Energy Meter and the Sunny Home Manager speak
  Speedwire multicast, not Modbus. Grid data over Modbus comes only
  through an inverter that has one of them attached.
- **Hybrids report AC power including the battery.** PV power on a
  Smart Energy is the sum of the DC strings in model 160.
- **Firmware updates** have changed which models appear and have
  broken SunSpec on Tripower X until a power cycle (03.02.11.R).

## Reporting back

Nobody on the project has an SMA. If you run one,
[open an issue](https://github.com/hilman2/ha-sunspec2/issues) with the
inverter model, its firmware version, which entities appeared, and
the `raw_blocks` part of the diagnostics download.
