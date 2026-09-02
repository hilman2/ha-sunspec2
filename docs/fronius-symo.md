# Fronius Symo, Primo, Eco, Galvo and Symo Hybrid

The generation before the GEN24: a SnapINverter with the Datamanager
2.0 card, or a Symo Hybrid with its Hybridmanager. For a GEN24, Verto
or Tauro see [fronius.md](fronius.md). The integration tells the two
generations apart from what the inverter reports about itself,
nothing to configure. The diagnostics download says
`"vendor": "fronius_datamanager"` when this page applies.

What is here comes from the
[Fronius Datamanager Modbus manual](https://www.fronius.com/~/downloads/Solar%20Energy/Operating%20Instructions/42,0410,2049.pdf)
and from people who drive these inverters from evcc, openHAB and
Loxone. Nobody on the project has one; see the last section.

## Before it works

On the Datamanager's web page, *Settings -> Modbus*:

1. **Data output via Modbus**: *tcp*. The card ships set to *rtu*.
   Port 502.
2. **SunSpec Model Type**: *int + SF* or *float*, both work.
3. **Inverter control via Modbus** for anything Home Assistant is to
   write. *Restrict the control* is an allowlist of addresses; put
   Home Assistant's in or leave it off.

The **unit ID** is the inverter number set on the inverter's own
display: 1 on a single inverter. Number 00 answers as 100.

On the inverter itself, *Setup -> Display Settings -> Night Mode*:
**ON** or **AUTO**. With the factory setting *OFF* the card goes down
with the inverter at dusk, every connection fails until sunrise, and
the log fills with connect errors that mean nothing. A configured
Fronius Smart Meter keeps the card up as well.

## Meters

A Fronius Smart Meter answers on the same address under **unit ID 240**,
the next ones under 241 to 244. Add it as a further entry with that
unit ID; the integration takes turns on the connection by itself.

## What the strings and the battery are doing

The inverter labels its DC inputs `String 1` and `String 2` in the
multiple MPPT model (160). On a Symo Hybrid, String 2 is the battery.

| Sensor | What it shows |
|---|---|
| PV power | The PV strings' DC power, summed. On a Symo Hybrid that is String 1 alone |
| Battery charge power | Watts going into the battery (Symo Hybrid) |
| Battery discharge power | Watts coming out of the battery (Symo Hybrid) |

The Symo Hybrid reports the battery's power without a sign. The
integration reads the direction from the storage block's charge
state, so the two battery sensors mean the same as on a GEN24. There
are no battery energy counters here: the channel has one lifetime
counter for both directions, and the inverter does not fill it.

The generic *Module n* sensors stay: same registers, numbered instead
of named.

## The battery modes (Symo Hybrid)

A Symo Hybrid gets the same **Battery mode** menu and the four power
fields as a GEN24; [fronius.md](fronius.md#the-modes) describes
them. What is different here:

- **Charge from grid** needs *Settings -> DNO Editor -> Battery charge*
  on the Datamanager as well. The Modbus switch is AND-linked with it,
  and there is no web login on this generation to set it for you.
- The powers are **whole watts**, no rounding to 10 W.
- **The maximum may be your battery's capacity.** One Symo Hybrid with
  an 11.5 kWh battery reports `WChaMax` as 11520, watt-hours where
  watts belong. The power fields are bounded by that number. The
  inverter computes its percentages against the same number, so a
  value you enter still means what it says; check Solar.web the first
  time.
- **A mode stays until you change it.** The storage block's revert
  timers are not supported on this generation. A forced charge runs
  until *Automatic* is selected or Modbus is switched off on the card,
  which resets every command.
- A battery in **energy saving mode** can take up to ten minutes to
  react to a command.

## The export limit

The generic export limit of [write-controls.md](write-controls.md)
works here, with three things the manual says:

- A new value is applied by **restarting the operating mode**: switch
  *Export limit enabled* off and on again. The integration option
  *Re-apply the export limit and power factor by switching them off
  and on when their value changes* is **on by default** on this
  generation, because that is the documented procedure and not a
  quirk. Switch it off if you would rather toggle by hand.
- Values **below 10 %** can put the inverter into standby, depending
  on its firmware.
- A **revert time** restarts with every Modbus message the card
  receives, the integration's polls included. A limit with a revert
  time never reverts while Home Assistant polls; switch the limit off
  instead.

## Things to know

- **The card is slow.** One request a second is what it does, and the
  manual asks for a timeout of ten seconds. Keep the scan interval at
  10 s or more, and expect the first poll after a card restart to
  take longer.
- **The card restarts itself** after a day without Solar.web. Every
  connection drops; the integration reconnects on the next poll.
- **Scale factors move with firmware**, the manual says so. The
  integration reads them with every value.
- **No inverter temperature** over Modbus on this generation; the
  registers read "not implemented".

## Reporting back

If you run one of these inverters,
[open an issue](https://github.com/hilman2/ha-sunspec2/issues) with
the model, the Datamanager or Hybridmanager version (`Opt` in the
diagnostics download) and what worked. The Symo Hybrid's battery
sensors and modes are built from one register dump; yours would be
the second.
