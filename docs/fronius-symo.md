# Fronius Symo, Primo, Eco, Galvo and Symo Hybrid

This page is for the Fronius inverters from before the GEN24: a Symo,
Primo, Eco or Galvo with a Datamanager 2.0 card (the plug-in card that
connects the inverter to your network), or a Symo Hybrid with its
Hybridmanager. For a GEN24, Verto or Tauro see [fronius.md](fronius.md).
The integration tells the two generations apart by what the inverter
reports about itself, so there is nothing to configure.

Nobody on the project has one of these inverters; see "Reporting
back" at the end.

## Before it works

Home Assistant talks to the inverter over your home network using a
standard called Modbus. The Datamanager card has it set up for a
different use until you change it. Open the card's web page in your
browser and go to *Settings -> Modbus*:

1. Set **Data output via Modbus** to *tcp*. The card ships set to
   *rtu*, which is for a cable, not your network. Leave the port at
   502.
2. Set **SunSpec Model Type** to *int + SF* or *float*. Both work.
   (SunSpec is the data format the inverter uses to describe itself.)
3. Switch on **Inverter control via Modbus** if Home Assistant is to
   change anything, such as the battery mode or the export limit.
   *Restrict the control* is a list of addresses that may send
   commands: put Home Assistant's address in it, or leave it off.

When you add the inverter in Home Assistant you are asked for a **unit
ID**. That is the number that says which device on the connection
should answer. For the inverter it is the inverter number set on the
inverter's own display: 1 on a single inverter. Number 00 answers as
100.

Then, on the inverter itself, go to *Setup -> Display Settings ->
Night Mode* and set it to **ON** or **AUTO**. The factory setting is
*OFF*. With *OFF* the card shuts down together with the inverter at
dusk. Every connection then fails until sunrise, and the Home
Assistant log fills with connection errors that mean nothing. A
configured Fronius Smart Meter keeps the card running at night as
well.

## What you get

Sensors for the solar strings and, on a Symo Hybrid, the battery:

| Sensor | What it shows |
|---|---|
| **PV power** | Power of all solar strings added together, in watts. On a Symo Hybrid this is the first string alone, because the second one is the battery |
| **Battery charge power** | Watts going into the battery (Symo Hybrid only) |
| **Battery discharge power** | Watts coming out of the battery (Symo Hybrid only) |

The Symo Hybrid does not say in which direction its battery power
flows. The integration works that out itself, so the two battery
sensors mean the same as on a GEN24. There are no battery energy
sensors on this generation, because the inverter does not report the
energy numbers.

The numbered **Module 1**, **Module 2** and so on sensors stay. They
show the same values for each string, numbered instead of named.

A Symo Hybrid also gets a **Battery mode** menu and four power fields
in watts, described next. No inverter temperature is available on this
generation: the inverter has no value to give.

### Adding a Fronius Smart Meter

A Fronius Smart Meter answers on the same address as the inverter, but
under its own **unit ID 240**. If you have more meters, they use 241
to 244. Add each meter as a further integration entry with the same IP
address and that unit ID. The integration takes turns on the
connection by itself.

## Controlling the battery (Symo Hybrid)

A Symo Hybrid gets the same **Battery mode** menu and the four power
fields as a GEN24. [fronius.md](fronius.md#the-modes) describes them.
What is different here:

- **Charge from grid** also needs *Settings -> DNO Editor -> Battery
  charge* switched on, on the Datamanager's web page. The switch in
  Home Assistant works together with that setting: both must be on.
  There is no web login on this generation that could set it for you,
  so you do it by hand.
- The power fields take **whole watts**. There is no rounding to 10 W.
- **A mode stays until you change it.** A forced charge runs until you
  select *Automatic*, or until you switch Modbus off on the card, which
  resets every command. There is no timer that takes the inverter back
  to normal by itself on this generation.
- A battery in **energy saving mode** can take up to ten minutes to
  react to a command.
- **The maximum power may show your battery's capacity.** One Symo
  Hybrid with an 11.5 kWh battery reports its maximum as 11520, which
  is watt-hours where watts belong. The four power fields cannot go
  above that number. The inverter computes its percentages against the
  same number, so a value you enter still means what it says. Check
  Solar.web (Fronius's online portal) the first time.

## Limiting what you feed into the grid

The export limit of [write-controls.md](write-controls.md) works here
too. Three things from the manual:

- **A new value only counts after the inverter restarts its operating
  mode.** To do that, switch **Export limit enabled** off and on again.
  The integration option *Re-apply the export limit and power factor by
  switching them off and on when their value changes* does it for you.
  On this generation it is **on by default**, because this is the
  documented procedure and not a quirk. Switch the option off if you
  would rather do the off and on by hand.
- **Values below 10 %** can put the inverter into standby, depending
  on its firmware.
- **A limit with a revert time never reverts while Home Assistant is
  running.** The revert time (how long the inverter honours the limit
  before it falls back) starts again with every message the card
  receives, and Home Assistant's regular reading counts as a message.
  To end a limit, switch it off instead of waiting.

## Things to know

- **The card is slow.** It handles about one request a second, and the
  manual asks for a timeout of ten seconds. Keep the scan interval (how
  often Home Assistant reads the inverter) at 10 seconds or more.
  Expect the first read after a card restart to take longer.
- **The card restarts itself** after a day without a connection to
  Solar.web. Every connection drops. The integration reconnects on the
  next read.
- **Firmware updates can change number formats.** The manual says so.
  The integration reads the format together with every value, so you do
  not need to do anything.

## Reporting back

If you use one of these inverters,
[open an issue](https://github.com/hilman2/ha-sunspec2/issues) with
the model, the Datamanager or Hybridmanager version, and what worked.
The version is in the diagnostics download, in the entry called `Opt`.
The diagnostics download says `"vendor": "fronius_datamanager"` when
this page applies to your inverter.

The Symo Hybrid's battery sensors and modes are built from one data
dump from a single inverter. Yours would be the second.

---

## Technical details

Nothing here is needed to use the integration.

### Sources

What is on this page comes from the
[Fronius Datamanager Modbus manual](https://www.fronius.com/~/downloads/Solar%20Energy/Operating%20Instructions/42,0410,2049.pdf)
and from people who drive these inverters from evcc, openHAB and
Loxone. Nobody on the project has one.

### Modbus settings

- **Data output via Modbus** *tcp* versus *rtu*: the card ships with
  *rtu*. Port 502.
- **SunSpec Model Type** *int + SF* means integer values with scale
  factors, *float* means floating point values.
- *Restrict the control* is an allowlist of addresses.
- The unit ID is the inverter number set on the inverter's display.
  Number 00 answers as unit ID 100.
- Fronius Smart Meters answer under unit IDs 240 to 244.

### Strings and battery

- The inverter labels its DC inputs `String 1` and `String 2` in the
  multiple MPPT model (160). On a Symo Hybrid, String 2 is the
  battery. **PV power** is the sum of the strings, which on a Symo
  Hybrid is String 1 alone.
- The Symo Hybrid reports the battery's power without a sign. The
  integration reads the direction from the charge state in the storage
  block (model 124).
- There are no battery energy counters because the channel has one
  lifetime counter for both directions, and the inverter does not
  fill it.
- The generic *Module n* sensors read the same registers, numbered
  instead of named.
- The inverter temperature registers read "not implemented" on this
  generation.

### Battery modes

- **Charge from grid** needs *Settings -> DNO Editor -> Battery charge*
  on the Datamanager. The Modbus switch (`ChaGriSet`) is AND-linked
  with it.
- The storage block's revert timers are not supported on this
  generation, so a mode stays until it is changed.
- One Symo Hybrid with an 11.5 kWh battery reports `WChaMax` as 11520:
  watt-hours where watts belong. The four power fields are bounded by
  that number, and the inverter computes its percentages against the
  same number.

### Export limit

- A new value is applied by restarting the operating mode. The
  integration option is `rearm_on_change`, on by default on this
  generation.
- Values below 10 % can put the inverter into standby, depending on the
  firmware.
- A revert time restarts with every Modbus message the card receives,
  the integration's polls included.

### Card behaviour

- One request a second is what the card handles. The manual asks for a
  timeout of ten seconds.
- The card restarts itself after a day without Solar.web.
- Scale factors move with firmware, the manual says so. The
  integration reads them with every value.

### Diagnostics

The diagnostics download says `"vendor": "fronius_datamanager"` when
this page applies. The Datamanager or Hybridmanager version is `Opt`
in it. The Symo Hybrid's battery sensors and modes are built from one
register dump.
