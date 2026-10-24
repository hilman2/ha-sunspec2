# SolarEdge

This page covers SolarEdge inverters: HD-Wave, StorEdge, Energy Hub,
Home Hub and Synergy. You get the inverter's measurements, its
meters and, if you have one, the battery. You can also control the
battery and limit what you feed into the grid. The integration
recognises a SolarEdge inverter by itself. There is nothing to
configure.

## Before it works

The integration talks to the inverter over your home network, using
a feature of the inverter called **Modbus TCP**. SolarEdge ships it
switched off. You switch it on in the inverter's settings. In SetApp
(SolarEdge's installer app) open *Site Communication -> Modbus TCP*.
On an inverter with an LCD display open *Communication -> LAN Conf*.

1. Switch on **Modbus TCP**.
2. Note the **port**. A port is a number that tells the network which
   service on the inverter to talk to. Inverters set up with SetApp
   use **1502**. Inverters with an LCD use **502**. Enter that number
   when you add the integration in Home Assistant. If your installer
   changed the port, use the one they set.
3. **Add the integration within two minutes** of switching Modbus TCP
   on. If nobody connects in that time, the inverter closes the port
   again. In that case switch it on once more.
4. Keep in mind that the inverter accepts **only one connection at a
   time**. A second program that tries to connect is refused. If a
   connection breaks without being closed properly, the inverter
   keeps it reserved for two minutes, and nobody can connect until
   then. In practice: **no other program may read the inverter while
   Home Assistant does.** That includes a second Home Assistant
   integration for SolarEdge, an energy manager and any monitoring
   tool. Turn those off or point them at Home Assistant instead.

To see the battery and to control anything, SolarEdge has to switch
on one more thing for your inverter. See "The battery or the controls
are missing" below.

The inverter restarts itself once a night for about two minutes and
does not answer during that time. The integration handles that by
itself.

## What you get

### The battery

If your inverter has a battery, you get one device per battery. It is
named after the inverter and shows the battery's manufacturer, model
and serial number as the inverter reports them.

| Sensor | What it shows |
|---|---|
| **State of energy** | The charge level in percent |
| **State of health** | How much of its original capacity the battery still has, in percent. Diagnostic |
| **Temperature** | Average cell temperature. The highest cell temperature is a separate diagnostic sensor |
| **Voltage**, **Current**, **Power** | Power is positive while the battery charges and negative while it discharges |
| **Energy charged**, **Energy discharged** | Running totals in Wh. Use these for the battery entries of the Energy Dashboard |
| **Available energy**, **Maximum energy** | What the battery can give right now, and what it can hold as it ages |
| **Rated energy**, **Maximum charge power**, **Maximum discharge power** | The values on the battery's nameplate. Diagnostic |
| **Status** | Off, standby, charging, discharging, fault, idle or power saving |

### The inverter

These sensors are on the inverter's own device:

| Sensor | What it shows |
|---|---|
| **Grid status** | On grid or off grid. Only useful for a site with backup power |
| **Vendor status code** | The status and error code the way SetApp shows it, for example `18xBF`. Diagnostic |
| **RRCR input state** | The state of the four switching inputs that a grid operator can use to signal the inverter (a "ripple control receiver"). Shown as a number from 0 to 15. Diagnostic, mostly useful when you talk to support |
| **Register writes** | A counter of how often Home Assistant has changed one of the inverter's long-term settings. See the warning below. Diagnostic |

The inverter's meters show up in the same entry. This page adds
nothing to them.

### Battery controls

These controls are on the inverter's device. They only appear when
SolarEdge has enabled battery access for your inverter. There is no
option to tick.

| Entity | What it does |
|---|---|
| **Storage control mode** | The battery's overall operating mode: disabled, maximize self-consumption, time of use, backup only or **remote control**. The entities below only do something in remote control |
| **Storage AC charge policy** | Whether the battery may charge from the grid at all |
| **Storage AC charge limit** | How much it may charge from the grid. In kWh if the policy is a fixed energy limit, in percent if it is a percentage of production |
| **Storage backup reserve** | Charge level in percent that the battery keeps back for a power outage. Only on hardware with backup |
| **Storage default mode** | What the battery does when no command is in force |
| **Storage command mode** | What the battery does now, for as long as the **Storage command timeout** (in seconds) lasts |
| **Storage charge limit**, **Storage discharge limit** | The most power in watts the command may charge or discharge with |
| **Re-arm storage command** | A switch. While on, it repeats the timeout and the command every 15 minutes, so the command does not run out |

### Export controls

These controls limit what the site feeds into the grid. They are off
until you tick **Enable experimental export controls (BETA)** in the
integration's options (*Settings -> Devices & services -> SunSpec
Modbus -> Configure*). It is the same option that switches on the
export controls for other inverters, described in
[Battery and export control](write-controls.md). They only appear
when SolarEdge has enabled battery access for your inverter.

| Entity | What it does |
|---|---|
| **Site limit mode** | Which meter the limit is measured against: export control with an export/import meter, export control with a consumption meter, or production control (no meter) |
| **Site limit scope** | Whether the limit applies to the site total or to each phase separately |
| **Site limit** | The limit in watts. The inverter stores it permanently, so each change counts in **Register writes** |
| **Active power limit** | The limit as a percentage of the inverter's rated power. It takes effect at once but only lives in the inverter's working memory: it does not count in **Register writes**, it is gone after a restart, and on some firmware it is gone again within seconds |
| **Keep active power limit asserted** | A switch. While on, it sets the active power limit again every 10 seconds. Use it if your firmware lets the limit slip away |

## Controlling the battery

> **This can bite.** The battery settings are stored in a kind of
> memory (flash memory) that wears out when it is written too often.
> SolarEdge warns against changing these settings again and again.
> Every change Home Assistant makes to them is counted in the
> **Register writes** sensor, and the count survives restarts. Watch
> it. If it climbs quickly, something is writing too often. An
> automation that changes a setting on every update is exactly what
> the warning is about.

### Setting up remote control

Change the settings in this order:

1. **Storage control mode** to *Remote control*.
2. **Storage command timeout**.
3. **Storage command mode**.
4. **Storage charge limit** and **Storage discharge limit**.

Users of a similar community integration found that changing
everything at once makes the inverter keep its old limits. Leave a
few seconds between the steps (in an automation, add a short delay
between the actions). Some inverters answer with nonsense for a
moment after a change.

### Charging the battery from the grid

Set **Storage AC charge policy** to *Always allowed*, **Storage
command mode** to *Charge from solar and grid* and **Storage charge
limit** to the power in watts you want.

### Selling battery energy to the grid

Set **Storage command mode** to *Discharge to maximize export* and
**Storage discharge limit** to the power in watts you want.

### Holding the battery

Set **Storage command mode** to *Solar power only*.

### Going back to normal

Set **Storage control mode** to *Maximize self-consumption*.

### Making a command last

A command does not run forever. When the **Storage command timeout**
runs out, the inverter goes back to the **Storage default mode**. It
does the same after its nightly restart.

To keep a command going, turn on **Re-arm storage command**. It
repeats the timeout and the command every 15 minutes. Keep the
timeout above 15 minutes (900 seconds), or the command runs out
between two repeats. The switch repeats what you last set in Home
Assistant. If you never set anything, it repeats what the inverter
currently shows. It only does so while **Storage control mode** is
*Remote control*.

Do not repeat more often than every 15 minutes. Users of the
community integration found that this is what works. More often
overloads the connection to the inverter, and every repeat counts in
**Register writes**.

### When something else is in charge

A time schedule set in the SolarEdge monitoring portal, or a grid
program you signed up for, overrides everything here. It can undo
your command within seconds. After you change something, check the
entity to see whether the inverter really took the value.

## Limiting what you feed in

Turn on the export controls first (see "Export controls" above).
Then use:

- **Site limit mode**, **Site limit scope** and **Site limit** if
  your site has a meter that the inverter can use. The inverter keeps
  adjusting its output until the meter shows your limit.
- **Active power limit** (and, if it keeps reverting, **Keep active
  power limit asserted**) for a plain cap on the inverter's output.

Two cautions:

- **Do not set the site limit to 0 W on a site that has other power
  generation.** For one user this drove production to zero. On
  firmware 4.24.14 it stopped the whole system.
- **Do not set the active power limit to 0 %.** Users of the
  community integration use 1 % as the lowest value for the same
  reason.

## Things to know

### The battery or the controls are missing

SolarEdge switches battery access on **per inverter**, and nobody has
found a pattern for which inverters have it. Without it, the
battery, the site limit and the battery controls are simply not
there. To get it, ask SolarEdge support to enable "Modbus access to
the power control and battery registers" for your site. Users of the
community integration got it by asking for exactly that, and several
were refused.

### A feature that SolarEdge turns on later

On firmware 4.23 and newer, the integration may notice a missing
feature only after a short delay. It then checks again after an hour.
If SolarEdge switches something on for you later, it shows up within
an hour. You do not need to restart anything.

### Firmware updates

Firmware updates arrive without notice and have switched Modbus off
several times in 2026. If the inverter stops answering after an
update, switch Modbus TCP off and on again in SetApp. If that does
not help, switch the inverter off and on.

### Energy totals

On LG and BYD batteries the energy totals reset. On the 48 V
SolarEdge battery they sometimes go backwards. The Energy Dashboard
treats a drop as a reset of the counter.

### Two batteries

Two batteries stacked on one inverter show up as one battery with the
capacity of both.

## Reporting back

Nobody on the project owns a SolarEdge. If you do,
[open an issue](https://github.com/hilman2/ha-sunspec2/issues) and
tell us:

- the inverter model,
- its firmware version,
- whether the battery appeared,
- the `raw_blocks` part of the diagnostics download (*Settings ->
  Devices & services -> SunSpec Modbus -> Download diagnostics*). It
  lists which blocks of data the inverter answered.

---

## Technical details

You do not need this part to use the integration. It records where
the information comes from and what the integration does underneath.

### Sources

The registers and their meaning come from SolarEdge's technical notes
"SunSpec Logging" (v3.2, June 2025) and "Power Control Protocol for
SolarEdge Inverters" (v1.3, 2017), and from the community's
[solaredge-modbus-multi](https://github.com/WillCodeForCats/solaredge-modbus-multi)
integration (Apache-2.0), whose users have driven these registers
since 2021. Their findings are folded in above. The module
`custom_components/sunspec2/vendors/solaredge.py` has the register
map.

### What is SunSpec and what is not

SunSpec is the standard Modbus register layout the integration uses
for most inverters. A SolarEdge inverter speaks it for everything it
measures: common model 1, the inverter models 101 to 103, model 160
on Synergy units (the strings) and the meter models 201 to 204, each
meter with a common block of its own. The integration recognises
SolarEdge by the manufacturer name in model 1.

The battery and everything you can set are not in SunSpec. SolarEdge
keeps them in registers of its own. Every 32-bit value in those
blocks carries its low word first, which the SunSpec models do not.
The battery blocks start at register 0xE100, 0xE200 and 0xE400 (three
slots), each identity block followed 68 registers later by its data
block. The stride is uneven: 256 between the first two slots, 512 to
the third. A battery counts as present when its rated energy is above
zero, because firmware 4.18 stopped filling the model string and
shows a phantom second battery with strings but no energy.

The battery device takes manufacturer, model and serial from the
inverter. The meters are the inverter's own, read through it, and
appear as SunSpec meter models in the same entry.

### Modbus TCP rules

- SetApp inverters use port 1502, LCD inverters 502.
- The port closes again if nothing connects within two minutes of
  switching Modbus TCP on.
- The inverter accepts one Modbus TCP session at a time, refuses a
  second one and holds a dropped one for two minutes.
- The inverter restarts once a night for about two minutes and
  refuses TCP while it does.

### Blocks that SolarEdge has not enabled

The battery, storage control and site limit blocks are one feature
set that SolarEdge support switches on per inverter, with no pattern
to which inverters have it. An inverter without it answers a Modbus
exception on old firmware and, from firmware 4.23, nothing at all.
Each such silent block costs one read timeout. The integration then
leaves the block alone for an hour and tries again, so a block that
SolarEdge switches on later shows up within the hour, without a
restart.

### The controls and their registers

The storage control block holds: the control mode (disabled, maximize
self-consumption, time of use, backup only, remote control), the AC
charge policy and limit, the backup reserve, the default mode, the
command mode, the command timeout and the charge and discharge
limits. Command and default modes are solar power only, charge from
clipped solar, charge from solar, charge from solar and grid,
discharge to maximize export, discharge to minimize import and
maximize self-consumption. The AC charge policy is disabled, always
allowed, fixed energy limit or percent of production, and the limit
is in kWh for the fixed energy limit and in percent for the percent
of production.

The site limit mode lives in bits 0 to 2 of its register. The
integration keeps the other bits as they are when it writes.

Site limit and storage control are written to flash. The active power
limit is held in RAM. The *Register writes* sensor counts the writes
to the persistent registers of the inverter, across restarts. The
active power limit is not in that count.

### Write order and timing

Written all at once, the control mode, timeout, command mode and
limits left the inverter on the old limits. The order that works,
from the users of the community integration: control mode to remote
control, then the timeout, then the command mode, then the limits,
with a few seconds between the writes.

The remote command is written again every 15 minutes while
*Re-arm storage command* is on (`REMOTE_COMMAND_REARM_SECONDS`, 900
s). The community's users settled on 15 minutes with an hour of
timeout. Writing more often "chokes the modbus". The switch writes
what Home Assistant last set, or what the inverter shows if Home
Assistant never set anything, and only while the control mode is
remote control.

The active power limit is written again every 10 seconds while *Keep
active power limit asserted* is on
(`ACTIVE_POWER_LIMIT_REASSERT_SECONDS`), because firmware has been
seen to revert it after ten seconds.

### The site limit

The site limit is a control loop inside the inverter that seeks the
target through the meter. Setting it to 0 W on a site with other
generation drove production to zero for one user and, on firmware
4.24.14, stopped the whole system.

### Sensors

- *State of energy* is what SolarEdge calls SOE.
- *Vendor status code* is the controller and error code, for example
  `18xBF`.
- *RRCR input state* is the state of the four RRCR (ripple control
  receiver) inputs, as a number 0 to 15.
