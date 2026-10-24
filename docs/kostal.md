# Kostal

This page covers Kostal PIKO IQ, PLENTICORE plus, PLENTICORE BI,
PLENTICORE G2, G3 and MP G3 inverters. It tells you what to switch on
in the inverter so Home Assistant can talk to it, which sensors and
controls you get, and how to use the battery controls.

The integration recognises a Kostal inverter by the manufacturer name
it reports. You configure nothing for that.

**Not the PIKO CI.** Kostal's commercial line works differently, so it
gets the generic entities and none of the extras on this page. The same
goes for any other Kostal that does not call itself a PLENTICORE or a
PIKO IQ.

## Before it works

1. Switch Modbus on in the inverter: *Settings -> Modbus / SunSpec
   (TCP) -> Activate Modbus*. Modbus is the language Home Assistant
   uses to talk to the inverter, and it is off from the factory.
2. **Enter port 1502** when you add the integration, not the usual
   502. The port is the "door" on the inverter's network address that
   the inverter listens on.
3. **Enter unit ID 71.** The unit ID is the address of the inverter
   behind that door. Both numbers are Kostal's defaults. You can change
   the unit ID in the same menu.
4. Leave the byte order at **little-endian (CDAB)**, which is how the
   inverter ships. The other choice, big-endian (ABCD), makes the
   Kostal-specific values on this page read as nonsense. The standard
   values (power, voltage, battery level) stay correct either way, so a
   wrong setting is easy to miss.

## What you get

**The standard inverter values.** AC power and voltage, the nameplate
data, one set of readings per PV string (the groups of solar panels
wired to the inverter), the energy meter, and the battery's state of
charge, health, voltage, current, power and number of charge cycles.

If your inverter is a PLENTICORE G2, G3 or MP G3, the meter values need
a recent enough firmware: G2 from SW 02.12, G3 from 3.04.01, MP G3 from
3.05.00. On older firmware the inverter reports no meter, and the house
consumption sensors below are the only view of your consumption.

**The house consumption, split by source.** How much of what the house
is drawing right now comes from the roof, from the battery and from the
grid. You get three power sensors, each with its own lifetime energy
counter, plus the total. Only Kostal's own interface has this split. It
is the same split the inverter's own portal shows.

**The battery, as a device of its own**, named after the type the
inverter reports: BYD, BMZ, Pylontech, VARTA, Dyness, LG, AXIstorage,
PIKO Battery Li, ZYC, HELIVOR or M-TEC. On it, beyond the standard
values above:

| Sensor | What it shows |
|---|---|
| Temperature | The battery's temperature |
| Charged and discharged, DC side | Lifetime energy in and out of the battery |
| Charged, AC side | Lifetime energy the battery took from the AC side |
| Discharged to grid | Energy the battery gave back to the grid, as opposed to what the house used itself |
| Charged from grid | Whether a charge came from the roof or was bought from the grid |
| Work capacity, maximum charge and discharge power | What the battery reports it can hold and take |
| Ready | Whether the battery is ready. Diagnostic |
| External management | Who is allowed to steer the battery, see below. Diagnostic |

**Diagnostics on the inverter**: the state of the energy manager (for
example idle, emergency battery charge or winter mode), the insulation
resistance, the temperature of the controller board, the string fuse
board, which energy meter is paired with the inverter, and the power
limit your grid operator currently allows, in percent.

An inverter without a battery gets none of the battery entities.

The energy counters are lifetime totals. You can add them to the Energy
dashboard as they are.

## What you can control

Four controls sit on the battery device from the start. They set
boundaries for the battery rather than drive it:

| Control | What it does |
|---|---|
| **Minimum state of charge** | How much the battery keeps back |
| **Maximum state of charge** | How full it is allowed to get |
| **Charge power limit** | Caps charging, in watts |
| **Discharge power limit** | Caps discharging, in watts |

One more control, **DC power setpoint**, drives the battery directly.

## How to use it

### Keeping the battery from draining below 20 %

Set **Minimum state of charge** to 20. The battery keeps that share
back. **Maximum state of charge** works the same way for the top end,
for example to stop charging at 90 %.

### Slowing down charging or discharging

Set **Charge power limit** or **Discharge power limit** to the most
watts you want the battery to take or give.

### Telling the battery to charge or discharge a fixed amount

> **This can bite.** The inverter keeps the value you write here
> until something else changes it. Kostal names no time limit for it. An
> automation that sets a value and then stops leaves the battery
> charging or discharging at that rate until you change it.

1. In the inverter's own settings, set the external battery management
   so that the inverter takes commands over Modbus. Home Assistant
   cannot change this setting, it can only show it. The sensor
   **External management** must read "Modbus". If it does not, the
   inverter reads your setpoint back and ignores it.
2. Set **DC power setpoint** in watts. Negative charges the battery,
   positive discharges it. Kostal does it this way round, which is the
   opposite of the generic battery controls in other docs.

### Capping the battery from an automation, safely (PLENTICORE G3)

PLENTICORE G3 from SW 03.05 has a second pair of limits with a safety
net behind them. The integration offers them as **Held charge power
limit** and **Held discharge power limit**, with a **Hold the power
limits** switch.

The safety net works like this. The inverter only keeps these limits
while Home Assistant keeps confirming them. The switch does that every
20 seconds. If Home Assistant goes down, or you turn the switch off,
the inverter drops the limits after the time set in **Time until
fallback** and uses **Charge power after fallback** and **Discharge
power after fallback** instead. The battery never stays stuck on a
limit you forgot about.

That makes this the safe way to cap the battery from an automation.

An older PLENTICORE does not support this. The four entities and the
switch do not appear for it.

## Things to know

- **Only the inverter is read.** A KOSTAL Smart Energy Meter on the
  same network has its own Modbus interface, which this integration does
  not read. What you see about the meter is what the inverter reports
  about it: the meter type and its own totals.

## Reporting back

Nobody on the project has a Kostal. Everything on this page comes from
Kostal's interface description rather than from a real device. A report
that a sensor shows what the inverter's portal shows, or that it does
not, is what tells us it works. Open an issue and attach a diagnostics
download from the integration's device page.

---

## Technical details

You do not need this section to use the integration.

### Sources

The registers and their meaning come from KOSTAL's interface
description "MODBUS (TCP) & SunSpec, PIKO IQ / PLENTICORE with control
information", revision 2.9.

### Two register sets

A Kostal inverter speaks SunSpec for what it measures and keeps a
second register set of its own alongside it, on the same port and the
same unit id.

The PIKO CI has a register map of its own, so it gets the generic
SunSpec entities only.

### Byte order

Little-endian (CDAB) is the inverter's factory setting. The
alternative, big-endian (ABCD), applies to Kostal's own registers only.
An inverter switched to it still reports its SunSpec models correctly,
because SunSpec fixes the word order itself, while everything read from
Kostal's own registers reads as nonsense.

### SunSpec models

The inverter offers models 1, 103, 113, 120, 123, 160, 203 and 802:
the AC measurements, the nameplate, the immediate controls, one block
per PV string, the meter, and the battery base model with its state of
charge, health, voltage, current, power and cycle count.

The inverter is there twice, as 103 with integer values and a scale
factor and as 113 with the same readings as float. The integration
ticks 103 and leaves 113 unticked, so each reading builds one sensor.
Both are in the model list in the options if you would rather have the
other one.

Model 203 needs PLENTICORE G2 from SW 02.12, G3 from 3.04.01 or MP G3
from 3.05.00. On older firmware the inverter reports no meter model.

No SunSpec model carries the house consumption split by source.

### Why the battery sensors are not in SunSpec

| Sensor | Why it is not in SunSpec |
|---|---|
| Temperature | Model 802 has no temperature field |
| Charged and discharged, DC side | 802 counts no energy at all |
| Charged, AC side | |
| Discharged to grid | Separates giving back from self-consumption |
| Charged from grid | Says whether a charge came from the roof or was bought |
| Work capacity, maximum charge and discharge power | What the battery reports it can take |
| Ready, external management | Diagnostics |

### Battery type 0

An inverter without a battery reports battery type 0, and the blocks
behind it are never read.

### External management

*External management* is read-only over Modbus. It is set in the
inverter itself, under the external battery management setting.

### DC power setpoint

Kostal names no timeout for this register, so a value written here is
one the inverter keeps. The inverter only acts on it while *External
management* reads "Modbus".

### The watchdog behind the held limits

PLENTICORE G3 from SW 03.05 has a second pair of power limit registers
with a watchdog behind them. Once written, those two registers have to
be written again and again. If that stops, then after *Time until
fallback* the inverter falls back to *Charge power after fallback* and
*Discharge power after fallback*.

The *Hold the power limits* switch does the rewriting every 20 seconds,
which is under the 30 second minimum the inverter accepts for the
fallback time. A Home Assistant that goes down, or a switch turned off,
hands the battery back to the inverter within the fallback time instead
of leaving a limit behind.

An older PLENTICORE answers a Modbus exception at these registers, and
neither the four entities nor the switch appear.
