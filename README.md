# SunSpec 2

[![CI](https://github.com/hilman2/ha-sunspec2/actions/workflows/ci.yml/badge.svg)](https://github.com/hilman2/ha-sunspec2/actions/workflows/ci.yml)
[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hilman2/ha-sunspec2)
[![GitHub release](https://img.shields.io/github/v/release/hilman2/ha-sunspec2)](https://github.com/hilman2/ha-sunspec2/releases)

**Works with: KACO · SolarEdge · Fronius · SMA · Kostal · Sungrow · Delta ·
GoodWe · ABB / FIMER · Power-One · SunPower · Chint Power Systems** — and
any other inverter, meter, or battery that speaks the SunSpec Modbus
specification. SunSpec is the manufacturer-independent standard for
photovoltaic equipment communication; if your device documentation
mentions "SunSpec Modbus" or you can find the SunSpec ID register
holding `0x53756e53` ("SunS") at one of the standard base addresses,
this integration will talk to it.

Home Assistant integration for SunSpec Modbus devices: solar inverters,
energy meters, and battery systems that follow the SunSpec specification.

- **Current `pysunspec2`** (1.3.3+, with the `[serial]` extra so HA installs
  pull `pyserial` correctly).
- **Debugging-first**: structured logging with device context, one-click
  diagnostics dump, opt-in raw register capture, classified errors surfaced
  in the Repairs panel.
- **Actively maintained**: bug reports, PRs, and feature requests welcome.

## Features

- Modbus TCP polling of any SunSpec-compliant device, auto-discovering the
  device's models on first connect
- Structured log lines prefixed with `[host:port#unit_id]` so multi-device
  installs are triageable from a single log stream
- One-click diagnostics download under
  *Settings → Devices & Services → SunSpec 2 → Download diagnostics* —
  JSON dump with redacted host, scanned models, latest values per point,
  recent errors per category, and version info
- Opt-in raw register capture: every Modbus read is also stored as hex bytes
  in the diagnostics dump, so a bug can be reproduced from the JSON alone
- Repairs panel integration for persistent transport / protocol / device
  errors with actionable troubleshooting text in English and German

## Installation via HACS

1. **HACS** → three-dot menu → **Custom repositories**
2. URL: `https://github.com/hilman2/ha-sunspec2`, Type: `Integration`, **Add**
3. In HACS find **SunSpec 2** → **Download**
4. **Restart Home Assistant**
5. **Settings → Devices & Services → Add Integration → SunSpec 2**
6. Enter the inverter's host, port (typically 502), and unit ID (typically 1)
7. On the second step pick which SunSpec models to expose as sensors

If your inverter is on the same LAN as Home Assistant and uses one of
the supported manufacturers, HA will detect it via DHCP automatically
and offer it as a discovered integration on the *Devices & Services*
page — you only have to confirm.

## Removing the integration

1. **Settings → Devices & Services → SunSpec 2** → three-dot menu →
   **Delete** for each configured device. This removes the entry, all
   sensor entities, and their device entry from the registry.
2. (Optional) In HACS, three-dot menu on **SunSpec 2** → **Remove**.
   This deletes the `custom_components/sunspec2` directory from your
   config.
3. **Restart Home Assistant** so HA forgets the integration was ever
   loaded.

Removing the integration leaves the inverter itself untouched - it
just stops talking to it. Recorder history for the deleted entities
is kept by HA's Recorder until its own purge interval kicks in (10
days by default), so dashboards that reference the entities will go
to "unknown" but not lose old data immediately.

## Migration

If you previously used another SunSpec integration and your Home Assistant
already has sensor entities under the `sunspec` platform (e.g.
`sensor.inverter_three_phase_watts`), SunSpec 2 will retarget them to its own
platform on first setup so your **entity IDs, Recorder history, dashboards,
and automations are preserved**.

The migration runs automatically. The previous integration must be
uninstalled first, otherwise both would compete for the inverter's single
Modbus TCP slot — SunSpec 2 detects this and refuses to start with a clear
message in the Repairs panel until the conflict is resolved.

**Migration steps:**

1. In **HACS**, uninstall the previous SunSpec integration
2. **Restart Home Assistant**
3. Install **SunSpec 2** as described above
4. Add the SunSpec 2 integration with the **same host, port and unit ID**
   you used before
5. A notification confirms: *"X sensor(s) were migrated from sunspec to
   sunspec2. Their entity IDs and Recorder history have been preserved."*

That's it. Your Energy dashboard, automations, and historical graphs keep
working without any further action.

## Supported devices

Anything that implements the [SunSpec Information Model][sunspec-spec],
typically over Modbus TCP. Tested in production against a KACO Powador
7.8 TL3 (firmware V2.30). Other SunSpec inverters — Fronius, SMA,
SolarEdge, Enphase Envoy, Outback and others — should work as well.

[sunspec-spec]: https://sunspec.org/

## Reporting issues

Bug reports are welcome at https://github.com/hilman2/ha-sunspec2/issues.
Please include the **diagnostics download**
(*Settings → Devices & Services → SunSpec 2 → Download diagnostics*) — the
host is automatically redacted, and the captured raw register bytes (when
enabled) make most bugs reproducible from the JSON alone.

## License

MIT. See `LICENSE`.
