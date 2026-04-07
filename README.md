# ha-sunspec2

Home Assistant custom integration for SunSpec Modbus devices (solar inverters,
energy meters, etc.).

Debugging-first brownfield rewrite of [cjne/ha-sunspec](https://github.com/cjne/ha-sunspec),
based on the modern `pysunspec2` library (1.3.3+).

## Status

**Work in progress.** Do not install yet. See `REWRITE_PLAN.md` for the roadmap.

## Why a fork?

1. Upstream maintainer response times of 4 to 6 weeks made bugfixes painful.
2. `pysunspec2` has had several releases (cache fix, updated models repo, TLS
   support) since the version pinned upstream.
3. The architecture needs to be debugging-first, so users can produce useful
   bug reports without a developer walking them through it.

## Attribution

Based on [cjne/ha-sunspec](https://github.com/cjne/ha-sunspec), MIT-licensed.
Original copyright (c) 2021 cjne. See `LICENSE` for full text.

## Installation

Not yet released. HACS publication is tracked in Phase 6 of `REWRITE_PLAN.md`.
