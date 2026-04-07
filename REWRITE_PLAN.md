# ha-sunspec2 Rewrite Plan

## Background

This project is a brownfield rewrite of
[cjne/ha-sunspec](https://github.com/cjne/ha-sunspec). The upstream integration
is MIT-licensed and functional, but maintainer response times of 4 to 6 weeks
made iteration on bugfixes and diagnostics painful. This fork aims to deliver:

1. Modern `pysunspec2` (1.3.3 as of 2026-04-07, upstream is pinned to 1.1.5).
2. A debugging-first architecture, so users can file actionable bug reports.
3. Faster merge cadence under a new maintainer.

## Decisions locked in

| Topic          | Choice                          | Notes |
|----------------|---------------------------------|-------|
| Domain         | `sunspec2`                      | Must differ from upstream so both can coexist in HACS. |
| Approach       | Brownfield                      | Verbatim copy first, refactor module by module. |
| Migration      | (b) Auto-migration              | Rewrite entity registry on first setup, preserve history. Confirmed 2026-04-07. See Phase 5. |
| Repo name      | `ha-sunspec2`                   | Assumed `github.com/hilman2/ha-sunspec2`, confirm before Phase 6. |
| License        | MIT                             | Original copyright preserved. |
| Min HA version | 2024.6.0                        | Code already uses modern config_entries APIs. |
| pysunspec2     | 1.3.3                           | Bump happens in Phase 1 with smoke test. |

## Phase 0: Scaffold (done)

Deliverables:

- [x] Directory structure under `D:\Git\ha-sunspec2\` (standalone, moved out
      of the original repo to avoid git nesting).
- [x] Verbatim copy of all Python modules under `custom_components/sunspec2/`.
- [x] Rename applied only to: package folder, `DOMAIN` const, `NAME` const,
      `VERSION`, `ISSUE_URL`, manifest fields. Nothing else touched.
- [x] New `manifest.json` still pinning `pysunspec2==1.1.5` (the bump is Phase 1,
      on purpose, so it is a clean bisect boundary).
- [x] `LICENSE` with original cjne copyright preserved.
- [x] `README.md` with WIP notice and upstream attribution.
- [x] `hacs.json`.
- [x] `.gitignore`.
- [x] `REWRITE_PLAN.md` (this file).

Goal: the integration installs and runs identically to upstream, just under a
new domain. Zero behavioral changes. This is the commit on which everything
else builds.

The folder lives standalone at `D:\Git\ha-sunspec2\`, ready for `git init`
whenever Phase 1 starts.

## Phase 1: Dependency bump and smoke test (done 2026-04-07)

- [x] Bump `pysunspec2` to `1.3.3` in `manifest.json`.
- [x] Run upstream tests in WSL Ubuntu. cjne `tests/` were imported verbatim
      and retargeted from `custom_components.sunspec` to `custom_components.sunspec2`.
      Result: 31/31 passed under `pysunspec2 1.3.3`.
- [x] Fix any API break caused by the library upgrade. None found in the
      Python API. Two adjacent issues surfaced and were fixed:
- [x] Manual smoke test against a real inverter (KACO Powador 7.8 TL3,
      confirmed by user 2026-04-07: AC/DC power, voltages, currents,
      frequency, temperature, lifetime energy all plausible).
- [x] Tag `v0.2.0`.

Why separate from Phase 0: if Phase 1 breaks something, we know it was the
version bump, not the rename. Clean bisect boundary.

### Phase 1 findings (carry into later phases)

1. **`pysunspec2` declares `pyserial` only as an extras dependency** but
   imports it unconditionally at the top of `sunspec2.modbus.modbus`. Latent
   since at least `1.1.5` (verified against the 1.1.5 wheel). Worked in
   production only because containerised HA installs typically pull pyserial
   in transitively via other integrations. Fixed by depending on
   `pysunspec2[serial]==1.3.3` in `manifest.json`. One-character change but
   technically a scope-add to Phase 1; without it, Phase 1's fresh-venv smoke
   test could not import the library at all.

2. **`pysunspec2` 1.3.2 ships an updated SunSpec models repository.** Model
   103's group `name` changed from `"inverter"` to `"inverter_three_phase"`
   (label is unchanged: `"Inverter (Three Phase)"`). Our `sensor.py` builds
   entity ids from `gdef["name"]`, so under 1.3.3 a model-103 device gets
   `sensor.inverter_three_phase_*` instead of `sensor.inverter_*`.

   **Phase 5 followup required:** existing `cjne/ha-sunspec` users on 1.1.5
   have entity ids like `sensor.inverter_watts` in their HA registry. HA's
   entity registry migration changes `unique_id` but preserves `entity_id`,
   so their existing entities will keep working after the auto-migration.
   But any NEW model-103 device added under our 1.3.3 stack will get
   `sensor.inverter_three_phase_watts`. Inconsistency between old and new
   devices on the same install. Phase 5 must decide whether to alias the
   new generated names back to the legacy form, or accept the inconsistency
   and document it for migrating users.

3. **Sensor friendly-name UX bug** (unrelated to the bump but newly visible
   thanks to finding 2): `sensor.py:156` does `f"{name.capitalize()} {desc}"`
   where `name` comes from `gdef["name"]`. `str.capitalize()` only uppercases
   the first character, leaves underscores intact. Result: friendly name in
   the UI is `"Inverter_three_phase Watts"`. Cleanup belongs to Phase 4
   (`.title().replace("_", " ")` on the group name). Does not affect
   `entity_id` or `unique_id`, so safe to land in Phase 4 without touching
   the Phase 5 migration contract.

## Phase 2: Structured logging and Diagnostics platform (done 2026-04-07)

- [x] `SunSpecLoggerAdapter` in `logger.py` binds host, port, unit_id (and
      optional model_id) to every record. Wired into `SunSpecApiClient`,
      `SunSpecDataUpdateCoordinator` and `SunSpecSensor` (with a fallback
      to the module logger for the test stub coordinator). Module-level
      `_LOGGER` survives only where there is genuinely no instance context
      (config-flow pre-connection, async_setup, async_migrate_entry,
      async_unload_entry, the `progress` callback in `api.py`).
- [x] `async_get_config_entry_diagnostics` in `diagnostics.py`. Dumps
      redacted config (host masked via `async_redact_data`), redacted
      options, scanned model summary, latest values per key, recent
      errors deque, raw register captures, and HA / pysunspec2 / integration
      versions. Defensive try/except per model and per point so a single
      corrupt point cannot blow up the dump.
- [x] Generic error ring buffer (`deque(maxlen=20)`) on the coordinator.
      Phase 3 will refine this into per-category buffers.
- [x] Opt-in `capture_raw_registers` in the options flow. When enabled,
      `modbus_connect` wraps `client.read` so every modbus read also
      lands in `api._captured_reads` (capped at 1000 entries). Diagnostics
      dump surfaces them under `raw_captures`.
- [x] `api.close()` now actually closes the underlying TCP socket.
      Previously it called `client.close()` which dispatched to
      `SunSpecModbusClientDevice.close` - a `pass` stub in pysunspec2.
      Latent resource leak fixed; the cached client object stays in
      CLIENT_CACHE so the next read re-connects via pysunspec2's
      auto-reconnect path in `ModbusClientTCP.read`.

Goal achieved: any user bug report starts with "please download diagnostics
and attach it to the issue", and we can reproduce locally from that JSON
alone. Smoke-tested against a real KACO Powador 7.8 TL3 - the captured
hex bytes decoded to the expected SunSpec scan markers, model 103 length
fields, and the inverter's "KACO new energy" / "Powador 7.8 TL3" / serial
number ASCII strings.

### Phase 2 findings (carry into later phases)

1. **Hot reload after options-flow toggle leaves sensors unavailable.**
   Symptom: toggling any option (capture_raw_registers, scan_interval,
   anything) at runtime triggers an entry reload via the update_listener,
   the new coordinator builds a fresh `SunSpecApiClient`, but the new
   client cannot get the inverter to respond. Sensors go to "unavailable"
   until the user restarts HA entirely. Verified against a real KACO
   Powador 7.8 TL3.

   Root cause is somewhere in the unload+setup interaction with the
   class-level `CLIENT_CACHE` and the inverter's single-Modbus-TCP-slot
   behaviour, but the exact failure is not visible without instrumented
   logs from a live restart cycle on the affected box. Two failed fix
   attempts (commits `6f4bb72` reverting cache invalidation, and `a2dffc7`
   making `close()` actually disconnect) did not solve it.

   **Workaround:** restart HA after toggling any sunspec2 option.

   **Phase 4 followup required:** when `api.py` is refactored (model.py
   extraction, type hints, typo fixes), also rebuild the connection
   lifecycle. Likely correct shape: instance-scoped client (drop
   CLIENT_CACHE entirely, one TCP socket per coordinator), explicit
   `async_will_remove_from_hass` cleanup, and a real reload integration
   test against a mock that imitates the single-connection-slot constraint.

2. **`SunSpecModbusClientDevice.close()` is a `pass` stub in pysunspec2.**
   Inherited unchanged by `SunSpecModbusClientDeviceTCP`. cjne/ha-sunspec
   has been calling `api.close()` for years thinking it closed the TCP
   socket. It did not. Phase 2 fixes the resource leak by routing
   `api.close()` to `cached.disconnect()` instead. Wrap is defensive
   (try/except, no raises). The fix did not solve finding 1 above
   - that has a separate cause - but it does prevent the socket from
   leaking across update cycles, which is correct on its own merits.

3. **Capture toggle UI quirk.** The `capture_raw_registers` checkbox is
   on the SECOND step of the options flow (`async_step_model_options`),
   not the first (`async_step_host_options`). Users have to submit the
   host page unchanged to reach the toggle. Phase 4 may want to either
   move this to the first step, or split capture out into its own
   options-flow step with a clearer label. Not a bug, just UX.

## Phase 3: Coordinator error classification (done 2026-04-07)

- [x] Typed exception hierarchy in `errors.py` (`SunSpecError` base +
      `TransportError`, `ProtocolError`, `DeviceError`, `TransientError`).
      `CATEGORIES` tuple as single source of truth.
- [x] `api.py` raises typed errors at the boundary:
      - `ModbusClientError` → `TransportError`
      - `SunSpecModbusClientError` → `ProtocolError`
      - `SunSpecModbusClientTimeout` → `TransientError`
      - `SunSpecModbusClientException` → `DeviceError`
      - `not is_connected()` / `not check_port()` → `TransportError`
- [x] Old `ConnectionError` / `ConnectionTimeoutError` (which shadowed
      Python builtins, cjne legacy) **deleted**. All 7 callers in
      `config_flow.py` and tests updated atomically.
- [x] `SunSpecDataUpdateCoordinator` now keeps `_recent_errors` as
      `dict[str, deque[dict]]` (one deque per category, maxlen=20) plus
      `_consecutive_failures: dict[str, int]` per category.
- [x] `_record_error` helper appends to the matching deque, bumps the
      counter, and triggers the Repairs hook if the threshold is crossed.
- [x] Repairs panel integration via `homeassistant.helpers.issue_registry`:
      - protocol errors fire on the **first occurrence** (config / hardware
        compat problem, not a transient state)
      - transport / device errors fire after **3 consecutive failures**
        (filters out brief power glitches and short network blips)
      - transient errors **never** escalate, only land in the buffer
- [x] `_clear_repair_issues` is called on every successful update cycle
      (so a recovered inverter drops out of the Repairs panel
      automatically) and on `async_unload_entry` (so removing the
      integration leaves no ghost issues).
- [x] `translations/en.json` and `translations/de.json` bootstrapped
      with three issue keys (`transport_error`, `protocol_error`,
      `device_error`) - title plus actionable description, with host /
      port / unit_id / error placeholders.
- [x] Diagnostics dump shape evolved: `recent_errors` is now a dict
      with 4 category keys, plus a new `consecutive_failures` top-level
      field showing how close each category is to its Repairs threshold.

`_async_update_data` last-resort safety net catches any unclassified
`Exception`, wraps it as `TransportError` and logs the full traceback,
so we know to add an explicit category if the case recurs.

Tests: 59/59 passing. The Phase-3 specific tests in `tests/test_errors.py`
exercise the per-category routing, the consecutive-failure counter, the
threshold-based repair issue creation for each category, the
"transient never escalates" rule, and the clear-on-unload behaviour.

## Phase 4: Sensor platform cleanup (done 2026-04-07)

- [x] Internal typo fixes: `_uniqe_id` → `_unique_id` (private storage),
      `wheneve` → `whenever`, `retreiving` → `retrieving`, `conifg` →
      `config`, `setttins` → `settings`. None touched the public
      `unique_id` property contract — Phase 5 migration is unaffected.
- [x] Friendly-name UX fix (Phase-1 followup): `name.capitalize()` →
      `name.replace('_', ' ').title()`. `Inverter_three_phase Watts` →
      `Inverter Three Phase Watts`. `entity_id` slug unchanged.
- [x] `SunSpecModelWrapper` extracted from `api.py` into `models.py`
      (1:1 move, zero behavioural change).
- [x] Type hints sweep across `api.py`, `__init__.py`, `sensor.py`,
      `entity.py`, `models.py` — public methods + critical signatures.
      ~40 annotations.
- [x] **Hot-reload fix (Phase-2 followup)**: the cjne pattern of
      `await async_unload_entry(...); await async_setup_entry(...)` in
      `async_reload_entry` was the root cause of the "sensors die after
      toggle" bug. HA 2026.x strictly requires the entry state to be
      `SETUP_IN_PROGRESS` when `async_config_entry_first_refresh()` is
      called, but the hand-rolled reload leaves the state in `LOADED`.
      Fix: dispatch to `hass.config_entries.async_reload(entry.entry_id)`
      which drives the state machine properly. One-line fix in
      `async_reload_entry`.
- [x] **CLIENT_CACHE refactor**: dropped the class-level `CLIENT_CACHE`
      in `SunSpecApiClient`, switched to instance-scoped `self._client`.
      Each api instance owns exactly one pysunspec2 client. The options
      flow no longer probes via a side-effect-laden `get_client(config=...)`
      path; it uses the new `known_models()` helper which reads the
      already-discovered model list off the live coordinator client
      without forcing a fresh TCP connect. This was originally pursued
      as the hot-reload fix (and the architectural cleanup is real and
      worth keeping), but it was NOT what the user-visible symptom was
      blocked on — see the previous bullet for the actual fix.
- [x] pytest coverage for `SunSpecModelWrapper` in `tests/test_models.py`:
      14 tests covering `getKeys`, `getValue` (basic / scale-factor /
      enum16 / bitfield32 / repeating-group), `getMeta`, `getGroupMeta`,
      `getPoint` (top-level + repeating group navigation).
- [x] Regression test for the hot-reload path in `tests/test_init.py`:
      `test_options_update_triggers_clean_reload` exercises the
      update_listener via `async_update_entry` (the same code path the
      user hits when saving the options form), unlike the existing
      `test_setup_unload_and_reload_entry` which calls
      `hass.config_entries.async_reload` directly and therefore never
      reproduced the bug.

Tests: 76/76 passing (was 59 before Phase 4: +14 model tests, +2 known_models
tests, +1 hot-reload regression test).

Smoke-tested against the user's KACO Powador 7.8 TL3 on commit `a675535`:
toggling `capture_raw_registers` in the options flow no longer kills
the sensors. Diagnostics dump after the toggle showed `raw_captures`
populated with real KACO bytes (SunS marker, model 103 length fields,
KACO ASCII strings) — proving the post-reload `SunSpecApiClient`
instance is fresh, the wrap is in place, and the previous "sensors die
after toggle" symptom is gone.

### Phase 4 findings (carry into later phases)

1. **Hot-reload root-cause discipline.** Three Phase-2 / Phase-4 attempts
   theorised about the wrong layer (`CLIENT_CACHE` invalidation, route
   `close()` to `disconnect()`, instance-scoped client) before the
   fourth attempt actually fixed it via `hass.config_entries.async_reload`.
   The mistake was theorising before reading the live HA log. The
   user-provided traceback in the fourth attempt pinpointed the real
   bug in seconds. **Lesson for future phases**: when a bug survives
   one fix attempt, demand the actual logs before proposing fix #2.

2. **`SunSpecApiClient` is now instance-scoped**, but the rest of
   `api.py` (`modbus_connect`, `check_port`, `read_model`, `TIMEOUT=120`,
   the `time.sleep(0.6)` between model reads) is still a near-verbatim
   carry from cjne. These are functional but smell like premature
   defensive code. **Phase 6 or later** should look at simplifying once
   real-user telemetry is in.

3. **`tests/test_init.py:test_options_update_triggers_clean_reload`**
   is the new canonical regression test for any future change that
   touches the entry lifecycle. Do not delete it.

## Phase 5: Migration helper from upstream `sunspec` domain (done 2026-04-07)

- [x] `migration.py` mit `migrate_from_cjne_sync()`: findet cjne config
      entries die unsere host/port/unit_id matchen, walks die entity
      registry, retargetet entities via `EntityRegistry.async_update_entity_platform`.
      Atomarer platform/config_entry_id/unique_id rewrite, entity_id und
      Recorder-History bleiben erhalten.
- [x] `find_blocking_cjne_entries()` Conflict Guard: refused setup mit
      `ConfigEntryNotReady` wenn cjne aktuell `ConfigEntryState.LOADED`
      für dieselbe host/port/unit_id ist. Verhindert TCP-Race auf
      Single-Slot-Invertern wie KACO Powador.
- [x] Repairs Panel: `cjne_conflict` issue (translation_key, severity=ERROR,
      is_fixable=False) mit voller 3-Schritte-Anleitung. Auto-cleared
      sobald cjne weg ist und nächster setup retry erfolgreich ist.
- [x] Persistent notifications: "SunSpec migration complete" auf success
      mit Sensor-Zähler, "SunSpec migration blocked" auf partial-blocked
      (defense in depth — sollte unter dem Conflict Guard nicht mehr
      auftreten).
- [x] `translations/en.json` und `de.json` erweitert um den
      `cjne_conflict` issue key.
- [x] 12 unit tests in `tests/test_migration.py` (8 für migration logic +
      4 für conflict guard) und 3 integration tests in `tests/test_init.py`
      (migration runs, blocked when cjne loaded, conflict issue cleared
      after resolution).

Smoke-Test bestätigt vom User 2026-04-07 auf KACO Powador 7.8 TL3:

  1. cjne installiert mit Recorder-History → sunspec2 setup → Conflict
     Guard greift, Repairs panel zeigt "cjne/ha-sunspec ist noch aktiv"
     mit voller deutscher Anleitung, sunspec2 in "Setup retry" Status
  2. cjne via HACS deinstalliert → HA Restart
  3. sunspec2 retried automatisch → migration findet die orphans →
     persistent notification: "SunSpec migration complete - 21 sensor(s)
     were migrated from cjne/ha-sunspec to sunspec2. Their entity IDs
     and Recorder history have been preserved"
  4. Daten kommen unter den ALTEN entity_ids weiter rein → user keeps
     all 21 sensors, all history, all dashboard / automation references

Phase 5 USP achieved.

### Phase 5 findings

1. **Phase 4's `unique_id`-Format-Disziplin hat sich ausgezahlt.** Weil
   wir das cjne format seit Phase 0 verbatim gehalten haben, war die
   eigentliche Migration ein 4-Zeilen-`async_update_entity_platform`-Call.
   Hätten wir das format in Phase 4 "verschönert", wäre Phase 5 ein
   substantieller Eingriff in den Entity-Renderer geworden.

2. **`async_update_entity_platform` ist die canonical HA-API** für
   cross-integration migration. Der erste Explore-Agent hat sie
   übersehen — ich musste den HA source direkt lesen um sie zu finden.
   Lesson: bei kritischen API-fragen direkt den source lesen, nicht
   nur den Doc-Ausgang vom Agent vertrauen.

3. **`STATE_UNKNOWN`-Constraint** auf `async_update_entity_platform`
   ist real und sinnvoll. Hat uns indirekt zum Conflict Guard gezwungen,
   was die bessere UX war (sauberer Block + Auto-Retry vs. Daten-
   Korruption durch parallele integrations).

## Phase 6: HACS release

- Create `github.com/hilman2/ha-sunspec2` as a standalone repo (not a fork of
  `cjne/ha-sunspec`, so HACS treats it as a distinct integration).
- CI workflows: `hassfest`, `ruff`, `mypy --strict`, `pytest`.
- Release drafter, Dependabot for `pysunspec2` and GitHub Actions.
- Submit to HACS default repo.
- Tag `v1.0.0` after a second user has tested the migration helper on their
  own deployment.

## Debugging-first principles

These shape every design decision in Phases 2 through 5:

1. **Every error has a category, a context, and an actionable message.** No
   `except Exception: log.warning(e)` survives anywhere in the codebase.
2. **Diagnostics first, code second.** If I cannot dump the state of the
   integration into a JSON file with one click, I cannot ask a user for
   useful bug reports.
3. **Fixture capture for free.** Any real-world bug should produce a pytest
   fixture in minutes, not days.
4. **Repairs panel over log-spam.** If a condition requires user action, it
   belongs in the Repairs panel, not in a `_LOGGER.warning`.
5. **Preserve user history.** Auto-migrate on upgrade, never force users to
   re-add sensors and lose their Energy dashboard.

## Open questions needing user confirmation

1. CI workflows in Phase 1 or defer to Phase 6? Leaning defer.
2. Min HA version `2024.6.0` acceptable, or bump newer?

Confirmed:
- GitHub repo: `hilman2/ha-sunspec2` (confirmed 2026-04-07).
- Working folder: `D:\Git\ha-sunspec2\` (standalone, confirmed 2026-04-07).
- Migration strategy: (b) auto-migration (confirmed 2026-04-07). Phase 4 must
  preserve `unique_id` format. Phase 5 implements the entity registry rewrite.
