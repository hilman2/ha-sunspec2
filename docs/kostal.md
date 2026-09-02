# Kostal

A Kostal inverter speaks SunSpec for what it measures and keeps a
second register set of its own alongside it, on the same port and the
same unit id. This page is about both: what to set in the inverter
before it answers, and what the integration reads on top of SunSpec.
The integration recognises the inverter by the manufacturer name it
reports, nothing to configure.

Covers PIKO IQ, PLENTICORE plus, PLENTICORE BI, PLENTICORE G2, G3 and
MP G3. The registers and their meaning come from KOSTAL's interface
description "MODBUS (TCP) & SunSpec, PIKO IQ / PLENTICORE with control
information", revision 2.9.

**Not the PIKO CI.** Kostal's commercial line has a register map of
its own, so it gets the generic SunSpec entities and none of this
page. Same for any other Kostal that does not report itself as a
PLENTICORE or a PIKO IQ.

## Before it works

1. Switch Modbus on in the inverter: *Settings -> Modbus / SunSpec
   (TCP) -> Activate Modbus*. It is off from the factory.
2. **Enter port 1502**, not the usual 502.
3. **Enter unit ID 71.** Both are Kostal's defaults; the unit id can
   be changed in the same menu.
4. Leave the byte order at **little-endian (CDAB)**, which is what the
   inverter ships with. The alternative, big-endian (ABCD), applies to
   Kostal's own registers only. An inverter switched to it still
   reports its SunSpec models correctly, because SunSpec fixes the
   word order itself, while everything on this page reads as nonsense.

## What SunSpec has

Models 1, 103, 113, 120, 123, 160, 203 and 802: the inverter's AC
measurements, the nameplate, the immediate controls, one block per PV
string, the meter, and the battery base model with its state of
charge, health, voltage, current, power and cycle count.

The inverter is there twice, as 103 with integer values and a scale
factor and as 113 with the same readings as float. The integration
ticks 103 and leaves 113 unticked, so each reading builds one sensor.
Both are in the model list in the options if you would rather have the
other one.

Model 203 needs PLENTICORE G2 from SW 02.12, G3 from 3.04.01 or MP G3
from 3.05.00. On older firmware the inverter reports no meter model
and the house consumption sensors below are the only view of it.

## What Kostal keeps in its own registers

**The house consumption, split by source.** How much of what the house
is drawing right now comes from the roof, from the battery and from
the grid, as three power sensors, each with its own lifetime energy
counter, plus the total. No SunSpec model carries this; it is the
split the inverter's own portal shows.

**The battery, as a device of its own**, named after the type the
inverter reports: BYD, BMZ, Pylontech, VARTA, Dyness, LG, AXIstorage,
PIKO Battery Li, ZYC, HELIVOR or M-TEC. On it:

| Sensor | Why it is not in SunSpec |
|---|---|
| Temperature | Model 802 has no temperature field |
| Charged and discharged, DC side | 802 counts no energy at all |
| Charged, AC side | |
| Discharged to grid | Separates giving back from self-consumption |
| Charged from grid | Says whether a charge came from the roof or was bought |
| Work capacity, maximum charge and discharge power | What the battery reports it can take |
| Ready, external management | Diagnostics, see below |

**Diagnostics on the inverter**: the energy manager's own state
(idle, emergency battery charge, winter mode), the insulation
resistance, the temperature of the controller board, the string fuse
board, which energy meter is paired with the inverter, and the power
limit the grid operator currently allows in percent.

An inverter without a battery gets none of the battery entities: the
inverter reports battery type 0, and the blocks behind it are never
read.

## Steering the battery

Four controls sit on the battery device from the start, because they
bound what the battery does rather than drive it:

| Control | What it does |
|---|---|
| Minimum state of charge | How much the battery keeps back |
| Maximum state of charge | How full it is allowed to get |
| Charge power limit | Caps charging, in watts |
| Discharge power limit | Caps discharging |

**The DC power setpoint** is the one that actually drives the battery,
and it is behind **"Enable experimental export controls (BETA)"** in
the options. Negative charges, positive discharges, which is Kostal's
convention and the opposite of the generic battery controls. Two
things to know before using it: the inverter only acts on it while
*External management* reads "Modbus", and Kostal names no timeout for
this register, so a value written here is one the inverter keeps. An
automation that sets it and then stops leaves the battery where it put
it.

*External management* is read-only over Modbus. Set it in the
inverter itself, under the external battery management setting, or the
setpoint is read back and ignored.

### Holding a limit on a G3

PLENTICORE G3 from SW 03.05 has a second pair of power limits with a
watchdog behind them, and the integration offers them as *Held charge
power limit* and *Held discharge power limit* with a **Hold the power
limits** switch.

The watchdog is the point. Once written, those two registers have to
be written again and again; stop, and after *Time until fallback* the
inverter falls back to *Charge power after fallback* and *Discharge
power after fallback*. The switch is what does the rewriting, every 20
seconds, which is under the 30 second minimum the inverter accepts for
the fallback time.

That makes this the safe way to cap the battery from an automation. A
Home Assistant that goes down, or a switch turned off, hands the
battery back to the inverter within the fallback time instead of
leaving a limit behind.

An older PLENTICORE answers a Modbus exception at these registers, and
neither the four entities nor the switch appear.

## Things to know

**The energy counters are lifetime totals** and go into the Energy
dashboard as they are.

**Only the inverter serves these registers.** A KOSTAL Smart Energy
Meter on the same network has a Modbus interface of its own that this
integration does not read; what the inverter reports about it is the
meter model and the meter's own totals.

## Reporting back

Nobody on the project has a Kostal. Every register on this page comes
from the interface description rather than from a device, so a report
that a sensor reads what the inverter's portal shows, or that it does
not, is what tells us it works. Open an issue with a diagnostics
download from the integration's device page.
