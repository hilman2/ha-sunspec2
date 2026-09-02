# Fronius GEN24, Verto and Tauro

A Fronius hybrid inverter with a battery gets a **Battery mode** menu
and four power fields in watts on top of the generic controls described
in [write-controls.md](write-controls.md), and sensors that say what
the battery and the PV strings are doing. This page is about those.
The integration recognises the inverter by the manufacturer name it
reports, nothing to configure.

Everything here is the SunSpec storage block (124) as Fronius reads it,
documented in the
[Fronius GEN24 Modbus manual](https://manuals.fronius.com/html/4204102649/en-US.html).
The same registers were driven by the community's
[fronius_modbus](https://github.com/callifo/fronius_modbus) integration
for a year, and its users' findings are folded in below.

## Before it works

On the inverter's web interface, *Communication -> Modbus*:

1. Switch on **Modbus TCP**. Port 502 is the default.
2. Switch on **Allow control** ("inverter control via Modbus"). Without
   it every write is refused.
3. **SunSpec model type**: *int + SF* or *float*, both work.
4. For **Charge from grid** also enable *battery charging from DNO grid*
   under *Device configuration -> Components -> Battery*. The Modbus
   switch below is AND-linked with that setting and cannot turn grid
   charging on by itself. With the web interface password entered
   (see below), the integration sets it for you.
5. Turn off any **scheduled charging** you set up in the web interface.
   Two controllers on one battery produce surprises.

The battery entities appear when the inverter reports a battery, that
is when `WChaMax` is above zero. Nothing to tick in the integration
options: only the export limit is behind the beta switch there.

Modbus commands lose against **IO control** and **dynamic power
reduction** where those are given priority in the inverter's settings.
If a mode change has no effect, look there first.

## The entities

| Entity | What it does |
|---|---|
| Battery mode | The menu below. Read back from the inverter, so it shows what the battery is doing, not what was last asked |
| Battery PV charge limit | Most watts the battery takes from the roof, used by the limit modes |
| Battery discharge limit | Most watts the battery gives to the house, used by the limit modes |
| Battery grid charge power | Watts of forced charging in *Charge from grid*. Multiples of 10 W |
| Battery grid discharge power | Watts of forced discharging in *Discharge to grid*. Multiples of 10 W |
| Battery charging from grid allowed | The `ChaGriSet` register, AND-linked with the web setting above |
| Battery minimum reserve | State of charge the inverter keeps back, in percent. Disabled by default |
| Battery max charge power | `WChaMax`, the value the four fields are bounded by |

The generic *Battery charge rate*, *Battery discharge rate* and
*Battery control mode* entities from the generic controls still exist
but ship disabled on a Fronius. They write the same registers in
percent, and two entities on one register disagree the moment either
is used.

The power fields keep their value across restarts and hold it while
another mode is active. Changing a field while its mode is active
writes the register at once; otherwise the next mode change picks the
value up.

## The modes

| Mode | What the battery does |
|---|---|
| Automatic | Whatever the inverter decides. Both caps off |
| PV charge limit | Charges from the roof with at most the PV charge limit |
| Discharge limit | Discharges to the house with at most the discharge limit |
| PV charge and discharge limit | Both limits at once |
| Charge from grid | Charges with the grid charge power, from the grid if the roof does not cover it. Solar.web shows "Forced Recharge" |
| Discharge to grid | Discharges with the grid discharge power, into the grid if the house does not take it |
| Block discharging | Charges from the roof, never discharges |
| Block charging | Discharges to the house, never charges |

To get back to normal operation select **Automatic**. That is also the
answer when the battery seems stuck in a forced charge: the inverter
keeps the last written mode until it is told otherwise or its control
is reset in the web interface.

## What the battery and the strings are doing

The inverter reports its battery as two DC channels in the multiple
MPPT model (160), next to the PV strings, and labels them `ST CHA` and
`ST DISCHA`. The integration reads the labels and adds sensors named by
what they carry.

| Sensor | What it shows |
|---|---|
| Battery charge power | Watts going into the battery |
| Battery discharge power | Watts coming out of the battery |
| Battery charged energy | Lifetime energy into the battery. The Energy Dashboard's "energy going in to the battery" |
| Battery discharged energy | Lifetime energy out of the battery. The dashboard's "energy coming out of the battery" |
| PV power | The PV strings' DC power, summed |

The generic *Module n* sensors stay: same registers, numbered instead
of named. The per-string energies are on those.

## Giving energy back over night

For a battery that is paid to deliver at night: a plan that discharges
what the battery holds above a reserve to the grid, at a steady power,
between two times of day. The entities sit on the battery device, next
to the modes.

| Entity | What it does |
|---|---|
| Scheduled discharge | The plan. On, it runs every day. Switched off inside the window, it hands the battery back |
| Scheduled discharge start | When the window opens. Default 20:00 |
| Scheduled discharge end | When it closes. Default 06:00 |
| Scheduled discharge reserve | State of charge the plan leaves in the battery, in percent. Default 10 |
| Battery capacity | The battery's usable energy in kWh, pre-filled from the nameplate model where the inverter has one. The plan cannot turn a state of charge into watts without it |

At the start of the window the plan reads the state of charge, takes
the energy above the reserve, spreads it over the window and selects
*Discharge to grid* with that power. A 60 kWh battery at 80 % with a
10 % reserve and a window from 20:00 to 06:00 gives 42 kWh over ten
hours, 4200 W. At the end of the window the plan selects *Automatic*.
Switched on inside the window, or started inside it after a restart,
the plan covers what is left of the window. The switch's attributes
show the power the last plan asked for.

The power never exceeds the battery's own maximum, and the state of
charge never goes below the inverter's own minimum reserve.

## Things to know

- **Grid charging is capped by the inverter.** Users of the community
  integration report firmware 1.39.5 holding grid charging around
  500 W regardless of the power set. Firmware 1.40 and later behave.
- **Multiples of 10 W** for the grid powers. The fields round for you.
  Other values made a GEN24 "charge at 500 W" in the community's
  experience.
- **Minimum reserve** does not stop discharging by itself. It sets the
  state of charge the inverter reserves, and Solar.web shows
  "Energy-saving mode" once it is set. A reserve below 5 % is ignored
  by the inverter.
- **A new export limit needs the switch off and on.** The GEN24 takes
  a new *Export limit* or *Power factor* only when its enable switch
  goes from off to on. With the switch on, the new value sits in the
  register and the inverter runs on the old one; Home Assistant shows
  the new value and the exported watts are the only witness. The
  integration option *Re-apply the export limit and power factor by
  switching them off and on when their value changes* does the cycle
  for you: switch off, the value, a second later switch on. It is off
  by default because for that second the inverter runs without the
  limit. Without it, toggle *Export limit enabled* after a change, or
  call `sunspec2.set_export_limit` with `enable: true` and tell us
  whether your firmware applies the value on that write alone.
- **Meters** hang off the inverter under unit ID 200 (201, 202 for the
  next ones). Add them as further entries with the same IP and that
  unit ID; the integration takes turns on the connection by itself.
- **What Modbus cannot do** on a GEN24, temperatures, the meter's
  position, the grid charging flags, goes through the inverter's web
  page. See the next section.

## The web interface

A GEN24 tells its own web page things it does not put into a
register. With the password of that page, the integration reads them
too. In the integration options, enter the **Web interface password**:
the local `customer` login you use in the browser on the inverter's
LAN address, not the Solar.web account. The password is not stored;
what Home Assistant keeps is a hash of it that logs in to this inverter
and nothing else. *Forget the stored web interface login* drops it.

| Entity | What it does |
|---|---|
| Inverter temperature | Ambient temperature inside the inverter |
| Battery cell temperature | With the battery's manufacturer, model and serial as attributes |
| Smart meter location | Whether the primary meter sits at the feed-in point or in the consumption path. Every meter with its Modbus unit ID is in the attributes |
| Battery charging from grid (web interface) | The flag `ChaGriSet` is AND-linked with. *Charge from grid* switches it on by itself while a login is stored |
| Battery charging from AC (web interface) | Its companion flag; grid charging needs both |
| Modbus control allowed | "Inverter control via Modbus" from the Modbus settings page |
| Reset Modbus control | The web page's Modbus reset: clears every limit and mode set over Modbus |

The web entities are polled once a minute over HTTP on the inverter's
address. A web page that does not answer makes them unavailable and
leaves the Modbus entities alone. A password that stops working shows
up as the same, with the reason in the log.

## Reporting back

Nobody on the project has a Fronius. If you run these modes,
[open an issue](https://github.com/hilman2/ha-sunspec2/issues) with the
inverter model, its firmware version and what each mode did. The
diagnostics download says `"vendor": "fronius"` when the profile
applied, and the `scanned_models` entry for 124 shows the registers
after your last change.
