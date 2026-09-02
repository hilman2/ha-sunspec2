# Battery and export control

The full version of the section in the
[README](../README.md#battery-and-export-control-beta). Read that one
first if you only want to know what the feature is.

> **This can bite.** Writing to an inverter changes its configuration.
> Some devices persist that through a reboot, some refuse to hand
> control back without a physical reset, and what a given register
> does is not always what the SunSpec specification says it does.
> Grid feed-in limits may also be regulated where you live.

The controls are off until you tick **"Enable experimental write
controls (BETA)"** in the options. No write entity exists before that.

## The entities

Which ones appear depends on what your inverter offers. An inverter
without a battery gets no battery controls, and the export limit comes
from whichever of the two supported blocks the device has.

| Entity | Type | SunSpec point | What it does |
|---|---|---|---|
| Export limit | Number, 0 to 200 % | 123 `WMaxLimPct` / 704 `WMaxLimPct` | Caps AC output to N % of nameplate. 0 for no export. Above 100 is allowed because some firmware uses 110 to mean "no limit". A Fronius needs the switch off and on for a new value, see [fronius.md](fronius.md) |
| Export limit enabled | Switch | 123 `WMaxLim_Ena` / 704 `WMaxLimPctEna` | The limit only applies while this is on |
| Export limit revert time | Number, seconds | 123 `WMaxLimPct_RvrtTms` / 704 `WMaxLimPctRvrtTms` | How long the inverter honours the limit before reverting on its own. See below |
| Export limit revert value | Number, 0 to 200 % | 704 `WMaxLimPctRvrt` | The percentage it falls back to when the timer expires. Disabled by default |
| Export limit stays on after revert | Switch | 704 `WMaxLimPctEnaRvrt` | Whether the limit stays enabled once the timer expires. Disabled by default |
| Active power setpoint | Number, watts | 704 `WSet` | The limit in watts rather than percent. Usually what a zero-export control loop wants, since percent means the automation has to know the nameplate |
| Active power setpoint enabled | Switch | 704 `WSetEna` | |
| Active power setpoint mode | Select | 704 `WSetMod` | Whether watts or percent is in force. Disabled by default |
| Battery charge rate | Number, 0 to 100 % | 124 `InWRte` | Charge power as a percentage of the maximum |
| Battery discharge rate | Number, 0 to 100 % | 124 `OutWRte` | Discharge power as a percentage of the maximum |
| Battery max charge power | Number, watts | 124 `WChaMax` | The ceiling those two percentages refer to |
| Battery control mode | Select | 124 `StorCtl_Mod` | Off, charge only, discharge only, both |
| Battery rate revert time | Number, seconds | 124 `InOutWRte_RvrtTms` | Same lapse behaviour as the export limit's revert time |
| Battery minimum reserve | Number, 0 to 100 % | 124 `MinRsvPct` | Charge to hold back, for example for backup power. Disabled by default |
| Power factor setpoint | Number, -1 to 1 | 123 `OutPFSet` | Cos-phi setpoint for reactive power |
| Power factor enabled | Switch | 123 `OutPFSet_Ena` | The setpoint only applies while this is on |
| Inverter grid connection | Switch | 123 `Conn` | **Most dangerous.** Off disconnects the inverter from the grid entirely |

The battery rates are percentages of `WChaMax`, not watts. An
automation that wants 4200 W has to compute `4200 / WChaMax * 100`, and
should read `WChaMax` from its own entity rather than hardcode it.
On a Fronius the integration does that for you: see
[docs/fronius.md](fronius.md) for the *Battery mode* menu and the watt
fields that replace the two percent rates there.

There is also a **`sunspec2.set_export_limit`** service action taking
`config_entry_id`, `percent` and an optional `enable`, so an automation
can set the limit without going through the Number entity.

**Where a device has both block 123 and block 704**, the controls come
from 704 and 123 stays read-only. 704 is the modern equivalent, it
accepts an absolute watt setpoint, and on the one device we have
evidence from it reports a lapsed limit honestly while 123 on the same
inverter still shows it as active. Two controls for one physical
setting would confuse even if they agreed.

Every point of these blocks that is *not* a control stays available as
a read-only sensor, the `*_WinTms` and `*_RmpTms` timers included, so
you can watch a limit approach its expiry.

## The revert timer, and how not to get caught by it

Most inverters treat an export limit as a **dead-man switch**. They
apply it for `RvrtTms` seconds and then revert on their own, so an
inverter driven by a controller that died returns to normal operation
instead of staying throttled forever.

On some devices the enable flag and the setpoint keep reporting the old
value afterwards. The limit then looks active when it is not, which is
what happened to the KACO in
[#17](https://github.com/hilman2/ha-sunspec2/issues/17).

Three ways to deal with it, in descending order of how much of the
safety net they keep:

1. **Re-write on a schedule.** Recommended. Set the revert time to
   comfortably more than your automation's interval, for example 120 s
   for an automation running every 15 s, and write the limit every
   time. It holds while your control loop is alive and lapses on its
   own if Home Assistant stops.
   [milanhin/pv_curtailment](https://github.com/milanhin/pv_curtailment)
   does exactly this, and it is the pattern to copy.
2. **Make the lapse harmless.** Block 704 only. Set *Export limit
   revert value* to the same percentage and *Export limit stays on
   after revert* to on. The timer still fires, it just lands on the
   value you wanted anyway.
3. **Turn the timeout off.** Set the revert time to 0 where the device
   allows it. Simplest, and it removes the dead-man switch: the
   inverter stays throttled if your automation, Home Assistant or the
   network goes away. Reasonable for a permanent cap, a bad idea for a
   dynamic control loop.

## What is deliberately not writable

SunSpec marks 1586 points across 67 models as writable. Most of them
are not settings:

- **Blocks 707 to 710** are the over and under voltage and frequency
  trip curves, **block 703** is the enter-service envelope, and
  `AntiIslEna` in block 704 is islanding detection. Those are
  type-approved grid protection settings under VDE-AR-N 4105 and its
  equivalents. Writing there does not misconfigure an inverter, it
  disables the protection that disconnects it from a faulted grid.
- **Block 121** reads like a settings block and is not one. It holds
  `VMax` and `VMin`, and `WMax`, which is the reference every
  percentage in the device is measured against. Changing it silently
  redefines every export limit ever written.
- **Blocks 126 and 129 to 142** are curves in repeating groups. Writing
  one point without the others produces a shape the device never agreed
  to.

None of these are exposed, and that is not an oversight. The reasoning
per entry is in the docstring of
[`write_controls.py`](../custom_components/sunspec2/write_controls.py).

## Enabling it

1. *Settings -> Devices & Services -> SunSpec Modbus -> Configure*
2. Click through to the model options step
3. Tick **"Enable experimental write controls (BETA)"**
4. Save. The entities appear on your inverter's device card

You do not have to tick the matching data blocks in the model list
yourself. They are polled automatically while the flag is on, and kept
out of the sensor list because their points are setpoints rather than
measurements.

If your inverter does not offer the blocks at all, no entities appear
even with the flag on. The diagnostics download tells you which blocks
it has, in the top-level `detected_models` array. Not `scanned_models`,
and the difference has cost people time before:
[which data blocks does my inverter actually have?](troubleshooting.md#which-data-blocks-does-my-inverter-actually-have)

## Why it is still a beta

- **Vendors deviate.** Which firmware exposes which block, how scale
  factors are handled, what ranges are accepted. This integration is
  written against the specification, not against your firmware.
- **Persistence varies.** Some inverters keep a write through a power
  cycle, others reset to defaults on reboot.
- **No hardware here to test it on.** The KACO Powador 7.8 TL3 this
  integration was developed against does not expose the control blocks,
  so the write path has never been smoke-tested against a live device.

Which is why: **if you run these controls, please
[open an issue](https://github.com/hilman2/ha-sunspec2/issues)** with
your inverter model, its firmware revision, and which writes worked and
which did not. That feedback is what lets the BETA flag come off.
