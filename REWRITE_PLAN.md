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

## Phase 3: Coordinator error classification

Current code catches bare `Exception` in the coordinator and logs
`_LOGGER.warning(exception)`. Worst case for debugging: no stack, no category,
no actionable message.

- Split errors into:
  - `TransportError`: TCP socket, Modbus framing, connection refused.
  - `ProtocolError`: SunSpec base-address not found, unknown model, scan
    returned junk.
  - `DeviceError`: device responded, value is implausible (out of range,
    wrong type).
  - `TransientError`: one-shot timeouts that should retry with backoff.
- Emit `ir.async_create_issue` for persistent `DeviceError` and `ProtocolError`
  so they show up in the Repairs panel with an actionable fix suggestion.
- Store last 20 errors per category in the coordinator for the diagnostics
  dump.

## Phase 4: Sensor platform cleanup

- Fix upstream typos where they live in internal identifiers only (`uniqe_id`,
  `conifg`, `setttins`, `wheneve`). Do NOT touch the `unique_id` format used
  for the entity registry, because Phase 5 auto-migration depends on it.
- Add type hints throughout.
- Extract `SunSpecModelWrapper` from `api.py` into its own module
  (`models.py`).
- pytest coverage for `getKeys`, `getValue`, scale-factor handling, enum and
  bitfield decoding.

## Phase 5: Migration helper from upstream `sunspec` domain

Goal: a user with `cjne/ha-sunspec` installed can install our integration and
all existing entities get retargeted to the new domain automatically,
preserving Recorder history.

Technique:
1. On `async_setup_entry`, check the entity registry for entities with
   `platform == "sunspec"` matching the same host, port, unit_id.
2. Rewrite each entity's platform to `sunspec2` and its unique_id prefix.
3. Log one summary line per migrated entity.
4. Show a persistent notification with a link to revert.

Risk: this only works if our `unique_id` format matches upstream's exactly.
Phase 4 must NOT change the unique_id format. A user who refuses
auto-migration can install both side by side (different domains, no conflict).

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
