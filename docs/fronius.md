# Fronius GEN24, Verto and Tauro

This page is for Fronius hybrid inverters (GEN24, Verto, Tauro) with a
battery. It shows what Home Assistant gets from them and how to
control the battery: a **Battery mode** menu, four power fields in
watts, a daily charge or discharge schedule, and sensors for the
battery and the solar strings. The integration recognises the inverter
by what it reports about itself, so there is nothing to configure.

These come on top of the generic controls in
[write-controls.md](write-controls.md). For the generation before
(Symo, Primo, Eco, Galvo, or a Symo Hybrid) see
[fronius-symo.md](fronius-symo.md).

## Before it works

Home Assistant talks to the inverter over your home network using a
standard called Modbus. The inverter has it switched off, or
read-only, until you change a few settings. Open the inverter's web
page in your browser and go to *Communication -> Modbus*:

1. Switch on **Modbus TCP**. This lets programs on your network read
   the inverter. Leave the port at 502, the default.
2. Switch on **Allow control** ("inverter control via Modbus"). Without
   it the inverter refuses every command from Home Assistant, so you
   can read values but not change anything.
3. Set **SunSpec model type** to *int + SF* or *float*. Both work.
   (SunSpec is the data format the inverter uses to describe itself.)
4. Only if you want **Charge from grid**: also enable *battery charging
   from DNO grid* under *Device configuration -> Components -> Battery*.
   The switch in Home Assistant works together with this setting: both
   must be on, and Home Assistant alone cannot turn grid charging on.
   If you enter the inverter's web interface password in the
   integration (see "The inverter's web interface" below), the
   integration sets it for you.
5. Turn off any **scheduled charging** you set up in the inverter's
   web interface. Two controllers on one battery produce surprises.

The battery entities appear on their own when the inverter reports a
battery. There is nothing to tick in the integration options for them.

The inverter has features that take priority over commands from Home
Assistant: **IO control** and **dynamic power reduction**, if you set
them to have priority in the inverter's settings. If a mode change
does nothing, look there first.

## What you get

On the battery device:

| Entity | What it does |
|---|---|
| **Battery mode** | The menu described below. It shows what the battery is actually doing, as the inverter reports it, not what you last asked for |
| **Battery PV charge limit** | The most power, in watts, the battery may take from your solar panels. Used by the modes with a charge limit |
| **Battery discharge limit** | The most power, in watts, the battery may give to the house. Used by the modes with a discharge limit |
| **Battery grid charge power** | Power in watts for forced charging in *Charge from grid*. Whole tens of watts only (see "Things to know") |
| **Battery grid discharge power** | Power in watts for forced discharging in *Discharge to grid*. Whole tens of watts only |
| **Battery charging from grid allowed** | Lets the battery charge from the grid. Works together with the matching setting in the inverter's web interface (see "Before it works", step 4) |
| **Battery minimum reserve** | How much charge, in percent, the inverter keeps back. Disabled by default |
| **Battery max charge power** | The highest charge power the inverter reports for this battery. The four power fields cannot go above it |

The generic **Battery charge rate**, **Battery discharge rate** and
**Battery control mode** entities from the generic controls still
exist, but they are disabled by default on a Fronius. They change the
same settings in the inverter as the fields above, only in percent.
Two entities on one setting disagree the moment you use either of
them.

The four power fields keep their value when Home Assistant restarts,
and keep it while another mode is active. If you change a field while
its mode is active, the inverter gets the new value at once.
Otherwise the new value is used the next time you change the mode.

Sensors, which show what the battery and the solar strings are doing:

| Sensor | What it shows |
|---|---|
| **Battery charge power** | Watts going into the battery |
| **Battery discharge power** | Watts coming out of the battery |
| **Battery charged energy** | Total energy that has gone into the battery over its life. Use it for the Energy Dashboard's "energy going in to the battery" |
| **Battery discharged energy** | Total energy that has come out of the battery over its life. Use it for the dashboard's "energy coming out of the battery" |
| **PV power** | Power of all solar strings added together, in watts |

The numbered **Module 1**, **Module 2** and so on sensors stay. They
show the same values for each string, numbered instead of named. The
energy of each single string is only on those.

<a id="the-modes"></a>

## Choosing what the battery does

Select one of these in **Battery mode**. The limits are the power
fields from the table above.

| Mode | What the battery does |
|---|---|
| Automatic | Whatever the inverter decides. No limits |
| PV charge limit | Charges from the solar panels, with at most the PV charge limit |
| Discharge limit | Discharges to the house, with at most the discharge limit |
| PV charge and discharge limit | Both limits at once |
| Charge from grid | Charges with the grid charge power, taking it from the grid if the solar panels do not cover it. Solar.web (Fronius's online portal) shows "Forced Recharge" |
| Discharge to grid | Discharges with the grid discharge power, into the grid if the house does not take it |
| Block discharging | Charges from the solar panels, never discharges |
| Block charging | Discharges to the house, never charges |

To get back to normal operation select **Automatic**. That is also the
answer when the battery seems stuck in a forced charge: the inverter
keeps the last mode it was given until it is told otherwise, or until
you reset its control in the web interface.

<a id="giving-energy-back-over-night"></a>

## Charging or discharging on a schedule

The battery plan charges the battery from the grid, or discharges it
into the grid, between two times of day, until the battery reaches a
chosen level. Its entities are on the battery device, next to the
modes.

| Entity | What it does |
|---|---|
| **Battery plan** | The on/off switch. The plan runs every day while it is on. Switching it off ends the active plan |
| **Battery plan direction** | Charge or discharge. Default discharge |
| **Battery plan start** | When the window opens. Default 20:00 |
| **Battery plan end** | When it closes. Default 06:00. Equal start and end means a full day |
| **Battery plan target** | The battery level in percent to charge up to or discharge down to. Default 10 |
| **Battery capacity** | How much energy the battery can hold, in kWh. It is filled in for you where the inverter knows it. The plan needs it to turn a battery level into watts |

At the start of the window, the plan works out how many watts it needs
to reach the target by the end, and asks the inverter for that power
for the whole window. Examples:

- Discharging a 60 kWh battery from 80 % to 10 % between 20:00 and
  06:00 means 42 kWh over ten hours, so 4200 W.
- Charging a 10 kWh battery from 50 % to 80 % over three hours means
  1000 W.

At the end of the window the plan selects *Automatic*. If you switch
the plan on inside the window, or Home Assistant restarts inside it,
the plan covers what is left of the window. The attributes of the
**Battery plan** switch show the power the last plan asked for.

The power never goes above the battery's own maximum, and the battery
level never goes below the minimum reserve the inverter itself keeps.

Each time the integration reads the inverter successfully, the plan
checks whether the target is reached. If it is, the plan selects
*Automatic*. Solar generation, what the house uses and the inverter's
own limits all change how fast the battery gets there. *Automatic*
ends the forced charging or discharging, but the inverter may still
use the battery on its own.

If you change the direction, the times, the target or the capacity,
all changes take effect together on the next successful read. The plan
then works out the power again for the time that is left. If you move
the window so that it no longer contains the current time, the active
plan stops. If you change nothing, the power stays the same all
window.

If you used the earlier scheduled-discharge entities, they keep their
IDs and settings.

### What happens if Home Assistant stops

The inverter forgets a forced charge or discharge after a set time
unless it is told again. That safety timer is the entity **Battery
rate revert time**. A GEN24 leaves the factory with 300 seconds in it,
and stops discharging after that. While the plan runs, it owns this
timer:

- It sets the timer to the length of the window. If Home Assistant
  dies at 20:05, the battery keeps going until 06:00 and then stops,
  rather than stopping at once or never.
- It sends the mode again before the timer runs out.
- At the end of the window it puts your old value back.

What the timer is, and the other ways to live with it, is in
[write-controls.md](write-controls.md#the-revert-timer-and-how-not-to-get-caught-by-it).

### Setting the plan from an automation

Add the **SunSpec: Set battery plan** action to an automation and
select the SunSpec device. You can set any combination of direction,
start, end, target, capacity and enabled. Fields can use templates.
Fields you leave out keep their current values. Setting
`enabled: false` stops the plan immediately.

```yaml
action: sunspec2.set_battery_plan
data:
  config_entry_id: YOUR_SUNSPEC_CONFIG_ENTRY_ID
  direction: charge
  start: "10:00"
  end: "16:00"
  target_pct: 80
  enabled: true
```

When a person calls the action, it needs administrator access.
Scheduled automations can call it without that.

## Things to know

- **The inverter may cap grid charging.** Users report that firmware
  1.39.5 holds grid charging at around 500 W, whatever power you set.
  Firmware 1.40 and later behave.
- **Grid powers are in steps of 10 W.** The fields round for you.
- **Minimum reserve does not stop discharging by itself.** It sets how
  much charge the inverter keeps back. Solar.web shows "Energy-saving
  mode" once it is set. The inverter ignores a reserve below 5 %.
- **A new export limit needs the switch turned off and on.** A GEN24
  takes a new **Export limit** or **Power
  factor** only when its enable switch goes from off to on. If the
  switch is already on, Home Assistant shows your new value, but the
  inverter keeps running on the old one. The only way to see it is the
  exported watts. There are two ways out:
  - Turn **Export limit enabled** off and on after each change.
  - Tick the integration option *Re-apply the export limit and power
    factor by switching them off and on when their value changes*. It
    does the off and on for you: switch off, new value, one second
    later switch on. It is off by default because for that one second
    the inverter runs without any limit.

  You can also call the action `sunspec2.set_export_limit` with
  `enable: true`. If you do, tell us whether your firmware applies the
  value from that call alone.
- **A smart meter is added as its own entry.** A meter connected to
  the inverter answers under its own number, the unit ID: 200 for the
  first, 201 and 202 for the next ones. Add it as a further integration
  entry with the same IP address and that unit ID. The integration
  takes turns on the connection by itself.
- **Some things the inverter does not give out over Modbus**:
  temperatures, where the smart meter sits, and the grid charging
  flags. They come from the inverter's web page, see the next section.

## The inverter's web interface

A GEN24 tells its own web page some things that it does not offer over
Modbus. With the password of that page, the integration reads them
too. In the integration options, enter the **Web interface password**.
This is the local `customer` login you use in your browser on the
inverter's address in your home network. It is not your Solar.web
account.

Home Assistant does not store the password. It keeps a scrambled copy
(a hash) that logs in to this inverter and nothing else. *Forget the
stored web interface login* removes it.

| Entity | What it does |
|---|---|
| **Inverter temperature** | Temperature inside the inverter |
| **Battery cell temperature** | Temperature of the battery cells. The battery's make, model and serial number are in its attributes |
| **Smart meter location** | Whether the main smart meter sits at the grid connection point or in the path to your consumption. Every meter and its unit ID are in the attributes |
| **Battery charging from grid (web interface)** | The setting from "Before it works", step 4. *Charge from grid* switches it on by itself while a login is stored |
| **Battery charging from AC (web interface)** | The companion setting. Grid charging needs both |
| **Modbus control allowed** | Shows whether "inverter control via Modbus" is on in the inverter's Modbus settings |
| **Reset Modbus control** | The web page's Modbus reset: clears every limit and mode set over Modbus |

These entities are read once a minute from the inverter's web page. If
the page does not answer, they become unavailable, and the other
entities keep working. A password that stops working looks the same,
with the reason in the Home Assistant log.

## Reporting back

Nobody on the project has a Fronius. If you use these modes,
[open an issue](https://github.com/hilman2/ha-sunspec2/issues) with
the inverter model, its firmware version and what each mode did.
Please attach the diagnostics download. It says `"vendor": "fronius"`
when this page applies to your inverter.

---

## Technical details

Nothing here is needed to use the integration. It is for people who
want to check a behaviour against the manual or the registers.

### Where the data comes from

The battery controls are the SunSpec storage model (124) as Fronius
reads it, documented in the
[Fronius GEN24 Modbus manual](https://manuals.fronius.com/html/4204102649/en-US.html).
The community's
[fronius_modbus](https://github.com/callifo/fronius_modbus)
integration drove the same registers for a year, and its users'
findings are folded into this page.

### Modbus settings

- Port 502 is the Modbus TCP default.
- **SunSpec model type** *int + SF* means integer values with scale
  factors, *float* means floating point values. The integration reads
  both.
- **Allow control** is "inverter control via Modbus". Without it every
  write is refused.
- IO control and dynamic power reduction win over Modbus commands where
  the inverter's settings give them priority.

### Battery entities and registers

- The battery entities appear when `WChaMax` (model 124) is above zero.
- **Battery charging from grid allowed** is the `ChaGriSet` register,
  AND-linked with the web setting *battery charging from DNO grid*.
- **Battery max charge power** is `WChaMax`, the value the four power
  fields are bounded by.
- The generic **Battery charge rate**, **Battery discharge rate** and
  **Battery control mode** write the same model 124 registers (`InWRte`,
  `OutWRte`, `StorCtl_Mod`) in percent. They ship disabled on a
  Fronius because two entities on one register disagree as soon as
  either is used.
- The four power fields are kept by `RestoreNumber` in
  `coordinator.storage_setpoints`. The integration writes the rates
  first, in one frame, and the mode last.
- The inverter keeps the last written mode until it is told otherwise
  or its control is reset in the web interface.

### Battery and strings as DC channels

The inverter reports its battery as two DC channels in the multiple
MPPT model (160), next to the PV strings, and labels them `ST CHA` and
`ST DISCHA`. The integration reads the labels and adds the sensors
named by what they carry. The generic *Module n* sensors read the same
registers; the per-string energies are on those.

### The battery plan

- **Battery rate revert time** is `InOutWRte_RvrtTms` in model 124, the
  inverter's dead-man switch for the battery rates.
- The plan sets it to the length of the window, writes the mode again
  before it lapses, and restores the old value at the end.
- The plan checks the target on every successful poll.
- A change to direction, times, target or capacity is applied on the
  next successful poll. Without changed settings the power stays
  constant until the revert timer needs refreshing.
- The plan power never exceeds the battery's own maximum (`WChaMax`),
  and the state of charge never goes below the inverter's own minimum
  reserve (`MinRsvPct`).

### Things to know, with the sources

- Firmware 1.39.5 holding grid charging around 500 W regardless of the
  power set is a finding of users of the community integration.
  Firmware 1.40 and later behave.
- **Multiples of 10 W** for the grid powers: other values made a GEN24
  "charge at 500 W" in the community's experience.
- **Minimum reserve** is `MinRsvPct`. The inverter ignores values below
  5 %.
- **Export limit and power factor** are in models 123 and 704. The
  GEN24 only takes a new value when `WMaxLim_Ena` (model 123) or
  `WMaxLimPctEna` (model 704) goes from off to on. With the enable
  switch on, the new value sits in the register while the inverter runs
  on the old one. The integration option *rearm_on_change* does the
  switch off, value, switch on cycle with one second between the
  writes.
- **Meters** sit under unit IDs 200, 201, 202. Several entries on one
  IP address share the connection through a per-host lock.

### Web interface

The web entities are polled once a minute over HTTP on the inverter's
address. They are the only source for the inverter and battery cell
temperature, the smart meter location, the grid charging flags
(`ChaGriSet` is AND-linked with *battery charging from grid*, and
grid charging needs *battery charging from AC* too), the state of
"inverter control via Modbus" and the Modbus reset.

### Diagnostics

The diagnostics download says `"vendor": "fronius"` when the profile
applied. Its `scanned_models` entry for model 124 shows the registers
as they were after your last change.
