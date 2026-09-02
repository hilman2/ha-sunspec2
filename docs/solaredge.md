# SolarEdge

A SolarEdge inverter speaks SunSpec for everything it measures: the
inverter, its meters, the strings of a Synergy unit. What it does not
put into SunSpec is the battery and everything you can set, and this
page is about what the integration reads and writes in SolarEdge's
own registers on top. The integration recognises the inverter by the
manufacturer name it reports, nothing to configure.

The registers and their meaning come from SolarEdge's technical notes
"SunSpec Logging" and "Power Control Protocol", and from the
community's
[solaredge-modbus-multi](https://github.com/WillCodeForCats/solaredge-modbus-multi)
integration, whose users have driven them since 2021. Their findings
are folded in below.

## Before it works

On the inverter, in SetApp under *Site Communication -> Modbus TCP*,
or on an LCD inverter under *Communication -> LAN Conf*:

1. Switch on **Modbus TCP**. SetApp inverters use port **1502**, LCD
   inverters 502, unless the installer changed it.
2. **Connect within two minutes** of switching it on, or the port
   closes again.
3. The inverter accepts **one Modbus connection**. A second one is
   refused, and a dropped one is held for two minutes before the next
   connect works. Nothing else may read the inverter at the same time.

The inverter restarts once a night, for about two minutes, and refuses
Modbus while it does. The integration rides that out.

## What the battery shows

An inverter with a battery gets one device per battery, named after
the inverter, with the battery's manufacturer, model and serial from
the inverter.

| Sensor | What it shows |
|---|---|
| State of energy | The charge level in percent, what SolarEdge calls SOE |
| State of health | In percent. Diagnostic |
| Temperature | Average cell temperature; the maximum is a diagnostic sensor |
| Voltage, Current, Power | Power is positive while the battery charges, negative while it discharges |
| Energy charged, Energy discharged | Lifetime counters in Wh, for the Energy Dashboard's battery entries |
| Available energy, Maximum energy | What the battery can give now, and what it can hold as it ages |
| Rated energy, Maximum charge power, Maximum discharge power | The nameplate. Diagnostic |
| Status | Off, standby, charging, discharging, fault, idle, power saving |

On the inverter's own device:

| Sensor | What it shows |
|---|---|
| Grid status | On grid or off grid, for a site with backup |
| Vendor status code | The controller and error code the way SetApp prints it, `18xBF` for instance. Diagnostic |
| RRCR input state | The four ripple control inputs, as a number 0 to 15. Diagnostic |
| Register writes | How many times this entry wrote a persistent register of the inverter, see below. Diagnostic |

## Steering the battery

> **This can bite.** SolarEdge warns that periodic changes to these
> registers wear the inverter's flash memory. Every write the
> integration makes to them shows up in the *Register writes* sensor,
> across restarts. An automation that writes every poll is what that
> warning is about.

The storage control block appears when SolarEdge has enabled it, on
the inverter's device, without any option to tick:

| Entity | What it does |
|---|---|
| Storage control mode | Disabled, maximize self-consumption, time of use, backup only, or **remote control**. Only in remote control do the entities below mean anything |
| Storage AC charge policy, Storage AC charge limit | Whether the battery may charge from AC at all, and how much: in kWh for a fixed energy limit, in percent for percent of production |
| Storage backup reserve | State of energy kept back for an outage, in percent, on hardware with backup |
| Storage default mode | What the battery does when no command is in force |
| Storage command mode | What it does now, for *Storage command timeout* seconds |
| Storage charge limit, Storage discharge limit | The most watts the command may charge or discharge with |
| Re-arm storage command | Writes the timeout and the command again every 15 minutes while on, see below |

The order that works, from the users of the community integration:
control mode to *Remote control* first, then the timeout, then the
command mode, then the limits. Written all at once the inverter kept
the old limits. Give it a few seconds between writes; some inverters
answer with nonsense for a moment after one.

**Recipes.** Charge from the grid: AC charge policy *Always allowed*,
command mode *Charge from solar and grid*, charge limit the watts you
want. Discharge to the grid: *Discharge to maximize export* with the
discharge limit. Hold the battery: *Solar power only*. Let it be:
control mode *Maximize self-consumption*.

**The command lapses.** When the timeout runs out the inverter falls
back to the default mode, and it does the same after its nightly
restart. *Re-arm storage command* writes the timeout and the command
again every 15 minutes while it is on, so a command survives the
night; keep the timeout above 15 minutes for that. It writes what
Home Assistant last set, or what the inverter shows if Home Assistant
never did, and only while the control mode is *Remote control*. Two
writes every 15 minutes is what the community's users run with; more
often "chokes the modbus", and every one of them counts in
*Register writes*.

A time profile set in the monitoring portal, or a grid program you
enrolled in, overrides all of this from the outside and can revert a
command within seconds. Read back before you trust a write.

## Limiting export

These are the export controls, behind **Enable experimental export
controls (BETA)** in the options like the SunSpec ones. They appear
when SolarEdge has enabled the blocks.

| Entity | What it does |
|---|---|
| Site limit mode | Which meter the limit is measured against: export control with an export/import meter or with a consumption meter, or production control without a meter |
| Site limit scope | Whether the limit applies to the site total or to each phase |
| Site limit | The limit in watts. Written to flash; count it |
| Active power limit | Percent of nominal power, applied at once, kept in RAM: not counted, not persistent, and on some firmware gone again within seconds |
| Keep active power limit asserted | Writes the active power limit again every 10 seconds while on, for firmware that lets it revert |

The site limit is a control loop inside the inverter that seeks the
target through the meter; setting it to 0 W on a site with other
generation drove production to zero for one user, and on firmware
4.24.14 stopped the whole system. Users of the community integration
keep 1 % as the floor for the active power limit for the same reason.

## Things to know

- **The battery and its controls are switched on per inverter by
  SolarEdge.** Support enables "Modbus access to the power control and
  battery registers" for a site, and there is no pattern to which
  inverters have it. Without it the battery, the site limit and the
  storage control are simply missing here. The diagnostics download
  lists the blocks that answered under `raw_blocks`. Users of the
  community integration got it enabled by asking support for exactly
  that wording; several were refused.
- **Firmware from 4.23 answers nothing** for a block it does not
  serve, instead of refusing it. Each such block costs one read
  timeout, then the integration leaves it alone for an hour and tries
  again. A block SolarEdge switches on later therefore shows up within
  the hour, without a restart.
- **Firmware updates arrive unannounced** and have switched Modbus
  off, several times in 2026. When the inverter stops answering after
  an update, switch Modbus TCP off and on in SetApp. If that does not
  help, power the inverter down and up.
- **Energy counters reset** on LG and BYD batteries and go backwards
  now and then on the 48 V SolarEdge battery. The Energy Dashboard
  treats a drop as a meter reset.
- **Two batteries stacked** on one inverter appear as one, with the
  capacity of both.
- **Meters** are the inverter's own, read through it. They appear as
  SunSpec meter models in the same entry; the integration does not
  add anything to them.

## Reporting back

Nobody on the project has a SolarEdge. If you run one,
[open an issue](https://github.com/hilman2/ha-sunspec2/issues) with the
inverter model, its firmware version, whether the battery appeared,
and the `raw_blocks` part of the diagnostics download.
