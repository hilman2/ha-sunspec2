# Troubleshooting

The [README](../README.md#when-something-is-wrong) covers the common
cases in a line or two each. This page is for when that was not enough.
Find your symptom below.

**Start with the diagnostics download.** It is a file that describes
what the integration sees and what the inverter answered. Your IP
address is removed automatically. Get it here:

*Settings -> Devices & Services -> SunSpec Modbus -> Download
diagnostics*

If something looks like wrong numbers rather than missing numbers,
switch on **Capture diagnostic registers** under **Advanced** in the
integration's options first, wait a minute, then download. The file
then also holds what the inverter sent, unprocessed.

Attach that file to every bug report.

## A sensor shows "unknown" on a bright day

The integration throws away readings that cannot be true. Inverters
report nonsense at dawn and dusk: megawatts of power, terawatt-hours of
energy. Such a value would stay in your long-term statistics forever.
The option **Peak AC power** sets the ceiling for what counts as
believable.

It is filled in from the inverter's own nameplate, plus 20 %. In most
cases you can leave it alone. Some older inverters do not publish their
nameplate. Then you enter the value once, in kilowatts.

If the ceiling is too low, real readings are thrown away and the sensor
reads `unknown`. To fix it, open *Settings -> Devices & Services ->
SunSpec Modbus -> Configure -> Advanced* and raise **Peak AC power**.
Clearing the field returns to automatic nameplate detection. If the
inverter does not publish a rating, no power ceiling is applied.

To see which sensor was dropped and why, look in the Home Assistant log.
You will find a line like this:

```
Dropping implausible value for DCW: 9800.0 W is beyond the 7500.0 W
ceiling from configured peak AC power (rejection 1). If this is a
real reading, raise or clear 'Peak AC power' in the integration
options.
```

In plain words: the sensor, the value it was sent, the ceiling it was
measured against, and what to do. The line repeats once every 20
rejections. One more line appears when the sensor recovers.

### How much room each kind of value gets

The number you enter is a ceiling for AC power. Other values are
bounded by something different, so each gets its own room:

| Value | Ceiling | Why |
|---|---|---|
| AC power | As entered | The value you set |
| Apparent and reactive power | 25 % on top | Grid rules require inverters to work down to a power factor of 0.80 |
| DC power (what the panels deliver) | Three times | The panels and the battery limit the DC side, not the AC output. Two panel inputs, each as strong as the whole AC side, already make double. A hybrid inverter also charges its battery on top |

Only live measurements are filtered. Nameplate values, settings and
limits are never touched.

### Energy counters get a second check

A lifetime energy counter that jumps up is thrown away if the jump is
larger than the inverter could have produced since the counter last
changed, times two.

"Since the counter last changed" is not "since the last poll". Many
inverters update that value only every few minutes and then jump by the
whole amount at once. A log line `Dropping implausible energy delta` is
therefore a real outlier, not just a slow counter.

A lifetime counter that goes down is held at its last value too, with or
without a Peak AC power set. A lifetime counter has no legitimate way
down, and Home Assistant would book a drop as a meter reset. The log
line for that reads `Holding ... a lifetime counter does not go down`.
If the counter stays low for three polls in a row, the integration takes
it as a real reset and accepts it.

## The inverter is unreachable every night

This is normal for most solar inverters. When the sun is gone they
switch off, and the network board switches off with them. So the
connection is gone until the sun is back.

The integration knows this. If the last reading before the connection
was lost said the inverter was OFF, SLEEPING, SHUTTING_DOWN or STANDBY,
it raises no repair notification and the log stays quiet. The sensors
still go unavailable after a few minutes. That is correct: the device is
switched off.

If your inverter disappears without ever saying it is going to sleep, or
does not report its state at all, switch on **Allow the inverter to go
offline when idle** under **Advanced** in the options. That stops the
"Cannot reach SunSpec inverter" notification for good.

An inverter that answers with an error, or with something that is not
SunSpec, is still reported either way. It proves the inverter is awake.

The diagnostics file shows in its `standby` section which of the two
paths applied.

## Sensors drop out for a few minutes at a time

A short network hiccup should not show. The integration tries again
after five seconds. If that fails too, the sensors keep showing the last
good value for five more polls. At the default interval that is about
three minutes. Only after that do they go unavailable.

If your gaps are longer, something else is going on:

- **Check whether another program reads the same inverter.** Home
  Assistant shares one connection between its integrations. An external
  program still needs its own connection, which a single-slot inverter
  cannot accept. Use a Modbus proxy when several programs need access.
- **Check the network link.** WiFi to the inverter is the usual
  suspect. A cable is better.

## "Cannot reach SunSpec inverter" in Repairs

The inverter has not been answering. The notification appears after
three failed polls in a row (network or device errors), or right away
after one answer the integration cannot make sense of. So by the time
you see it, the problem is real.

1. Is the inverter powered on, and is it day?
2. Is it on the network? Can you open its web page?
3. **Is another program holding the connection?** Most inverters accept
   exactly one Modbus client at a time. That includes the old cjne
   integration, openHAB and scripts.

Open the diagnostics file and look at the `recent_errors` section.
It lists the last errors the integration saw.

## Everything went unavailable after changing the options

You saved the options with no data blocks ticked, so there was nothing
left to read. Open the options again and tick at least one. Current
versions refuse an empty selection. If you managed to produce one, update
first.

## Everything is unavailable and Repairs mentions cjne

The old `cjne/ha-sunspec` integration is still loaded for the same
inverter. Two integrations cannot use a single-connection inverter at
the same time. Uninstall cjne in HACS, restart Home Assistant, and the
migration runs by itself. See
[Coming from cjne/ha-sunspec](../README.md#coming-from-cjneha-sunspec).

<a id="which-data-blocks-does-my-inverter-actually-have"></a>

## Which data blocks does my inverter have?

Inverters publish their data in "blocks" (SunSpec calls them models).
Each block covers one topic: the AC output, the meter, the battery, the
controls. Which blocks your inverter has decides which sensors you can
get.

Look in the diagnostics file at the top-level list **`detected_models`**.
That is everything the inverter answered with.

Do not use `scanned_models` for this. It only lists the blocks the
integration is currently reading. A block your inverter has but you have
not ticked is missing there. That mix-up made a KACO Blueplanet look as
if it had no power control block in
[#17](https://github.com/hilman2/ha-sunspec2/issues/17), although it had
been answering writes to those registers for two years.
