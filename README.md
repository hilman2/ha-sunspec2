<picture>
  <source media="(prefers-color-scheme: dark)"
          srcset="custom_components/sunspec2/brand/dark_logo@2x.png">
  <img alt="SunSpec 2" src="custom_components/sunspec2/brand/logo@2x.png" width="360">
</picture>

# SunSpec 2

[![CI](https://github.com/hilman2/ha-sunspec2/actions/workflows/ci.yml/badge.svg)](https://github.com/hilman2/ha-sunspec2/actions/workflows/ci.yml)
[![hacs](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/default)
[![GitHub release](https://img.shields.io/github/v/release/hilman2/ha-sunspec2)](https://github.com/hilman2/ha-sunspec2/releases)
[![Quality Scale](https://img.shields.io/badge/Quality%20Scale-Gold-FFD700.svg)](https://developers.home-assistant.io/docs/core/integration-quality-scale/)

> [!NOTE]
> **2026.10.0 polls through [modbus-connection](https://github.com/home-assistant-libs/modbus-connection)**,
> the Modbus transport Home Assistant's own integrations moved to in
> 2026. The SunSpec model layer awaits it; nothing in the read or write
> path leaves the event loop any more. Same entities, same options,
> no migration. What changed underneath is in the
> [release notes](https://github.com/hilman2/ha-sunspec2/releases/tag/v2026.10.0).

Home Assistant integration for solar inverters, meters and batteries
that speak **SunSpec Modbus**. Set up entirely in the UI: enter the IP
or let it scan your network, tick the data you want, done. No YAML, no
template sensors.

What you get:

- Everything the device publishes as a normal HA sensor: power,
  energy, voltages, currents, temperatures, operating state and error
  events
- The lifetime energy counter as an Energy dashboard source, with no
  template sensor in between
- One integration for every brand, so a SolarEdge string and a KACO
  next to it look the same in Home Assistant
- Sensors that ride out a flaky network instead of flickering
  unavailable every few minutes
- Dawn and dusk garbage filtered out before it reaches your statistics
- A diagnostics download that makes a bug report reproducible

## Will it work with my inverter?

If the manual mentions "SunSpec Modbus", yes. That covers most brands:

**KACO** Powador, blueplanet . **SolarEdge** SE, HD-Wave, Energy Hub .
**Fronius** Symo, Primo, Galvo, GEN24 . **SMA** Sunny Boy, Tripower
(SunSpec answers on unit ID 126, see [docs/sma.md](docs/sma.md)) . **Kostal** Plenticore,
Piko . **Sungrow** SG, SH . **GoodWe** XS, DT, ET . **ABB / FIMER /
Power-One** Aurora, Trio, UNO, REACT . **Delta** Solivia, RPI .
**SunPower** . **Chint Power Systems**

Most inverters need Modbus TCP switched on in their own web interface
before they answer. It is usually off by default.

Developed against a KACO Powador 7.8 TL3. Everything else is the
standard doing its job, so reports about other hardware are welcome.

## Installation

The integration is in the default HACS store.

1. **HACS** -> search for **SunSpec Modbus** -> **Download**
2. **Restart Home Assistant**
3. **Settings -> Devices & Services -> Add Integration -> SunSpec
   Modbus**
4. Choose how to connect:
   - **Enter IP address manually** if you know the inverter's IP
   - **Scan my network** if you do not
   - **Connect via serial port** for RS-485, usually a USB adapter
     on `/dev/ttyUSB0` or `COM3`
5. Confirm port `502` and unit ID `1` (the usual defaults), or the
   serial settings
6. Tick the data blocks you want sensors for

If your inverter picks up a fresh DHCP lease, Home Assistant may also
find it on its own and offer it on the *Devices & Services* page.

## Adding it to the Energy dashboard

Use the **Lifetime energy** sensor as your *Solar production* source
under **Settings -> Dashboards -> Energy**. Nothing else to configure.

SunSpec reports that value in watt-hours, so the number gets large
after a few years. The Energy dashboard handles it correctly either
way, but if you prefer kWh: open the entity, click the cog, change
**Unit of Measurement**, and confirm when HA offers to convert your
existing history.

## Options

Reachable any time via *Settings -> Devices & Services -> SunSpec
Modbus -> Configure*.

| Option | Default | What it does |
|---|---|---|
| Device name prefix | empty | Prefix for the device name, e.g. `Garage`. Useful with several inverters |
| Scan interval | 30 s | How often the inverter is polled |
| Models | sensible defaults | Which SunSpec data blocks become sensors |
| Peak AC power | read from the inverter | Ceiling for the plausibility filter. Readings above it are dropped. Older firmware does not publish its rated power, then you enter it once |
| Scan delay | 0.5 s | Pause between blocks while mapping the inverter. Raise it if setup fails on slow hardware |
| Release the Modbus connection between polls | off | Only needed when another program outside Home Assistant reads the same inverter |
| Inverter powers down when idle | off | Suppresses the unreachable warning for inverters that vanish at night without saying so |
| Capture raw registers | off | Puts the raw Modbus bytes into the diagnostics download, for bug reports |
| Enable experimental export controls | off | The export limit and its relatives. See [Battery and export control](#battery-and-export-control) |

Host, port and unit ID are changed via **Reconfigure** in the same
menu.

## Several inverters on one connection

Add one entry per unit ID, with the same IP and port (or the same
serial port). The integration takes turns on the connection by itself,
which matters because many inverters only accept one Modbus client at
a time. Nothing to switch on.

Each unit ID becomes its own device with its own sensors and options.

You cannot, however, run this alongside another Modbus program reading
the same inverter, be that the old cjne integration, openHAB or a
script. Put a Modbus proxy in front if you need that.

## Battery and export control

> **This can bite.** Writing to an inverter changes its configuration,
> and some devices persist that through a reboot or refuse to hand
> control back. Grid feed-in limits may also be regulated where you
> live.

Steering a battery and capping your export from Home Assistant.

**Battery controls** appear on their own for an inverter with the
SunSpec storage block: charge and discharge rates, a control mode, the
grid charging switch and the minimum reserve. **Fronius GEN24, Verto
and Tauro with a battery** additionally get a *Battery mode* menu, from
"Charge from grid" to "Block discharging", with the powers entered in
watts, and a scheduled discharge that gives the battery's surplus back
over night. With the password of the inverter's web page, temperatures,
the smart meter's position and the grid charging flags come too. See
**[docs/fronius.md](docs/fronius.md)**; the older **Symo, Primo, Eco
and Galvo**, and the **Symo Hybrid** with its battery, are in
**[docs/fronius-symo.md](docs/fronius-symo.md)**. **SolarEdge** keeps its battery
outside SunSpec; the integration reads it from SolarEdge's own
registers, one device per battery, where SolarEdge has enabled them.
See **[docs/solaredge.md](docs/solaredge.md)**.

**Export controls** are a beta and off by default: tick **"Enable
experimental export controls (BETA)"** in the options for an export
limit in percent or watts with an enable switch and a revert timer, a
power factor setpoint, the grid connection switch, and the
`sunspec2.set_export_limit` service action for automations.

**[Full documentation in docs/write-controls.md](docs/write-controls.md)**:
every entity with the register behind it, the revert timer and how not
to get caught by it, and which blocks are deliberately left alone.

If you run the export controls, please
[open an issue](https://github.com/hilman2/ha-sunspec2/issues) with your
inverter model, its firmware version, and what worked. That feedback is
what lets the BETA flag come off.

## When something is wrong

**Download the diagnostics first.** *Settings -> Devices & Services ->
SunSpec Modbus -> Download diagnostics*. Your IP is redacted
automatically, and it contains almost everything needed to work out
what happened.

The cases below are the common ones in a line or two.
**[docs/troubleshooting.md](docs/troubleshooting.md)** has the long
version: how the plausibility filter picks its ceilings, what the log
lines mean, and which field in the diagnostics answers which question.

**Sensors drop out for a few minutes now and then.** Expected on a
shaky network. The integration retries and keeps showing the last good
value for about three minutes before giving up. If gaps are longer than
that, check the WiFi or cable to the inverter.

**The inverter is unreachable every night.** Normal. Most inverters
switch off with the sun and take their network connection with them.
The integration recognises this and stays quiet. If yours vanishes
without announcing it first, switch on **Inverter powers down when
idle** in the options.

**Everything is unavailable after changing the options.** You probably
saved with no data blocks ticked. Re-open the options and tick them
again.

**Power or energy values read `unknown` on a bright day.** The
plausibility filter is set too low. Raise **Peak AC power**, or clear
the field to switch the filter off. The log names the sensor and the
ceiling it used.

**Spikes in your statistics at dawn or dusk.** That is the same filter
doing its job, or not doing it because the ceiling is too high. It is
prefilled from the inverter's own nameplate, so in most cases you can
leave it alone.

**Repairs says "Cannot reach SunSpec inverter".** The inverter really
is not answering. Check power, check the network, and check that no
other Modbus program is holding the connection.

## Coming from cjne/ha-sunspec

**Your entity IDs, history, dashboards and automations are kept.** The
old entities are moved over to this integration on first setup.

1. Uninstall cjne/ha-sunspec in **HACS**
2. **Restart Home Assistant**
3. Install SunSpec Modbus and add it with the **same host, port and
   unit ID** as before
4. A notification confirms how many sensors were migrated

Both integrations cannot run at the same time. If cjne is still loaded,
SunSpec 2 says so in the Repairs panel and waits.

Thanks to [@cjne](https://github.com/cjne) for the original integration
and years of community support. This one builds on that work.

## Removing it

1. *Settings -> Devices & Services -> SunSpec Modbus* -> three-dot menu
   -> **Delete**, for each device
2. Optionally remove it in HACS as well
3. Restart Home Assistant

The inverter itself is untouched, the integration just stops talking to
it. Your recorded history stays until HA's own purge removes it, 10
days by default.

## How polling works

The integration opens one connection to the inverter and reads it every
30 seconds, which you can change in the options. A failed read is
retried after five seconds. If it keeps failing, the sensors hold their
last good value for another five cycles, roughly three minutes, before
they go unavailable. That is what keeps a short network hiccup out of
your graphs.

The connection is held open between polls rather than rebuilt each
time, because that is what the Modbus stack in a typical inverter
copes with best.

## Limitations

- **One Modbus client at a time** on most inverters. Several entries in
  this integration work fine, another program does not. See
  [Several inverters on one connection](#several-inverters-on-one-connection).
- **DHCP discovery needs a fresh lease**, so it misses inverters with a
  static IP. Use **Scan my network** for those.
- **Vendor-specific data blocks** are read, but their points may show
  the raw SunSpec label instead of a translated name.
- **Rated power is detected only where the inverter publishes it.**
  Older firmware needs it entered once.

## Reporting issues

Bug reports are welcome at
<https://github.com/hilman2/ha-sunspec2/issues>. **Always attach the
diagnostics download.**

---

Built on [`pysunspec2`][pysunspec2], the SunSpec Alliance reference
client, hence the name. The integration carries its own fork of it in
[`custom_components/sunspec2/pysunspec2/`][fork], and talks to the
inverter, over TCP and over RS-485, through
[`modbus-connection`][modbus-connection], the Modbus transport Home
Assistant's own integrations use. Meets the Home Assistant **Gold**
quality scale, documented rule by rule in
[`quality_scale.yaml`][quality-scale].

MIT licensed, see [`LICENSE`](LICENSE). The embedded pysunspec2 fork
stays under its Apache 2.0 license, see the `LICENSE` file next to it.

[pysunspec2]: https://github.com/sunspec/pysunspec2
[fork]: custom_components/sunspec2/pysunspec2/__init__.py
[modbus-connection]: https://github.com/home-assistant-libs/modbus-connection
[quality-scale]: custom_components/sunspec2/quality_scale.yaml
