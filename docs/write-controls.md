# Battery and export control

Most of this integration only reads: it shows you what your inverter
does. This page is about the part that **changes** what the inverter
does. You can limit how much power goes into the grid, and you can
steer a battery. The [README](../README.md#battery-and-export-control)
has the short version.

> **This can bite.** Writing to an inverter changes its settings. Some
> inverters keep such a change through a restart. Some refuse to hand
> control back until you reset them by hand. What a setting does is not
> always what the standard says it does. And limits on how much you
> may feed into the grid can be regulated where you live.

## What is switched on and what is not

| Part | Switched on by |
|---|---|
| Battery controls | Nothing to do. They appear on their own when the inverter has a battery that can be steered |
| Export limit, power factor, grid connection, the `sunspec2.set_export_limit` action | Available when the inverter supports the corresponding controls |

Some brands have their own extras on top. They have their own pages:
[Fronius GEN24, Verto and Tauro](fronius.md),
[Fronius Symo, Primo, Eco, Galvo and Symo Hybrid](fronius-symo.md),
[SolarEdge](solaredge.md), [Kostal](kostal.md), [SMA](sma.md) and
[APsystems](apsystems.md).

## What you get

Which of these appear depends on what your inverter offers. An inverter
without a battery gets no battery controls.

### Limiting what you feed into the grid

| Entity | What it does |
|---|---|
| **Export limit** | Caps the inverter's output to a share of its rated power, from 0 to 200 %. 0 means no export at all. Values above 100 are allowed because some inverters use 110 for "no limit". A Fronius needs a switch off and on for a new value, see [Fronius](fronius.md) |
| **Export limit enabled** | The limit only applies while this is on |
| **Export limit revert time** | How many seconds the inverter keeps the limit before it drops it on its own. See [The revert timer](#the-revert-timer-and-how-not-to-get-caught-by-it) |
| **Export limit revert value** | The percentage the inverter falls back to when the timer runs out. Switched off by default |
| **Export limit stays on after revert** | Whether the limit stays on once the timer has run out. Switched off by default |
| **Active power setpoint** | The same limit in watts instead of percent. Usually what you want for a "never export" automation, because with percent your automation has to know the rated power |
| **Active power setpoint percent** | Sets active power from -100 to 100 % of rated power. Negative values request import on devices that support it |
| **Active power setpoint enabled** | The active power setpoint only applies while this is on |
| **Active power setpoint mode** | Select **Percent of maximum** to use **Active power setpoint percent**, or **Watts** to use **Active power setpoint**. Switched off by default |
| **Power factor setpoint** | A value from -1 to 1 that sets how much reactive power the inverter produces. Most people never need it |
| **Power factor enabled** | The power factor only applies while this is on |
| **Inverter grid connection** | **The most dangerous one.** Off disconnects the inverter from the grid completely. Switched off by default: turn it on in the entity list if you need it |

Where an inverter has two ways of doing this, the integration takes the
newer one for the controls and shows the older one as read-only values.
Two controls for one setting would confuse you even if they agreed.

### Steering a battery

| Entity | What it does |
|---|---|
| **Battery charge rate** | Charging power as a percentage of the battery's maximum, 0 to 100 % |
| **Battery discharge rate** | Discharging power as a percentage of the maximum, 0 to 100 % |
| **Battery max charge power** | The maximum in watts that the two percentages refer to |
| **Battery control mode** | Off, charge only, discharge only, or both |
| **Battery rate revert time** | Seconds until the inverter drops your charge and discharge rates on its own. Works like the export limit's timer |
| **Battery minimum reserve** | How much charge the battery keeps back, for example for a power cut. Switched off by default |

The two rates are **percentages, not watts**. If your automation wants
4200 W, it has to calculate `4200 / maximum * 100`. Read the maximum from
the **Battery max charge power** entity instead of typing it in, so the
automation survives a battery swap.

On a Fronius the integration does that for you: it offers a *Battery
mode* menu and fields in watts. See [Fronius](fronius.md).

Every value in these blocks that is not a control stays available as a
read-only sensor. That includes the timers, so you can watch a limit
approach its end.

### Setting the limit from an automation

The action **`sunspec2.set_export_limit`** sets the limit without you
having to write to the number entity. It takes the `config_entry_id` of
your SunSpec entry, a `percent` value, and optionally `enable`.

## The revert timer, and how not to get caught by it

Most inverters treat an export limit as a **dead-man switch**. They
apply it for a number of seconds and then drop it on their own. That is
on purpose: if the controller that set the limit died, the inverter
goes back to normal operation instead of staying throttled forever.

The catch: on some inverters the switch and the value keep showing the
old limit after it has lapsed. It then looks active when it is not. That
is what happened to the KACO in
[#17](https://github.com/hilman2/ha-sunspec2/issues/17).

There are three ways to deal with it. They are listed from most safety
net to least:

1. **Write the limit again and again.** This is the recommended way.
   Set the revert time to clearly more than your automation's interval:
   120 seconds for an automation that runs every 15 seconds, for
   example. Write the limit every time it runs. The limit holds as long
   as your automation lives, and drops by itself if Home Assistant
   stops. [milanhin/pv_curtailment](https://github.com/milanhin/pv_curtailment)
   does exactly this and is the pattern to copy.
2. **Make the lapse harmless.** Only on inverters that offer the
   newer control. Set **Export limit revert value** to the same
   percentage as your limit, and switch **Export limit stays on after
   revert** on. The timer still runs out, but it lands on the value you
   wanted anyway.
3. **Turn the timer off.** Set the revert time to 0 where the inverter
   allows it. This is the simplest, but it also removes the safety net:
   if your automation, Home Assistant or the network goes away, the
   inverter stays throttled. Fine for a permanent cap, a bad idea for an
   automation that changes the limit all day.

## Enabling the export controls

The controls appear on your inverter's device page when it supports
them. You do not have to enable an integration option or select the
control data blocks. Setup and polling only read values. A control or
service action writes when you use it.

Some controls, including **Inverter grid connection**, start disabled
in the entity list. Enable an individual entity there when you need it.

If your inverter has no control data, no entities appear. To check what
your inverter has, see
[Which data blocks does my inverter have?](troubleshooting.md#which-data-blocks-does-my-inverter-actually-have).

## What you cannot change on purpose

The standard marks a lot of settings as writable. Most of them are not
meant for a home automation, and the integration leaves them alone:

- **Grid protection.** The settings for over and under voltage and
  frequency, the conditions under which the inverter is allowed back on
  the grid, and the anti-islanding switch (which makes the inverter shut
  down when the grid goes away) are type-approved safety settings. A
  wrong value does not just misconfigure your inverter. It switches off
  the protection that disconnects it from a faulted grid.
- **The rated-power block.** It looks like a settings block, but it holds
  the reference that every percentage in the inverter is measured
  against. Changing it would silently change what every export limit
  ever written means.
- **Curves.** A curve is a series of points that belong together.
  Writing one point without the others produces a shape the inverter
  never agreed to.

None of that is an oversight. The reasoning for each entry is in the
source file
[`write_controls.py`](../custom_components/sunspec2/write_controls.py).

## Differences between inverters

- **Inverters differ from the standard.** Which firmware offers which
  data, how numbers are scaled, which values are accepted: all of it
  varies. This integration follows the standard, not your firmware.
- **Some inverters keep a write through a power cycle, others forget
  it.**

If you use these controls, please
[open an issue](https://github.com/hilman2/ha-sunspec2/issues) with your
inverter model, its firmware version and which writes worked and which
did not.

---

## Technical details

### Which entity writes which SunSpec point

SunSpec calls a data block a *model*. The controls live in models 123
(immediate controls), 704 (DER AC controls) and 124 (basic storage
control).

| Entity | Model and point |
|---|---|
| Export limit | 123 `WMaxLimPct`, 704 `WMaxLimPct` |
| Export limit enabled | 123 `WMaxLim_Ena`, 704 `WMaxLimPctEna` |
| Export limit revert time | 123 `WMaxLimPct_RvrtTms`, 704 `WMaxLimPctRvrtTms` |
| Export limit revert value | 704 `WMaxLimPctRvrt` |
| Export limit stays on after revert | 704 `WMaxLimPctEnaRvrt` |
| Active power setpoint | 704 `WSet` |
| Active power setpoint percent | 704 `WSetPct` |
| Active power setpoint enabled | 704 `WSetEna` |
| Active power setpoint mode | 704 `WSetMod` |
| Battery charge rate | 124 `InWRte` |
| Battery discharge rate | 124 `OutWRte` |
| Battery max charge power | 124 `WChaMax` |
| Battery control mode | 124 `StorCtl_Mod` |
| Battery rate revert time | 124 `InOutWRte_RvrtTms` |
| Battery minimum reserve | 124 `MinRsvPct` |
| Power factor setpoint | 123 `OutPFSet` |
| Power factor enabled | 123 `OutPFSet_Ena` |
| Inverter grid connection | 123 `Conn` |

- The battery rates are percentages of `WChaMax`.
- The `*_WinTms` and `*_RmpTms` points (window and ramp timers) stay
  available as read-only sensors.
- **Where a device has both 123 and 704**, the controls come from 704
  and 123 stays read-only. 704 is the modern equivalent and accepts an
  absolute watt setpoint. On the one device with evidence, 704 reports a
  lapsed limit honestly while 123 on the same inverter still shows it as
  active.
- A running Fronius scheduled discharge sets *Battery rate revert time*
  to the length of its window, see
  [Fronius](fronius.md#giving-energy-back-over-night).

### What is deliberately not writable

SunSpec marks 1586 points across 67 models as writable.

- **Models 707 to 710** are the over and under voltage and frequency
  trip curves. **Model 703** is the enter-service envelope. `AntiIslEna`
  in model 704 is islanding detection. These are type-approved grid
  protection settings under VDE-AR-N 4105 and its equivalents.
- **Model 121** holds `VMax`, `VMin` and `WMax`. `WMax` is the reference
  that every percentage in the device is measured against.
- **Models 126 and 129 to 142** are curves in repeating groups.

### Which models are polled

The battery model (124) and export models (123, 704) are polled whenever
the inverter has them, regardless of the model selection. Writable
points become controls. The existing `WSetPct` sensor remains available
alongside its control to preserve entity IDs and history.

The diagnostics file lists the models the inverter has in its top-level
`detected_models` array. Not `scanned_models`, which only lists what is
being polled.
