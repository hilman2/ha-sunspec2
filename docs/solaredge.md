# SolarEdge

A SolarEdge inverter speaks SunSpec for everything it measures: the
inverter, its meters, the strings of a Synergy unit. What it does not
put into SunSpec is the battery and everything you can set, and this
page is about what the integration reads from SolarEdge's own
registers on top. The integration recognises the inverter by the
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
| Site limit mode, Site limit | The export or production limit set on the inverter. Diagnostic |
| Storage control mode | Whether the battery runs on self-consumption, a time profile, backup only or remote control. Diagnostic |

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
