# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## ⚠️ **READ THIS FIRST — ALWAYS `git pull` BEFORE TOUCHING ANYTHING** ⚠️

**Before you create a branch, before you read code to plan a change, before
anything**: run `git checkout main && git pull --ff-only`. The repo accepts
PRs from the community and from Dependabot, and `main` may have moved since
the last session. Skipping this step means:

- branching from a stale base, which the protected branch will refuse to
  merge (`strict: true` requires the PR head to be up to date),
- reading outdated code while planning a fix and recommending things that
  no longer apply,
- silently re-introducing a bug that someone else already fixed upstream.

There is **no exception** to this rule. Even for "I'm just looking" tasks,
pull first so the code you read is the code that's live. Cost: 200 ms.
Cost of skipping: a wasted PR that has to be rebased or rewritten.

```bash
git checkout main
git pull --ff-only
git checkout -b feat/whatever  # only after the pull
```

If `git pull --ff-only` refuses (local main has diverged), stop and
investigate before doing anything destructive — never `--force`, never
`reset --hard` without understanding why local diverges from origin.

---

## What this is

Home Assistant custom integration for SunSpec Modbus devices (solar
inverters, meters, batteries). Standalone brownfield rewrite of
`cjne/ha-sunspec`. UI-only setup via config flow, no YAML.

## Test suite

Pytest does **not** run on Windows-Python (HA imports `fcntl`). It runs
in Docker, which is also the only environment that reproduces the gate:
CI installs with pip in two steps (`pytest<9` is forced after
`pytest-homeassistant-custom-component` pulls 9.0.x), and `uv` cannot
express that, it calls the requirement unsatisfiable and stops.

```bash
# Full suite, about 30 seconds sequential (was 3.5 minutes until the
# read_model pacing pause was switched off for tests, see conftest.py)
docker compose -f tests/docker/compose.yml run --rm tests

# Single file / single test: everything after the service name goes to pytest
docker compose -f tests/docker/compose.yml run --rm tests pytest tests/test_init.py -q

# Parallel, and live output when something hangs
docker compose -f tests/docker/compose.yml run --rm tests-fast
docker compose -f tests/docker/compose.yml run --rm tests-verbose
```

`tests/docker/README.md` has the rest: rebuilding after a dependency
change, and why the source is mounted read-only under a non-root user.

The dependency set is in `tests/docker/Dockerfile` and mirrors the
pytest job in `.github/workflows/ci.yml`. **Change one, change the
other.** `voluptuous-serialize` is named explicitly there because
`tests/test_config_flow.py` imports it and HA stopped depending on it
in 2026.9.

**The Home Assistant version is nowhere written down.** Image and
workflows both call `.github/scripts/resolve_ha_versions.py`, which
asks PyPI for the newest `pytest-homeassistant-custom-component` that
pins a final HA release. Any static constraint here is a choice of test
target that goes stale on its own, and did: the gate spent six months
on HA 2026.2.3. Docker caches the resolve layer, so a local rebuild
that should pick up a newer HA needs `--no-cache`.

`.github/workflows/ha-beta.yml` runs the same suite against the next HA
beta every Monday, opens an issue labelled `ha-beta` when it fails, and
closes it when the run goes green again. It is not a required check.
Reproduce it locally with the `tests-beta` compose service.

`.github/workflows/models-sync.yml` fetches `sunspec/models` on the 1st
of every second month and opens a `chore/models-<date>` PR when the
embedded JSON definitions differ. `.github/scripts/sync_models.py` does
the work and runs locally against any clone of that repo. The commit
the definitions match is in `pysunspec2/models/UPSTREAM`.

Python is 3.14. Home Assistant has required 3.14.2 since 2026.3.0, so a
3.13 environment silently resolves to HA 2026.2.3 and tests the
integration against a release nobody runs.

Lint: `ruff check custom_components/ tests/` and `ruff format --check
...`. Config in `pyproject.toml` (line length 100 soft, py313 target,
which is one release behind the runtime on purpose; the reason is at the
setting).

### Type checking

`custom_components/` runs under `mypy --strict`, `tests/` under the same
config with signatures and a few test idioms exempted. Both are in
`[tool.mypy]` in `pyproject.toml`, including the reasoning for the split
and for every per-module exception. `mypy` with no arguments checks the
right paths.

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/d/Git/ha-sunspec2 && uv run \
  --with mypy --with homeassistant \
  --with 'modbus-connection[tmodbus]' \
  --with voluptuous --with pytest --with pytest-homeassistant-custom-component \
  --with pytest-mock mypy"
```

Install the same dependency set the CI job installs. `warn_unused_ignores`
is on, so a missing package changes the verdict rather than skipping a
check: a run without Home Assistant's type information reports the
`type: ignore` comments as unnecessary.

Two suppressions are deliberate and carry their reason at the line:
`entity.py` builds a three-element device identifier where HA's
`DeviceInfo` types a two-element one, and `test_init.py` asserts an entry
state that mypy narrowed away across an `async_reload`.

### CI gate (must pass before merge)

Every push to `main` runs five jobs in `.github/workflows/ci.yml`:
**Lint (ruff)**, **mypy (strict)**, **Hassfest** (HA manifest
validation), **HACS validation**, and **pytest**. **`main` is
branch-protected**: all five jobs are required status checks, and
`enforce_admins` is on - no one can bypass
the gate, not even the repo owner. Direct push to `main` is therefore
not possible; every change goes through a PR and the merge button only
unlocks once CI is green.

Workflow is:

```bash
git checkout -b feat/whatever
# ...edit...
git commit
git push -u origin feat/whatever
gh pr create --fill
gh pr merge --auto --squash --delete-branch   # merges itself once the five checks are green
```

`--auto` is for our own PRs only. GitHub lets only accounts with write
access arm it, so an external contributor's PR still needs a human
merge; never arm it on someone else's PR.

A red CI is loud and the user notices fast, so always run the test
suite, ruff **and** mypy locally before pushing the branch:

```bash
# Run from WSL Ubuntu
uv run --with ruff ruff check custom_components/ tests/
uv run --with ruff ruff format --check custom_components/ tests/
```

The mypy invocation is in "Type checking" above.

If `ruff format --check` reports drift, run `ruff format
custom_components/ tests/` to fix it. The classic trap is `SIM117` for
nested `with` statements - combine them into a single tuple-style
`with (a, b):` block instead of relying on the local pytest run alone.

### Releases

Tagging is gated by `.github/workflows/release.yml`: a tag push (`vX.Y.Z`)
triggers a job that **re-verifies** the CI workflow on the same commit
is green and only then publishes the GitHub release (or unpublishes the
release-drafter draft if one already exists for that tag). It is
therefore impossible to ship a release that doesn't have a green CI on
its tagged commit.

The release process is:

1. Bump `VERSION` in `custom_components/sunspec2/const.py` **and** in
   `custom_components/sunspec2/manifest.json` (must match - hassfest
   fails otherwise) on a feature branch.
2. PR + merge to `main` through the protected gate.
3. `git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z`
4. The release workflow takes it from there. If you ever see the
   release stuck waiting on CI, check `gh run list --workflow Release`.

**Versions follow Home Assistant's calendar scheme** since September
2026: `2026.9.0` is the first release of the month, `2026.9.1` a fix
to it, `2026.10.0` the next month's. Tags keep the `v` prefix:
`v2026.9.0`. The old `0.x.y` numbers in commit subjects and code
comments are history, not a pattern to continue.

A tag with a suffix, `v2026.9.0b1` (or `rc1`), publishes a
**pre-release** instead: not latest, so HACS offers it only to users
who ticked "Show beta versions". That is how a build reaches the
owner's own inverter before everyone else's. The three-number part
must equal `manifest.json`, the workflow refuses otherwise. Tags,
pre-release or final, only on the owner's say-so.

release-drafter keeps one draft named "Next release" (tag `next`); it
cannot compute a calendar version. The release workflow publishes that
draft under the pushed final tag and appends the compare link. A
pre-release tag leaves the draft alone.

### Test fixture caveat (resolved in v0.20.0)

`tests/test_data/inverter.json` model 103 has `Evt1` set to 3, so the
bitfield32 sensor renders as `"GROUND_FAULT,DC_OVER_VOLT"`. That used
to fail HA's strict ENUM validation in `async_write_ha_state`: the
combined string is not one of the `options`, the error was swallowed by
`entity_platform` during setup, and a later `coordinator.async_refresh()`
let the ValueError escape. Tests driving a refresh therefore had to use
`MOCK_CONFIG_PREFIX` (model 160 only).

**Fixed**: bitfields no longer carry the ENUM device class, because a
bitfield is a set of flags rather than one of N states (enumerating the
combinations would be 2**32 for `Evt1`). `MOCK_CONFIG` is safe in
refresh tests now, and `tests/test_sensor.py` has one that would have
caught the original bug. Same root cause as cjne/ha-sunspec#370.

## Architecture

The full read pipeline lives under `custom_components/sunspec2/`:

- **`pysunspec2/`**: the SunSpec client library itself, embedded as a
  fork of `sunspec/pysunspec2` v1.3.6 since v0.30.0 (Apache 2.0, origin
  and every change recorded in its `__init__.py`). The model JSON files
  live under `pysunspec2/models/json/`. Excluded from ruff and mypy, so
  what our code imports from it is `Any`. Its upstream unit tests sit in
  `tests/pysunspec2/`. Since the `feat/modbus-connection` branch the
  integration polls through `modbus/unit_device.py`, a device over a
  `modbus-connection` unit (Home Assistant's asyncio Modbus transport,
  the manifest's only requirement); points, groups, models and devices
  have `async_read`/`async_write`/`async_scan` in place of upstream's
  sync methods. Upstream's socket and serial clients are deleted, and
  so are their tests and `pyserial`. Transport fixes go into
  `unit_device.py` or upstream into modbus-connection, not into
  workarounds in `api.py`.
- **`vendors/`**: what a manufacturer does beyond the SunSpec text.
  `profile.py` is the shape (`VendorProfile`, `StorageModeProfile`,
  the `StorageMode` and `Rate` vocabulary, watt/percent conversion),
  one module per vendor holds the knowledge with its sources. The
  coordinator picks the profile from `Mn` of model 1 (`coordinator.vendor`).
  `storage_modes.py` turns a storage profile into the *Battery mode*
  Select and four `RestoreNumber` watt setpoints (kept in
  `coordinator.storage_setpoints`); rates are written first in one
  frame, the mode last. Fronius is the first profile (2026.9.2); its
  sources and quirks are in `vendors/fronius.py` and `docs/fronius.md`.
- **`api.py`** (`SunSpecApiClient`): instance-scoped, async end to end
  since the `feat/modbus-connection` branch (no executor in the read or
  write path). Owns one `ModbusConnection` and one client at a time
  and, since v0.22.0, keeps them: `async_get_client()` builds the
  client only when `self._client` is None, so a single session is
  reused across cycles; `async_close()` drops the link, `async_shutdown()`
  the connection. Caches the scanned model
  layout (base address plus id/addr/len per block) and re-reads both
  ends of the chain on reconnect, so a reconnect rebuilds the model
  objects from the cache instead of re-scanning; the coordinator
  persists that layout across restarts (`STRUCTURE_STORAGE_KEY`).
  `reconnect_next()` flags the next `get_client()` to disconnect and
  rebuild, and drops the cached layout with it. It runs only on failure
  paths, never on a timer: there is no periodic rescan. Translates
  pysunspec2 exceptions to the typed `errors.py` hierarchy at the
  boundary.
- **`__init__.py`** (`SunSpecDataUpdateCoordinator`): the polling brain.
  Holds a per-`(host, port)` class-level `asyncio.Lock` because KACO
  Powador and SolarEdge gateways only allow one Modbus TCP slot at a
  time. The read cycle runs under that lock; the lock is
  released across the in-cycle retry sleep so other coordinators on the
  same gateway can poll. Since v0.22.0 the cycle does not connect and
  close per poll: one session is held open (measured on a KACO Powador
  7.8 TL3 at a 30 s interval, reconnecting per poll failed 5 of 6
  cycles, one held session served 20 of 20 at a steady 1.6 s). It is
  handed back only where `release_slot_between_polls` says so (the
  `CONF_RELEASE_SLOT` option, or a second config entry on the same
  host/port), and torn down with `close(force=True)` by
  `_after_failed_cycle` on the failure path.
- **`models.py`** (`SunSpecModelWrapper`): facade over a list of
  pysunspec2 model instances. Flattens repeating-group points into a
  `group:idx:point` key namespace.
- **`sensor.py`**: builds one `SunSpecSensor` (or `SunSpecEnergySensor`,
  a `RestoreSensor`) per valid point, mapping units to HA device classes
  via `HA_META`. Plausibility filters live here.
- **`entity.py`** (`SunSpecEntity`): base class with the `available`
  override that powers the stale-data tolerance window. Reads
  `coordinator.consecutive_failed_cycles`, **not** `last_update_success`,
  see "Resilience contract" below.
- **`errors.py`**: four-category exception hierarchy
  (`transport / protocol / device / transient`). Drives Repairs panel
  thresholds: protocol fires at 1 consecutive, transport+device at 3,
  transient never escalates.
- **`migration.py`**: one-shot retarget of orphan `cjne/ha-sunspec`
  entities to our domain so users keep their entity ids and Recorder
  history. Hard-blocks setup with `ConfigEntryNotReady` if cjne is
  still actively loaded for the same `host/port/unit_id`.
- **`diagnostics.py`**: JSON dump exposed via HA's diagnostics API,
  redacts host. Pulls from coordinator buffers; the optional raw register
  capture lives on `api._captured_reads`.
- **`config_flow.py`**: full UI flow plus options flow. The options
  flow model list comes from the coordinator's `detected_models` cache,
  falling back to `api.known_models()`; neither opens a fresh TCP
  connection. Forcing a reconnect there would race the coordinator on
  single-slot inverters.

### Resilience contract

Two coupled mechanisms make the integration usable on flaky inverter
networks:

1. **In-cycle retry**: `_async_update_data` runs `_run_one_update_cycle`
   under the gateway lock; on failure it releases the lock, sleeps
   `INTERVAL_RETRY_DELAY_SECONDS` (5s), and retries once. The very
   first refresh during setup deliberately skips the retry so a
   misconfigured inverter still fails fast through `ConfigEntryNotReady`.
2. **Stale-data tolerance**: while `consecutive_failed_cycles <=
   STALE_DATA_TOLERANCE_CYCLES` (5), `SunSpecEntity.available` returns
   True so sensors keep reporting the last good value.
   `_after_failed_cycle` manually invokes `async_update_listeners()`
   when the counter crosses the threshold, because HA's
   `DataUpdateCoordinator._async_refresh` early-returns on consecutive
   failures and would otherwise leave the entity frozen on the stale
   value forever.

If you ever change `available` to consult `last_update_success`, the
manual listener call will fire while HA still has it set to True and
the unavailable transition will silently break.

## Releases

Bump `VERSION` in **both** `custom_components/sunspec2/const.py` and
`custom_components/sunspec2/manifest.json`. They must match or hassfest
fails. Tag with `git tag -a vX.Y.Z -m "..."`.

## Conventions

- **No em-dashes** anywhere in code, comments, commit messages, or docs.
- **No `Co-Authored-By` trailer** on commits. They go out clean under
  `hilman2`.
- Commits referencing previous incidents in the message body should
  explain *why* the fix is shaped the way it is, not just *what* it
  changes. The history reads like a runbook.

## Issue and PR communication

Comments on issues and PRs, and issue bodies themselves, are for the person
reading them, not for the archive.

- **Any concrete question to the reporter goes at the very top**, right under
  the TL;DR. Never buried mid-post, never at the end.
- **Open with a TL;DR** as soon as the post runs longer than a few lines.
- **Default to short and low on jargon.** Most people in this tracker are HA
  users with an inverter, not contributors to this repo.
- Deep technical derivation (byte-level diffs of pysunspec2, exception
  hierarchies, why an alternative fix was rejected) belongs in the commit
  message or a code comment, where it reads like a runbook. It does not belong
  in the thread.
- If background still has to be there, put it below a `---` separator as an
  optional section.

Sanity check before posting: is the thing you want the reader to do visible in
the first five lines?

## Memory and persisted state

Live test environment notes, repo conventions, and per-user preferences
sit in `~/.claude/projects/D--Git-ha-sunspec2/memory/` and override
this file when they conflict.
