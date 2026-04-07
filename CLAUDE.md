# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ha-sunspec2` is a Home Assistant custom integration for SunSpec Modbus devices (solar inverters, energy meters). It is a **brownfield rewrite** of [cjne/ha-sunspec](https://github.com/cjne/ha-sunspec) under a new domain (`sunspec2`) so both can coexist in HACS. The project is in early-phase development; do not assume any feature exists unless you have read the code.

`REWRITE_PLAN.md` is the source of truth for scope, sequencing, and locked-in decisions. Read it before proposing structural changes — every refactor is scheduled to a specific phase, and the phase boundaries exist for `git bisect` reasons.

## Phase discipline (critical)

Work in this repo is intentionally sequenced into phases. The current state is **Phase 5 done** (v0.6.0 released 2026-04-07). Phase 6 (HACS release + CI) is the only remaining phase.

- **`pysunspec2` is now pinned to `1.3.3` with the `[serial]` extra.** The extra is load-bearing — `sunspec2.modbus.modbus` does an unconditional `import serial` at module top, but pysunspec2 declares pyserial only as an optional extra. Without `[serial]`, fresh HA installs that lack pyserial transitively will hard-fail at import. Latent upstream bug since at least 1.1.5; fixed in our manifest as part of Phase 1.
- **Do not change the `unique_id` format** until Phase 5 migration logic is in place. The format produced by `get_sunspec_unique_id` in `custom_components/sunspec2/__init__.py` must match upstream `cjne/ha-sunspec` exactly so that Phase 5 auto-migration of the entity registry preserves Recorder history.
- **Phase 0 = verbatim copy.** Only the package folder name, `DOMAIN`, `NAME`, `VERSION`, `ISSUE_URL`, and manifest fields were renamed from upstream. Internal typos like `uniqe_id`, `conifg`, `setttins`, `wheneve` are preserved on purpose; they get fixed in Phase 4 (and only in identifiers that are NOT part of the entity registry contract).
- **Model 103 entity-id rename caveat (Phase 1 finding).** `pysunspec2` 1.3.2 ships an updated SunSpec models repo where model 103's group `name` changed from `"inverter"` to `"inverter_three_phase"`. New devices added under our stack get `sensor.inverter_three_phase_*` instead of the legacy `sensor.inverter_*`. Phase 5 must decide whether the auto-migration aliases the new form back to the legacy form for users coming from `cjne/ha-sunspec`. See `REWRITE_PLAN.md` Phase 1 findings for details.
- **Phase 4 fixed the hot-reload bug** (sensors die after options-flow toggle). The actual root cause was NOT what Phase 2 / Phase 4 first thought: it was the cjne pattern of `await async_unload_entry(...); await async_setup_entry(...)` in `async_reload_entry`. HA 2026.x strictly requires `SETUP_IN_PROGRESS` state when `async_config_entry_first_refresh` is called, but the hand-rolled reload leaves the state in `LOADED`. The one-line fix is to dispatch via `hass.config_entries.async_reload(entry.entry_id)` which drives the state machine properly. The CLIENT_CACHE removal in commit `e508460` is also a real architectural improvement (instance-scoped client, no cross-instance state) but did NOT fix the user-visible symptom. Regression test in `tests/test_init.py:test_options_update_triggers_clean_reload` exercises the full update_listener path.
- **Phase 5 cjne migration: never break the unique_id format.** `migration.py` retargets cjne entities to our domain via `EntityRegistry.async_update_entity_platform`, the canonical HA API for cross-integration migration. It works ONLY because Phase 0 copied cjne's `get_sunspec_unique_id` verbatim and every subsequent phase guarded the format. If a future change touches the format, Phase 5 migration breaks for existing cjne users. Smoke-tested 2026-04-07: 21 sensors migrated atomically on a real KACO Powador with full Recorder history preserved. The companion `find_blocking_cjne_entries` guard refuses setup with `ConfigEntryNotReady` when cjne is still actively running for the same host/port/unit_id, preventing TCP-slot races on single-Modbus-slot inverters.
- **`api.close()` actually closes now.** For years, `SunSpecApiClient.close()` called `client.close()` which dispatched to a `pass` stub in pysunspec2. Phase 2 fixed this to call `cached.disconnect()` instead, closing the TCP socket via `ModbusClientTCP.disconnect`. Phase 4 then dropped `CLIENT_CACHE` entirely and made the client instance-scoped — `close()` now drops `self._client` to None so the next `get_client()` builds a fresh client. This is correct on its own merits (resource leak fix + no cross-instance state) but did NOT solve the hot-reload issue — that had a different cause, see the next bullet.
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
                        │              (instance-scoped self._client, one per coordinator)
                        │
                        └── SunSpecDataUpdateCoordinator (__init__.py)
                                ├── per-category error buffers + Repairs hooks (Phase 3)
                                └── sensor.py: SunSpecSensor / SunSpecEnergySensor
                                        └── SunSpecEntity (entity.py)

errors.py     ← typed exception hierarchy (Phase 3)
logger.py     ← context-bound LoggerAdapter (Phase 2)
diagnostics.py ← async_get_config_entry_diagnostics (Phase 2)
models.py     ← SunSpecModelWrapper (extracted in Phase 4)
translations/ ← Repairs panel issue keys (Phase 3)
```

Key data flow:

1. `SunSpecApiClient.modbus_connect` opens a TCP socket and runs `client.scan(...)` to discover SunSpec models. As of Phase 4 the resulting client lives on `self._client` (instance attribute, no class-level cache). `close()` calls `disconnect()` and sets `self._client = None`; the next `get_client()` builds a fresh one.
2. The coordinator's `_async_update_data` filters discovered model IDs against the user's `option_model_filter` and reads each enabled model into a `SunSpecModelWrapper`.
3. `SunSpecModelWrapper` lives in `models.py` (extracted from `api.py` in Phase 4) and walks the SunSpec model tree (`points`, `groups`, repeating groups indexed by integer suffix), exposing `getKeys()`, `getValue()`, `getMeta()`. The `key` format is either `pointName` or `groupName:index:pointName`.
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
