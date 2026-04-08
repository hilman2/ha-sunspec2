# SunSpec 2

[![CI](https://github.com/hilman2/ha-sunspec2/actions/workflows/ci.yml/badge.svg)](https://github.com/hilman2/ha-sunspec2/actions/workflows/ci.yml)
[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hilman2/ha-sunspec2)
[![GitHub release](https://img.shields.io/github/v/release/hilman2/ha-sunspec2)](https://github.com/hilman2/ha-sunspec2/releases)
[![Quality Scale](https://img.shields.io/badge/Quality%20Scale-Gold-FFD700.svg)](https://developers.home-assistant.io/docs/core/integration-quality-scale/)

**Works with: KACO . SolarEdge . Fronius . SMA . Kostal . Sungrow . Delta .
GoodWe . ABB / FIMER . Power-One . SunPower . Chint Power Systems** and any
other inverter, meter or battery that speaks the SunSpec Modbus
specification. SunSpec is the manufacturer-independent standard for
photovoltaic equipment communication; if your device documentation
mentions "SunSpec Modbus" or you can find the SunSpec ID register holding
`0x53756e53` ("SunS") at one of the standard base addresses, this
integration will talk to it.

## What this is

A Home Assistant integration for any device that implements the
[SunSpec Information Model][sunspec-spec] over Modbus TCP. Built on
[`pysunspec2`][pysunspec2] (the official SunSpec Alliance reference
client) - hence the **`sunspec2`** name. The integration auto-discovers
which SunSpec model blocks the device exposes and turns each readable
point into a Home Assistant sensor.

## Use cases

- **Plug your inverter into the HA Energy dashboard** with the lifetime
  energy register as the production source. No template sensors, no
  Modbus YAML, no per-brand integration needed.
- **Multi-brand homes**: cover a SolarEdge string and a KACO Powador
  with the same integration, no setup difference.
- **Production monitoring with automations**: build "battery charge from
  PV surplus" or "switch on the wallbox when production exceeds X kW"
  flows directly off the AC power sensor.
- **Diagnostic dashboards**: temperatures, operating state, error
  events and DC-side currents/voltages are all exposed as separate
  sensors so a card with a glance at "what is the inverter doing right
  now" is one drag-and-drop away.

## Features

- **Auto-discovery of SunSpec model blocks** on the device, with model
  multi-select in the config flow so the user picks which families
  become sensors.
- **Active network scan** ("Add Integration -> SunSpec -> Scan my
  network"): probes Modbus TCP port 502 across the local subnet,
  ARP-matches responders against a curated SunSpec vendor MAC OUI
  list, and floats inverter-vendor matches to the top of the picker.
- **Passive DHCP discovery** as a best-effort second path: when HA sees
  a fresh DHCP lease whose MAC matches one of the supported vendor
  OUIs, the integration appears as a discovered tile in
  *Devices & Services*.
- **Auto-detected nameplate AC power**: reads SunSpec model 120 (`WRtg`)
  or model 121 (`WMax`) on the first cycle and pre-fills the
  plausibility filter so the user does not have to type the inverter's
  rated power by hand.
- **Plausibility filter** drops dawn / dusk garbage (the MW or TWh
  spikes some inverters report at startup) before they poison
  long-term statistics.
- **Resilience for flaky inverter networks**: in-cycle retry after a
  short pause, plus stale-data tolerance that keeps sensors on their
  last good value through up to five consecutive failed cycles. A
  KACO Powador with a chronically twitchy ethernet link no longer
  flips to "unavailable" every other minute.
- **Friendly device names**: the device shows up in HA as
  `Powador 7.8 TL3` (or whatever the `Md` field of common model 1
  reports) instead of a generic SunSpec block label.
- **Structured per-device logging** with `[host:port#unit_id]` prefix
  on every line so multi-inverter setups stay triageable from a
  single log stream.
- **One-click diagnostics dump** with the host redacted, plus optional
  raw-register capture so a bug report can be reproduced from the
  JSON file alone.
- **Repairs panel integration** for persistent transport / protocol /
  device errors with actionable troubleshooting text in English and
  German.

## Supported devices

Tested in production against a **KACO Powador 7.8 TL3** (firmware
V2.30). Designed to work with anything that implements the SunSpec
Information Model over Modbus TCP, including:

- **KACO** Powador, blueplanet
- **SolarEdge** SE / HD-Wave / Energy Hub
- **Fronius** Symo, Primo, Galvo
- **SMA** Sunny Boy / Tripower (with the Speedwire Modbus profile
  enabled in Sunny Explorer)
- **Kostal** Plenticore, Piko
- **Sungrow** SG / SH series
- **GoodWe** XS / DT / ET
- **ABB / FIMER / Power-One** Aurora, Trio, UNO, REACT
- **SunPower** SPR-8000m / SPR-10000m
- **Delta** Solivia, RPI
- **Chint Power Systems** CPS

Anything not in the list above that advertises "SunSpec Modbus
TCP" in its documentation should also work; the integration is
brand-agnostic by design.

## Installation via HACS

1. **HACS** -> three-dot menu -> **Custom repositories**
2. URL: `https://github.com/hilman2/ha-sunspec2`, Type: `Integration`
3. In HACS find **SunSpec Modbus** -> **Download**
4. **Restart Home Assistant**
5. **Settings -> Devices & Services -> Add Integration -> SunSpec Modbus**
6. Pick the connection type:
   - **Enter IP address manually** for Modbus TCP if you know the IP
   - **Scan my network** for Modbus TCP if you don't know the IP
   - **Connect via serial port** for Modbus RTU over RS-485
     (typically a USB-to-RS-485 adapter on `/dev/ttyUSB0` or `COM3`)
7. Confirm the connection details (port 502 + unit ID 1 for TCP, or
   serial port + baud rate + parity + unit ID for RTU)
8. Pick the SunSpec models you want sensors for and optionally enter
   the inverter's nameplate AC power for the plausibility filter

If your inverter is on the same LAN as Home Assistant and uses one of
the supported manufacturers, HA can also detect it via DHCP
automatically and offer it as a discovered integration on the
*Devices & Services* page - you only have to confirm.

## Configuration parameters

| Parameter | Where | Default | Purpose |
|---|---|---|---|
| `transport` | Setup | `tcp` | Modbus transport. Either `tcp` or `rtu`. RTU mode talks to the inverter over a serial line (RS-485) instead of Ethernet |
| `host` | Setup TCP, Reconfigure | - | Inverter IP or hostname (TCP only) |
| `port` | Setup TCP, Reconfigure | `502` | Modbus TCP port (TCP only) |
| `serial_port` | Setup RTU | - | Serial device path, e.g. `/dev/ttyUSB0` or `COM3` (RTU only) |
| `baudrate` | Setup RTU | `9600` | Serial baud rate (RTU only) |
| `parity` | Setup RTU | `N` | Serial parity, `N` (none) or `E` (even). RTU only |
| `unit_id` | Setup, Reconfigure | `1` | Modbus unit / slave ID |
| `prefix` | Setup, Options | empty | Optional prefix for the device name (e.g. `Garage`, `Cellar`) for multi-inverter setups |
| `scan_interval` | Setup, Options | `30 s` | How often the coordinator polls the inverter |
| `models_enabled` | Setup, Options | sensible defaults | Which SunSpec model blocks become sensors |
| `max_ac_power_kw` | Setup, Options | auto-detect from model 120/121 | Plausibility filter ceiling. Drops readings above this value |
| `capture_raw_registers` | Options | off | Wraps every Modbus read so the bytes appear in the diagnostics dump |

## How the integration polls

A single `DataUpdateCoordinator` per config entry runs the following
loop, by default every 30 seconds:

1. Acquire a class-level **per-(host, port) `asyncio.Lock`**. This
   matters when several config entries share the same Modbus TCP
   gateway: KACO Powador and many other devices only allow one
   Modbus TCP slot at a time, so two coordinators behind one
   gateway would race each other without this lock.
2. Walk every enabled SunSpec model and read its valid points.
3. On the first successful cycle: cache the inverter's full model
   list, the common-block device info, and the auto-detected
   nameplate.
4. On a successful cycle: reset every per-category failure counter
   and clear any active Repairs issues. If the previous run had
   failed, log a single recovery WARNING so the user can correlate
   the recovery moment in the log.
5. Close the TCP socket with `SO_LINGER=0` so the kernel sends a
   TCP RST instead of a polite FIN. Single-slot inverters free
   their slot immediately on RST instead of waiting on their own
   keep-alive.

**On failure**, the coordinator does NOT immediately mark the entity
unavailable. Instead:

1. **In-cycle retry**: release the gateway lock, sleep 5 seconds,
   then run the cycle one more time. This catches the most common
   failure mode (a one-shot blip).
2. **Stale-data tolerance**: while consecutive cycles keep failing,
   the entity `available` property serves the last good value for
   up to 5 cycles. Only after that does the sensor flip to
   "unavailable". With the default 30 s interval plus the 5 s
   retry, this rides out roughly three minutes of dropped
   connectivity without bouncing the long-term statistics graphs
   to "unknown".
3. **Repairs panel** issues fire after 1 consecutive protocol error
   or 3 consecutive transport / device errors, with actionable
   text in English and German. Issues clear automatically on the
   next successful cycle.

## Example uses

### Energy dashboard

The lifetime energy sensor (`WH` on a three-phase inverter, key
`watthours`) reports the inverter's total produced energy in Wh and
has the `total_increasing` state class. Add it to **Settings ->
Dashboards -> Energy** as a *Solar production* source. No template
sensor needed.

### Surplus-driven wallbox automation

The AC power sensor (`W`, key `watts`) gives the current production
in watts. A simple "switch on the wallbox if production exceeds
3 kW for 5 minutes" automation reads it directly:

```yaml
trigger:
  - platform: numeric_state
    entity_id: sensor.powador_7_8_tl3_ac_power
    above: 3000
    for: "00:05:00"
action:
  - service: switch.turn_on
    target:
      entity_id: switch.wallbox
```

### Quick-glance dashboard card

Drop the **device** for your inverter onto a dashboard via *Add
Card -> By device*. The integration's friendly device name plus
the per-entity translation_keys means the card already shows
`AC power`, `DC voltage`, `Frequency`, `Cabinet temperature` and so
on out of the box - no manual `name:` overrides.

## Troubleshooting

**The Diagnostics dump is the primary debugging tool.** Get it via
*Settings -> Devices & Services -> SunSpec Modbus -> Download
diagnostics*. The host field is automatically redacted, the captured
raw register bytes (when enabled) make most bugs reproducible from
the JSON file alone, and the per-category recent-error buffer plus
the consecutive-failure counters tell you immediately what went
wrong.

Common situations and what to check:

- **All sensors `unavailable` after a model selection edit**: you
  saved the options form with no models ticked. Re-open the options
  flow and re-tick the models you want. v0.7.6 onwards refuses to
  save an empty selection; if you are on an older version, update
  first.
- **All sensors `unavailable` and Repairs shows a `cjne_conflict`
  issue**: the legacy `cjne/ha-sunspec` integration is still loaded
  for the same host. Single-slot inverters cannot be polled from
  two integrations at once. Uninstall cjne via HACS, restart Home
  Assistant, and the migration runs automatically.
- **Sensors flip to `unavailable` for a few minutes every few hours**:
  this is exactly what the resilience features (in-cycle retry +
  stale tolerance) are designed to absorb. If you are still seeing
  this on v0.8.x or later, it usually means the underlying network
  link is dropping for longer than three minutes - check the WiFi /
  ethernet to the inverter.
- **Dawn / dusk spikes in your statistics**: set the **Peak AC
  power** option to your inverter's nameplate. The plausibility
  filter drops every reading above that value, including the MW /
  TWh garbage some inverters generate at startup.
- **Repairs panel says "Cannot reach SunSpec inverter"**: open the
  diagnostics dump, look at `recent_errors`. If the same error
  appears three times in a row, the inverter is genuinely
  unreachable - check power, network and that no other Modbus
  client is holding the slot.

## Experimental: inverter write controls (BETA, opt-in)

> **Important: this is genuinely risky.** Read the whole section before
> ticking the opt-in. Writing to a Modbus register on a real inverter
> can persist a configuration change that locks you out of the device
> until a physical reset. There are no test users yet who have
> validated this code path against real hardware - the integration
> owner does not have an inverter that exposes SunSpec model 123.

v0.12.0 adds an **opt-in beta** for setpoint control via the standard
**SunSpec model 123 (Immediate Controls)**. When enabled, the
integration registers two extra HA platforms (`number` and `switch`)
plus a service action so you can curtail your inverter's export from
HA automations.

### What you get when you enable it

| Entity | Type | SunSpec point | What it does |
|---|---|---|---|
| Export limit | Number (0..100 %) | model 123 `WMaxLimPct` | Caps AC output to N % of nameplate. Set to 0 for "Nulleinspeisung" |
| Power factor setpoint | Number (-1..1) | model 123 `OutPFSet` | Cos-phi setpoint for reactive power control |
| Export limit enabled | Switch | model 123 `WMaxLim_Ena` | The export limit only takes effect while this switch is ON |
| Power factor enabled | Switch | model 123 `OutPFSet_Ena` | The PF setpoint only takes effect while this switch is ON |
| Inverter grid connection | Switch | model 123 `Conn` | **Most dangerous**: turning OFF disconnects the inverter from the grid entirely |

Plus the **`sunspec2.set_export_limit`** service action with two
parameters (`config_entry_id`, `percent`, optional `enable`) so
automations can flip the export limit without going through a
Number entity.

### Why it's opt-in

- **Vendor deviations**: SunSpec model 123 is part of the standard
  but vendors are inconsistent about which firmware revisions
  expose it, what scale-factor handling they apply, and what
  ranges they accept. We test against the spec, not against your
  specific firmware.
- **Persistence semantics vary**: some inverters persist a write
  through power-cycle, others reset to defaults on reboot. Some
  require a non-zero `WMaxLimPct_RvrtTms` (revert timeout) to
  prevent the limit from sticking forever.
- **No real-hardware test on the integration owner's side**: my
  KACO Powador 7.8 TL3 does not expose model 123, so I cannot
  smoke-test the write path against a live device. The first
  community user who runs this on a real inverter is doing the
  validation work, hence the BETA flag.

### How to enable

1. Open *Settings → Devices & Services → SunSpec Modbus → Configure*
2. Click through to the model options step
3. Tick **"Enable experimental write controls (BETA)"**
4. Save - the integration reloads and the Number / Switch entities
   appear under your inverter's device card

If your inverter does NOT expose model 123, the entities will not
register even with the flag on - the diagnostics dump's
`scanned_models` array tells you whether 123 is present.

### Looking for testers

If you are running this beta on a real inverter and want to share
results, please open an issue on
<https://github.com/hilman2/ha-sunspec2/issues> with the model name,
the firmware revision, and a description of which writes worked
and which did not. That feedback is what will let us drop the
BETA flag and ship v1.0.

## Known limitations

- **Single Modbus TCP slot devices** like KACO Powador can only be
  polled from one integration at a time. Running ha-sunspec2 in
  parallel with cjne, openHAB, or any other Modbus client against
  the same device produces flapping sensors. The integration
  detects an active cjne entry on the same host and refuses to
  start with a clear Repairs panel message.
- **DHCP discovery requires a fresh lease**, which means it does
  not fire for inverters with a static IP (most home installs)
  and only fires every few hours for DHCP leases. The active
  **Scan my network** path is the workaround for both situations.
- **Vendor-specific SunSpec extension models** (the 6xx, 7xx, 8xx
  blocks) are read if the inverter exposes them, but the per-point
  translations are limited to the standard inverter / nameplate /
  settings keys. Vendor-specific events fall back to the SunSpec
  spec label from `pysunspec2`.
- **Auto-detected nameplate** only works for inverters that
  expose model 120 (`WRtg`) or model 121 (`WMax`). Older or
  vendor-stripped firmware (notably some KACO Powador models)
  does not have these blocks; the user has to type the
  nameplate by hand once during setup.

## Migration from cjne/ha-sunspec

If you previously used the [cjne/ha-sunspec][cjne] integration and
your Home Assistant already has sensor entities under the `sunspec`
platform (e.g. `sensor.inverter_three_phase_watts`), SunSpec 2 is
the natural upgrade path. **Your entity IDs, Recorder history,
dashboards, and automations are preserved** - the migration
automatically retargets the existing entities to the new platform
on first setup.

The cjne integration is the original community port and laid the
groundwork for everything that followed. SunSpec 2 builds on that
work, adds the resilience features (in-cycle retry, stale-data
tolerance), the active network scan, the friendly device names,
and the full Bronze + Silver + Gold quality scale items. Big thanks
to **@cjne** for the original codebase and the years of community
support.

**Migration steps:**

1. In **HACS**, uninstall cjne/ha-sunspec
2. **Restart Home Assistant**
3. Install SunSpec Modbus as described in *Installation via HACS*
4. Add the SunSpec Modbus integration with the **same host, port
   and unit ID** you used before
5. A notification confirms: *"X sensor(s) were migrated from
   sunspec to sunspec2. Their entity IDs and Recorder history have
   been preserved."*

That's it. The Energy dashboard, automations and historical graphs
keep working without any further action.

If both integrations are loaded at the same time, SunSpec 2 detects
the conflict and refuses to start with a clear Repairs panel
message until cjne is uninstalled. Single-slot inverters cannot be
polled from two integrations simultaneously.

[cjne]: https://github.com/cjne/ha-sunspec

## Removing the integration

1. **Settings -> Devices & Services -> SunSpec Modbus** -> three-dot
   menu -> **Delete** for each configured device. This removes the
   entry, all sensor entities, and their device entry from the
   registry.
2. (Optional) In HACS, three-dot menu on **SunSpec Modbus** ->
   **Remove**. This deletes the `custom_components/sunspec2`
   directory from your config.
3. **Restart Home Assistant** so HA forgets the integration was
   ever loaded.

Removing the integration leaves the inverter itself untouched. It
just stops talking to it. Recorder history for the deleted entities
is kept by HA's Recorder until its own purge interval kicks in
(10 days by default), so dashboards that reference the entities
will go to "unknown" but not lose old data immediately.

## Quality scale

This integration meets the Home Assistant **Gold** quality scale.
Every Bronze, Silver and Gold rule is documented in
[`custom_components/sunspec2/quality_scale.yaml`][quality-scale]
with one entry per rule, marked done / exempt with a one-paragraph
explanation of how the rule is satisfied.

The current state at a glance:

- **Bronze**: 18/18 done or exempt - config flow, runtime data,
  test before configure / before setup, device-info, has-entity-name,
  unique config entry, removal docs, etc.
- **Silver**: 9/9 done or exempt - entity-unavailable with
  stale-data tolerance, log-when-unavailable, parallel-updates,
  test coverage with 120+ tests, etc.
- **Gold**: 18/18 done or exempt - active discovery,
  reconfiguration flow, stale-devices, entity-translations,
  exception-translations, icon-translations, entity-category,
  entity-disabled-by-default, all the docs-* items, etc.

[quality-scale]: custom_components/sunspec2/quality_scale.yaml

## Reporting issues

Bug reports are welcome at <https://github.com/hilman2/ha-sunspec2/issues>.
**Always include the diagnostics download** (*Settings -> Devices &
Services -> SunSpec Modbus -> Download diagnostics*). The host is
automatically redacted, and the captured raw register bytes (when
enabled) make most bugs reproducible from the JSON alone.

[pysunspec2]: https://github.com/sunspec/pysunspec2
[sunspec-spec]: https://sunspec.org/

## License

MIT. See [`LICENSE`](LICENSE).
