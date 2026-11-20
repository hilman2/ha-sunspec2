# Blueprints: ready-made automations

A blueprint is an automation with blanks. You add it to Home Assistant
once, fill in your own entities in a form, and Home Assistant creates
the automation. You write no YAML. One blueprint makes as many
automations as you like, one per inverter for example.

| Blueprint | What it does |
|---|---|
| [Limit the inverter while the price is negative](#limit-the-inverter-while-the-price-is-negative) | Lowers the export limit to a fixed percentage while the electricity price is below a threshold |
| [Follow the house consumption](#follow-the-house-consumption) | Keeps the export limit at what the house uses, so that nothing, or only what you allow, goes into the grid |
| [Block battery discharging while a large consumer runs](#block-battery-discharging-while-a-large-consumer-runs) | Keeps the battery from emptying itself into a car charger or a heat pump |
| [Charge the battery from the grid while the price is low](#charge-the-battery-from-the-grid-while-the-price-is-low) | Charges the battery at a low price, up to a level you choose |
| [Tell me when the inverter has a problem](#tell-me-when-the-inverter-has-a-problem) | Sends a message when the inverter stops answering, reports a fault or raises an event |

How to add a blueprint to your Home Assistant, and how to get a newer
version of one, is in the
[README](../README.md#blueprints-ready-made-automations). The form of
each blueprint says what belongs in its fields. This page has what the
form does not say: what the automation does in which case, and what
your brand of inverter needs.

> **This can bite.** The first four blueprints change settings in your
> inverter, and they do it while you are not looking. Use the controls
> by hand first and read
> [Battery and export control](write-controls.md), so that you know what
> your inverter does with a limit or a battery mode.

Every blueprint is tested in Home Assistant with a simulated inverter.
Inverters differ, and not every brand has been tried with every
blueprint. Watch the first days, and please
[open an issue](https://github.com/hilman2/ha-sunspec2/issues) with your
inverter model and what worked.

## Limit the inverter while the price is negative

With a dynamic tariff, feeding into the grid earns nothing or costs
money while the price is negative. This blueprint sets the **Export
limit** of the inverter to a percentage you choose while the price is
below a threshold. When the price is back above the threshold, the
blueprint sets the limit to 100 %.

You need the **Export limit** entity of your inverter (see
[Battery and export control](write-controls.md#limiting-what-you-feed-into-the-grid))
and a sensor with the current price.

A limit of 0 % stops the inverter, and your house then takes all its
power from the grid. That pays off only while the price you pay is
negative too. If only feeding in costs money, a limit that leaves
enough for the house is better:
[Follow the house consumption](#follow-the-house-consumption) does
that by itself.

What the automation does:

- It writes the limit when the price crosses the threshold, not with
  every new price.
- If the inverter is off when the price crosses, the automation writes
  the limit when the inverter answers again, for example in the morning.
- If the price sensor has had no value for an hour, the automation
  sets the limit to 100 %.
- If a write fails, the automation tries twice more, 30 seconds apart.
  After the third failure it sends a message, if you chose somebody
  under **Notify**.
- When Home Assistant starts, the automation writes the limit that
  fits the current price.

### What your inverter needs

| Inverter | What to set |
|---|---|
| An inverter with an **Export limit revert time** above 0 | The inverter drops the limit after that many seconds. Either set **Export limit revert time** to 0, or choose a period under **Write the limit again** that is shorter than the revert time. More in [The revert timer](write-controls.md#the-revert-timer-and-how-not-to-get-caught-by-it) |
| APsystems ECU | Turn **Switch the limit on with every write** off, and switch **Export limit enabled** on once by hand. Leave **Write the limit again** at *Never*. The micro inverters follow a new limit only after several minutes. See [APsystems](apsystems.md#limiting-what-you-feed-in) |
| Fronius GEN24, Verto and Tauro | Tick **Re-enable controls after a setpoint change** under **Advanced** in the integration options. Without it the inverter keeps running on the old limit. See [Fronius](fronius.md#things-to-know) |

## Follow the house consumption

This blueprint keeps what you feed into the grid under a limit, down
to nothing at all. Every 30 seconds the automation reads how much power
the inverter delivers and how much goes into or comes out of the grid.
The sum is what the house uses. The automation sets the **Export
limit** to that, plus the feed-in you allow.

With a price sensor, the automation limits the feed-in only while the
price is below a threshold. That is the setting for a tariff where
feeding in costs money at negative prices: the house still runs on
solar power, and nothing goes into the grid.

You need:

- the **Export limit** entity of your inverter
- a sensor for the AC power of the inverter
- a sensor for the power at your grid connection, from a meter
- the rated power of the inverter in watts

Some inverters do this by themselves, and better, because they react
within a second. A Fronius with a smart meter has dynamic power
reduction in its own settings. A SolarEdge with a meter has the
[site limit](solaredge.md#limiting-what-you-feed-in). Use those where
you have them.

What the automation does:

- It writes the limit on every round, also when the value has not
  changed. Set the **Export limit revert time** of your inverter to at
  least three times the period, 90 seconds for the default of 30. If
  Home Assistant stops, the inverter then goes back to full power by
  itself. Without a revert time the inverter keeps the last limit.
- The limit is rounded down, so the automation rather takes a few
  watts from the grid than feeds a few in.
- While the inverter power or the grid power has no value, the
  automation writes the percentage from **Limit without a reading**.
  It is 100 at first, which is full power. Enter 0 if you must never
  feed in.
- With a price sensor: when the price is back above the threshold, the
  automation sets the limit to 100 % once and then leaves the inverter
  alone. It does the same when the price sensor has had no value for an
  hour.

What it cannot do:

- **It is not fast.** Between two rounds a consumer that switches off
  feeds its power into the grid. If you are not allowed to feed in at
  all, this blueprint is not enough. Use a limit built into the
  inverter.
- **It handles one inverter.** With several inverters on one meter,
  every automation would work out the same house consumption.
- **It is not for APsystems.** An ECU passes a limit on only after
  several minutes. Use
  [Limit the inverter while the price is negative](#limit-the-inverter-while-the-price-is-negative).
- **A Fronius runs without a limit for a second at every write** when
  **Re-enable controls after a setpoint change** is on, and does not
  take a new limit when it is off. See
  [Fronius](fronius.md#things-to-know).

## Block battery discharging while a large consumer runs

A car charger takes more power than the roof delivers, and the home
battery makes up the difference until it is empty. This blueprint sets
the **Battery mode** to *Block discharging* while the consumer runs.
The battery still charges from the solar panels. When the consumer has
been off for five minutes, the automation selects *Automatic* again.

You need the **Battery mode** entity. The integration offers it for
Fronius inverters with a battery, see
[Fronius](fronius.md#choosing-what-the-battery-does). For the consumer
you need a switch or binary sensor that is on while it runs, or a
sensor with its power.

What the automation does:

- It takes the battery over only from *Automatic*, and gives it back
  only from the mode it selected itself. A battery plan or a mode you
  chose by hand stays as it is.
- The inverter drops a mode after its **Battery rate revert time**, 300
  seconds on a GEN24 from the factory. The automation therefore selects
  the mode again every 3 minutes. If you change the revert time, keep
  **Select the mode again** shorter than it.
- With **Mode while the consumer runs** set to *Discharge limit*, the
  battery keeps helping with the power in **Battery discharge limit**.

To use the battery for the consumer after all, switch the automation
off and select *Automatic*.

## Charge the battery from the grid while the price is low

This blueprint sets the **Battery mode** to *Charge from grid* while
the electricity price is below a threshold. It selects *Automatic*
again when the price is back above the threshold or when the battery
has reached the level you chose.

You need the **Battery mode** entity, as above, and a sensor with the
current price. A sensor with the battery level is optional. Without
one the battery charges for as long as the price is low.

Before the first run:

1. Enter the charging power in **Battery grid charge power**. It
   starts at 0 W, and with 0 W the battery does not charge.
2. Allow charging from the grid in the inverter, see
   [Fronius, Before it works](fronius.md#before-it-works).

What the automation does:

- Charging stops at **Charge up to** and starts again when the level
  has fallen by more than **Start again below**. With the defaults the
  battery charges to 90 % and starts again below 85 %.
- It starts a charge only from *Automatic*. A battery plan or a mode
  you chose by hand stays as it is.
- It selects the mode again every 3 minutes, for the same reason as the
  blueprint above.
- If the price sensor has had no value for an hour, the automation
  selects *Automatic*.

The automation looks at the current price only. It does not pick the
cheapest hours of the night. For charging between two fixed times, use
the [battery plan](fronius.md#giving-energy-back-over-night).

## Tell me when the inverter has a problem

This blueprint sends a message in three cases. Each has its own part of
the form, and a part you leave empty is skipped. The blueprint only
reads. It changes nothing in the inverter.

| Case | What to choose |
|---|---|
| The inverter stops answering | Any sensor of the inverter, for example its power. You get a message when the sensor has been unavailable for 15 minutes |
| The inverter reports a fault | The **Operating state** sensor, or **Inverter state** on newer inverters. You get a message when it changes to FAULT. You can add other states |
| The inverter raises an event | One or more **Events** sensors, or **Alarms** on newer inverters. You get a message with the names of the events that are new, for example GROUND_FAULT |

Most inverters switch off at night and stop answering then. For that
reason the first case only counts while the sun is more than 10
degrees above the horizon. If the inverter has not come back when the
sun reaches that height in the morning, you get the message then. For
an inverter that runs day and night, such as one with a battery, enter
-90.

An inverter on a roof that is in the shade in the morning wakes up
late. If it wakes later than the sun reaches 10 degrees, raise the
value under **Only while the sun is above**.

A second message follows when the problem is over. You can turn that
off under **More**. There you can also add actions of your own, a
light that turns red for example. They run together with every
message.

The names of the states and events are the ones the sensor shows under
**Developer tools -> States**. The **Vendor events** sensors start
disabled. To have them watched, enable them in the entity list of the
device first.
