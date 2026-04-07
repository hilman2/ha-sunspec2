# ha-sunspec2

[![CI](https://github.com/hilman2/ha-sunspec2/actions/workflows/ci.yml/badge.svg)](https://github.com/hilman2/ha-sunspec2/actions/workflows/ci.yml)
[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hilman2/ha-sunspec2)
[![GitHub release](https://img.shields.io/github/v/release/hilman2/ha-sunspec2)](https://github.com/hilman2/ha-sunspec2/releases)

Home Assistant custom integration for SunSpec Modbus devices: solar inverters,
energy meters, and battery systems that follow the SunSpec specification.

Debugging-first brownfield rewrite of [cjne/ha-sunspec][cjne], based on the
modern `pysunspec2` library (1.3.x). The original integration is MIT-licensed
and functional, but maintainer response times of several weeks made bugfixes
painful and the architecture made user bug reports hard to act on. This fork
aims to deliver:

1. Modern `pysunspec2` (1.3.3+, with the `[serial]` extra so HA installs
   pull `pyserial` correctly).
2. A debugging-first architecture: structured logging with device context,
   one-click diagnostics dump, opt-in raw register capture, classified errors
   in the Repairs panel.
3. **Lossless migration from `cjne/ha-sunspec`**: existing entity IDs and
   Recorder history are preserved automatically when you switch.

## Status

**Production-tested.** Smoke-tested against a real KACO Powador 7.8 TL3 across
all six development phases. Currently in v0.7.x — Phase 6 (HACS submission +
CI) is the final phase before tagging v1.0.

[cjne]: https://github.com/cjne/ha-sunspec

## Features

- **Modbus TCP polling** of any SunSpec-compliant device
- **Structured logging** with `[host:port#unit_id]` prefix on every record
  so multi-device installs are triageable from a single log stream
- **Diagnostics download**: Settings → Devices & Services → SunSpec 2 → drei
  Punkte → Download diagnostics. JSON dump with redacted host, scanned
  models, latest values per point, recent errors, raw register captures (if
  enabled), and version info.
- **Opt-in raw register capture**: toggle in the options flow. Once enabled,
  every modbus read is also stored as hex bytes in the diagnostics dump so
  bug reports can be reproduced from the JSON alone.
- **Repairs panel integration**: persistent transport / protocol / device
  errors surface in Settings → System → Repairs with actionable
  troubleshooting text in English and German.
- **Auto-migration from cjne/ha-sunspec**: install our integration after
  uninstalling cjne, and existing sensors get retargeted automatically with
  full Recorder history preserved. Conflict guard refuses setup while cjne
  is still actively running, so the two integrations never race over the
  inverter's single Modbus TCP slot.

## Installation via HACS

1. **HACS** → drei Punkte oben rechts → **Custom repositories**
2. URL: `https://github.com/hilman2/ha-sunspec2`, Type: `Integration`, **Add**
3. In HACS find **SunSpec 2** → **Download**
4. **Restart Home Assistant**
5. **Settings → Devices & Services → Add Integration → SunSpec 2**
6. Enter the inverter's host, port (typically 502), and unit ID (typically 1)
7. On the second step pick which SunSpec models to expose as sensors

## Migration from cjne/ha-sunspec

If you currently use the upstream `cjne/ha-sunspec` integration and want to
switch without losing your Recorder history:

1. Open HACS, **uninstall** `cjne/ha-sunspec`
2. **Restart Home Assistant** (cjne's entities become orphans in the entity
   registry but stay there with their entity IDs and history)
3. Install **SunSpec 2** as described above
4. Add the SunSpec 2 integration with the same host/port/unit_id you used
   for cjne
5. A persistent notification will confirm: "X sensor(s) were migrated from
   the cjne/ha-sunspec integration to sunspec2. Their entity IDs and
   Recorder history have been preserved."

If you try to install SunSpec 2 *while* cjne is still active, the setup will
refuse with a Repairs panel issue ("cjne/ha-sunspec ist noch aktiv") and
clear three-step instructions. Once you uninstall cjne and restart, the
SunSpec 2 setup retries automatically.

## Supported devices

Anything that implements the [SunSpec Information Model][sunspec-spec],
typically over Modbus TCP. Tested in production:

- KACO Powador 7.8 TL3 (firmware V2.30)

Other inverters that work with `pysunspec2` should work here too, including
Fronius, SMA, SolarEdge, Enphase Envoy, Outback, etc. The integration
auto-discovers the device's SunSpec models on first connect.

[sunspec-spec]: https://sunspec.org/

## Development

Tests run on WSL Ubuntu with `uv` + Python 3.13. pytest does NOT work on
Windows Python because the HA test deps import `fcntl`. From a Windows shell:

```bash
wsl -d Ubuntu ~/venvs/ha-sunspec2/bin/python -m pytest tests/ --asyncio-mode=auto -v
```

CI runs ruff lint + ruff format check + hassfest + HACS validation + pytest
on every push and pull request via `.github/workflows/ci.yml`.

## License

MIT, original copyright preserved from [cjne/ha-sunspec][cjne]. See `LICENSE`.

## Issues

Bug reports are welcome at https://github.com/hilman2/ha-sunspec2/issues.
Please include the **diagnostics download** (Settings → Devices & Services →
SunSpec 2 → drei Punkte → Download diagnostics) — the host is automatically
redacted.
