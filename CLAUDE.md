# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ha-sunspec2` is a Home Assistant custom integration for SunSpec Modbus devices (solar inverters, energy meters). It is a **brownfield rewrite** of [cjne/ha-sunspec](https://github.com/cjne/ha-sunspec) under a new domain (`sunspec2`) so both can coexist in HACS. The project is in early-phase development; do not assume any feature exists unless you have read the code.

`REWRITE_PLAN.md` is the source of truth for scope, sequencing, and locked-in decisions. Read it before proposing structural changes — every refactor is scheduled to a specific phase, and the phase boundaries exist for `git bisect` reasons.

## Phase discipline (critical)

Work in this repo is intentionally sequenced into phases. The current state is **Phase 3 done** (v0.4.0 released 2026-04-07).

- **`pysunspec2` is now pinned to `1.3.3` with the `[serial]` extra.** The extra is load-bearing — `sunspec2.modbus.modbus` does an unconditional `import serial` at module top, but pysunspec2 declares pyserial only as an optional extra. Without `[serial]`, fresh HA installs that lack pyserial transitively will hard-fail at import. Latent upstream bug since at least 1.1.5; fixed in our manifest as part of Phase 1.
- **Do not change the `unique_id` format** until Phase 5 migration logic is in place. The format produced by `get_sunspec_unique_id` in `custom_components/sunspec2/__init__.py` must match upstream `cjne/ha-sunspec` exactly so that Phase 5 auto-migration of the entity registry preserves Recorder history.
- **Phase 0 = verbatim copy.** Only the package folder name, `DOMAIN`, `NAME`, `VERSION`, `ISSUE_URL`, and manifest fields were renamed from upstream. Internal typos like `uniqe_id`, `conifg`, `setttins`, `wheneve` are preserved on purpose; they get fixed in Phase 4 (and only in identifiers that are NOT part of the entity registry contract).
- **Model 103 entity-id rename caveat (Phase 1 finding).** `pysunspec2` 1.3.2 ships an updated SunSpec models repo where model 103's group `name` changed from `"inverter"` to `"inverter_three_phase"`. New devices added under our stack get `sensor.inverter_three_phase_*` instead of the legacy `sensor.inverter_*`. Phase 5 must decide whether the auto-migration aliases the new form back to the legacy form for users coming from `cjne/ha-sunspec`. See `REWRITE_PLAN.md` Phase 1 findings for details.
- **Phase 2 known issue: hot reload kills sensors.** Toggling ANY option in the options flow (capture_raw_registers, scan_interval, anything) at runtime triggers an entry reload, after which sensors stay "unavailable" until the user restarts HA entirely. Verified against KACO Powador 7.8 TL3. Two failed fix attempts (`6f4bb72` reverting cache invalidation, `a2dffc7` making `close()` actually disconnect) did not solve it. Workaround: restart HA after toggling. Phase 4 must rebuild the connection lifecycle as part of the api.py refactor — likely instance-scoped client (drop CLIENT_CACHE) plus an integration test that imitates the inverter's single-Modbus-TCP-slot constraint.
- **`api.close()` actually closes now.** For years, `SunSpecApiClient.close()` called `client.close()` which dispatched to a `pass` stub in pysunspec2. Phase 2 fixed this to call `cached.disconnect()` instead, closing the TCP socket via `ModbusClientTCP.disconnect`. The cached client object stays in CLIENT_CACHE so the next read auto-reconnects via `ModbusClientTCP.read`'s `if self.socket is None: self.connect(...)` path. This is correct on its own merits (resource leak fix) but did NOT solve the hot-reload issue above — they have different causes.
- **Phase 3 typed errors are the contract for the coordinator catch path.** `errors.py` defines `SunSpecError` and four subclasses (`TransportError`, `ProtocolError`, `DeviceError`, `TransientError`). `api.py` raises one of those at the pysunspec2 boundary; the coordinator catches `SunSpecError` and dispatches via category. The old `ConnectionError`/`ConnectionTimeoutError` (which shadowed Python builtins) are gone — never reintroduce them. Tests and `config_flow.set_connection_error` use the typed names.
- **Repairs panel thresholds.** `_record_error` in the coordinator escalates to `ir.async_create_issue` based on category: `protocol` fires on the first occurrence, `transport` and `device` after 3 consecutive failures, `transient` never escalates. Issues clear automatically on the next successful update cycle and on `async_unload_entry`. Translation keys live in `translations/en.json` and `translations/de.json` under `issues.<category>_error`.
- When asked to fix a bug or add a feature, first check `REWRITE_PLAN.md` to see whether it is already scheduled to a later phase. If so, flag that to the user instead of front-running the plan.

## Architecture

Single-platform (sensor) integration that polls a SunSpec Modbus TCP device on a configurable interval.

```
config_flow.py  →  __init__.py (async_setup_entry)
                        │
                        ├── SunSpecApiClient (api.py)
                        │       └── pysunspec2 SunSpecModbusClientDeviceTCP
                        │              (cached per host:port:unit_id in CLIENT_CACHE)
                        │
                        └── SunSpecDataUpdateCoordinator (__init__.py)
                                └── sensor.py: SunSpecSensor / SunSpecEnergySensor
                                        └── SunSpecEntity (entity.py)
```

Key data flow:

1. `SunSpecApiClient.modbus_connect` opens a TCP socket, runs `client.scan(...)` to discover SunSpec models, and caches the connected client in the class-level `CLIENT_CACHE` keyed by `host:port:unit_id`.
2. The coordinator's `_async_update_data` filters discovered model IDs against the user's `option_model_filter` and reads each enabled model into a `SunSpecModelWrapper`.
3. `SunSpecModelWrapper` (currently in `api.py`, scheduled to move to `models.py` in Phase 4) walks the SunSpec model tree (`points`, `groups`, repeating groups indexed by integer suffix) and exposes `getKeys()`, `getValue()`, `getMeta()`. The `key` format is either `pointName` or `groupName:index:pointName`.
4. `sensor.py` builds one `SunSpecSensor` per `(model_id, model_index, key)` triple, mapping SunSpec units to HA `UnitOfX` and `SensorDeviceClass` via the `HA_META` table. Energy points become `SunSpecEnergySensor`, which subclasses `RestoreSensor` and substitutes the last-known value when the device reports `0` (so `total_increasing` long-term stats do not get reset).

Config entries are versioned. `async_migrate_entry` currently handles v1 → v2 (`slave_id` → `unit_id`). Bump `VERSION` on `SunSpecFlowHandler` and add a migration branch when changing the schema.

The `_test_connection` path in `config_flow.py` reads the SunSpec common model (`async_get_device_info`, model `1`) to derive a unique ID from the device serial (`SN`); when the device omits the serial it falls back to `host:port:unit_id`. Do not change this fallback without understanding it — some devices in the wild do not expose `SN`.

## Test environment

There are no tests yet. When tests are added (Phase 4):

- pytest does **not** run on Windows Python because the HA test deps import `fcntl`. Always run tests in **WSL Ubuntu** with **`uv` + Python 3.13**, never via Windows Python.
- From a Windows shell, use `wsl -d Ubuntu` (the default WSL distro on this machine is `docker-desktop`, which does not have a usable test environment).

There is no `pyproject.toml`, no `requirements*.txt`, no lint config, and no CI yet — all of those land in Phase 6. If you need to lint locally, the cached intent is `ruff` and `mypy --strict`, but neither is configured in the repo.

## Repo conventions

- The package lives under `custom_components/sunspec2/`. The empty `custom_components/__init__.py` is required so Home Assistant treats it as a namespace package — do not delete it.
- Standalone working folder at `D:\Git\ha-sunspec2\`. **It is not yet a git repo** (`git init` happens at the start of Phase 1). Treat the working tree as ground truth; there is no history to consult yet.
- Target HA version is `2024.6.0` (locked in `hacs.json`). The codebase already uses `async_forward_entry_setups`, `core_config.Config`, etc.; do not regress to legacy APIs to support older HA.
- License is MIT, original cjne copyright preserved in `LICENSE`. New files do not need a header.

## Debugging-first principles

Phases 2 to 5 are shaped by these rules — apply them when touching error-handling or logging code, even ahead of the formal phase work:

1. Every error gets a category, a context (host/port/unit_id/model_id), and an actionable message. The bare `except Exception: _LOGGER.warning(exception)` in `_async_update_data` is a known wart that Phase 3 replaces — do not propagate that pattern elsewhere.
2. User-actionable problems belong in the Repairs panel (`ir.async_create_issue`), not in `_LOGGER.warning` spam.
3. Anything you would need to reproduce a bug should be dumpable via `async_get_config_entry_diagnostics` (Phase 2 — file does not exist yet).
