# Troubleshooting in detail

The [README](../README.md#when-something-is-wrong) covers the common
cases in a line or two each. This page is for when that was not enough.

**Start with the diagnostics download.** *Settings -> Devices &
Services -> SunSpec Modbus -> Download diagnostics*. The host is
redacted automatically. Switch on **Capture raw registers** in the
options first if you are chasing something that looks like a decoding
problem, and the Modbus bytes end up in the file too.

## The plausibility filter

Inverters report nonsense at dawn and dusk: megawatts of AC power,
terawatt-hours of lifetime energy, single readings that end up in your
long-term statistics forever. The **Peak AC power** option is the
ceiling that keeps those out.

It is prefilled from the inverter's own nameplate, read from SunSpec
block 120 or 121 with 20 % added on top, so in most cases you can leave
it alone. Older or vendor-stripped firmware publishes no nameplate, and
then you enter the value once.

The number is an **AC active power** ceiling, and the other quantities
are bounded by something else, so each gets its own room:

| Quantity | Ceiling | Why |
|---|---|---|
| AC active power | as configured | The value you set |
| Apparent and reactive power | 25 % on top | Grid codes require operation down to cos phi 0.80 |
| DC power | three times | The DC side is bounded by the MPPT inputs and the battery, not by the AC stage. Two MPPTs each rated at full AC power is already 2x, and a DC-coupled hybrid charges its battery on top |

Only live measurements are filtered. Nameplate ratings, setpoints and
limit registers are never touched.

**If a sensor reads `unknown` on a bright day**, the ceiling is too
low. The log says which sensor, which ceiling, and where the ceiling
came from:

```
Dropping implausible value for DCW: 9800.0 W is beyond the 7500.0 W
ceiling from configured peak AC power (rejection 1). If this is a
real reading, raise or clear 'Peak AC power' in the integration
options.
```

The line repeats once every 20 rejections, and one more line is logged
when the sensor recovers. The `plausibility_filter` block in the
diagnostics has the full picture, including the ceiling in force for
each quantity.

Raise the value, or clear the field entirely to switch the filter off.

### Energy counters get a second filter

A jump in a lifetime counter is dropped when it is larger than the peak
power could have produced since the counter last moved, times two.

Measured since it last moved, not per poll: many inverters update that
register only every few minutes and then jump by the whole amount at
once. A `Dropping implausible energy delta` line in the log is
therefore a real outlier, not a slow counter.

## The inverter disappears every night

Normal for most PV inverters. Below a certain DC input they shut down
and take the communication board with them, so the Modbus session dies
until the sun is back.

Since v0.28.0 the integration recognises this by itself. If the last
successful poll reported the inverter as OFF, SLEEPING, SHUTTING_DOWN
or STANDBY, no repair issue is raised and the log stays quiet until it
wakes up. Sensors still go unavailable after a few minutes, which is
the honest state for a device that is switched off.

If your inverter vanishes without ever reporting one of those states,
or publishes no operating state at all, switch on **Inverter powers
down when idle** in the options. That suppresses the unreachable
repair unconditionally.

A device that answers with a Modbus error, or answers with something
that is not SunSpec, still reports it either way. Those prove the
inverter is awake.

The `standby` block in the diagnostics shows both inputs to that
decision, so you can see which path applied.

## Sensors drop out for a few minutes at a time

The integration is built to absorb this: it retries a failed read after
five seconds, and keeps serving the last good value for five more
cycles, roughly three minutes at the default interval, before the
sensors go unavailable.

If the gaps are longer than that, something else is going on:

- Check whether **Release the Modbus connection between polls** is on.
  It rebuilds the Modbus session on every poll, which is the thing an
  embedded Modbus stack handles worst. On a KACO Powador 7.8 TL3 at a
  30 s interval, reconnecting per poll failed 5 of 6 cycles while one
  held session served 20 of 20. The option exists for the case where
  another program outside Home Assistant has to reach the same
  inverter, and it is a bad default for everyone else.
- On versions before v0.22.0 that reconnect happened unconditionally.
  Update.
- Otherwise the network link itself is dropping. Check the WiFi or the
  cable to the inverter.

## "Cannot reach SunSpec inverter" in the Repairs panel

Open the diagnostics and look at `recent_errors`. The repair fires
after three consecutive transport or device errors, or after a single
protocol error, so by the time you see it the inverter has genuinely
not been answering.

Check power, check the network, and check that no other Modbus client
is holding the connection. Most inverters accept exactly one.

## Everything went unavailable after an options change

You saved the options form with no data blocks ticked. Re-open it and
tick them again.

Versions from v0.7.6 refuse to save an empty selection. If you managed
to produce one, update first.

## Everything is unavailable and Repairs mentions cjne

The old `cjne/ha-sunspec` integration is still loaded for the same
inverter. Two integrations cannot poll a single-slot device at once.
Uninstall cjne in HACS, restart Home Assistant, and the migration runs
by itself.

## Which data blocks does my inverter actually have?

The top-level **`detected_models`** array in the diagnostics. That is
the raw scan result, everything the device answered to.

`scanned_models` is a different thing: it is built from the blocks
being polled, so a block your inverter has but you never enabled is
missing from it. Checking that one is what made a KACO Blueplanet look
like it had no block 123 in
[#17](https://github.com/hilman2/ha-sunspec2/issues/17), even though it
had been answering writes to those registers for two years.
