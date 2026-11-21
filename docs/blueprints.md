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

The four blueprints tell you when they need you: when a write has
failed for two hours, and when an automation has switched itself off
because of a setting. That appears as a notification in Home Assistant
and goes to whoever you chose under **Notify**.

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
- While the price sensor has no value, the automation writes nothing.
  After an hour without a value it sets the limit to 100 %.
- If a write fails, the automation tries twice more, 30 seconds apart,
  and then every five minutes for two hours. Each attempt writes what
  fits the price at that moment, and the attempts end when the price
  loses its value. You get a message after the third failure and when
  the automation gives up, if you chose somebody under **Notify**.
- When Home Assistant starts, the automation writes the limit that
  fits the current price.

The automation takes a limit back only while it is switched on. Before
you switch it off or delete it during a low price, set **Export limit**
to 100 % by hand.

### What your inverter needs

| Inverter | What to set |
|---|---|
| An inverter with an **Export limit revert time** above 0 | The inverter drops the limit after that many seconds. Either set **Export limit revert time** to 0, or choose a period under **Write the limit again** that is at least a minute shorter than the revert time. More in [The revert timer](write-controls.md#the-revert-timer-and-how-not-to-get-caught-by-it) |
| APsystems ECU | Turn **Switch the limit on with every write** off, and switch **Export limit enabled** on once by hand. Leave **Write the limit again** at *Never*. The micro inverters follow a new limit only after several minutes. See [APsystems](apsystems.md#limiting-what-you-feed-in) |
| Fronius GEN24, Verto and Tauro | Tick **Re-enable controls after a setpoint change** under **Advanced** in the integration options. Without it the inverter keeps running on the old limit. See [Fronius](fronius.md#things-to-know) |
| Fronius Symo, Primo, Eco, Galvo and Symo Hybrid | A limit below 10 % can put the inverter into standby. Enter 10 or more under **Limit while the price is low**. See [older Fronius](fronius-symo.md#limiting-what-you-feed-into-the-grid) |

## Follow the house consumption

This blueprint keeps what you feed into the grid under a limit, down
to nothing at all. In rounds of 30 seconds the automation reads how
much power the inverter delivers and how much goes into or comes out of
the grid. The sum is what the house uses. The automation sets the
**Export limit** to that, plus the feed-in you allow.

With a price sensor, the automation limits the feed-in only while the
price is below a threshold. That is the setting for a tariff where
feeding in costs money at negative prices: the house still runs on
solar power, and nothing goes into the grid.

You need:

- the **Export limit** entity of your inverter
- the sensor of this integration with the AC power of the inverter
- a sensor for the power at your grid connection, from a meter that
  reports at least every 30 seconds, also while its value stays the
  same. It has to report the power of the moment, or an average over
  ten seconds at most
- the rated power of the inverter in watts

Some inverters do this by themselves, and better, because they react
within a second. A Fronius with a smart meter has dynamic power
reduction in its own settings. A SolarEdge with a meter has the
[site limit](solaredge.md#limiting-what-you-feed-in). Use those where
you have them.

What the automation does:

- It works in rounds. In a round it waits for the meter, reads the
  inverter, writes the limit and then waits 30 seconds. It writes the
  limit also when it has not changed. Set the **Export limit revert
  time** of your inverter to at least four times the period, 120
  seconds for the default of 30. If Home Assistant stops, the inverter
  then goes back to full power by itself. Without a revert time the
  inverter keeps the last limit.
- The first limit comes about a minute after the automation starts, or
  after the price has gone low.
- A round in which the meter has not reported writes nothing. A meter
  that reports once a minute therefore gets a new limit once a minute.
  Choose **Every minute** under **Write the limit** for such a meter,
  and a revert time of 240 seconds. A slower meter does not work with
  this blueprint.
- A sensor that only reports when its value changes does not work
  either. Template sensors are of that kind. While the power is steady
  such a sensor gets no limit, and the limit it had ends: by the
  inverter after its revert time, or by the automation after ten
  minutes with **Limit without a reading**.
- The limit is rounded down, so the automation rather takes a few
  watts from the grid than feeds a few in. A meter that reports in
  steps of 100 W can still be 50 W off, in either direction.
- If the inverter does not take a limit, the automation tries again in
  the next round. After three failures in a row it waits five minutes
  between two attempts.
- While the inverter power or the grid power has no value, or the
  meter has not reported for ten minutes, the automation writes the
  percentage from **Limit without a reading**. It is 100 at first,
  which is full power. Enter 0 if you must never feed in.
- With a price sensor: when the price is back above the threshold, the
  automation writes the percentage from **Limit otherwise** once, 100
  at first, and then leaves the inverter alone. If that write fails,
  it tries again as described for the blueprint above.
- With a price sensor that has no value, the automation writes
  nothing. An inverter with a revert time goes back to full power
  after that time, one without keeps the last limit. After an hour
  without a value the automation writes **Limit otherwise**.

The automation takes a limit back only while it is switched on. Before
you switch it off or delete it, set **Export limit** to 100 % by hand,
unless your inverter has a revert time.

What it cannot do:

- **It is not fast.** Between two rounds a consumer that switches off
  feeds its power into the grid. The same goes for the sun coming out
  from behind a cloud in the seconds between the reading of the meter
  and the reading of the inverter: one limit is then off, and the next
  round corrects it. If you are not allowed to feed in at all, this
  blueprint is not enough. Use a limit built into the inverter.
- **It handles one inverter.** With several inverters on one meter,
  every automation would work out the same house consumption.
- **It needs an inverter that Home Assistant reads within ten
  seconds and that follows a limit within ten seconds.** With many
  data blocks ticked, or a high **Model read pause**, one reading of
  the inverter takes longer. The limit then goes back and forth after
  a change of the consumption. The same happens with a meter that
  reports an average over the period or longer.
- **One limit can be off after a reading that failed.** When the
  integration cannot read the inverter for a moment, the sensor keeps
  its last value, and the automation works with that. The same can
  happen in the first round after the meter was silent for longer than
  the revert time. The next round corrects it.
- **It is not for APsystems.** An ECU passes a limit on only after
  several minutes. Use
  [Limit the inverter while the price is negative](#limit-the-inverter-while-the-price-is-negative).
- **A Fronius GEN24 runs without a limit for a second at every write**
  when **Re-enable controls after a setpoint change** is on, and does
  not take a new limit when it is off. See
  [Fronius](fronius.md#things-to-know).
- **An older Fronius can go into standby below 10 %.** Enter 10 under
  **Lowest limit** for a Symo, Primo, Eco, Galvo or Symo Hybrid. See
  [older Fronius](fronius-symo.md#limiting-what-you-feed-into-the-grid).

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
  only from the mode it selects for the consumer. A battery plan that
  is charging or discharging, or another mode you chose by hand, stays
  as it is. The inverter does not tell who selected a mode: if you
  select *Block discharging* by hand, the automation gives the battery
  back as well, five minutes after the consumer stops and when Home
  Assistant starts.
- The inverter drops a mode after its **Battery rate revert time**, 300
  seconds on a GEN24 from the factory. The automation therefore selects
  the mode again every 3 minutes. If you change the revert time, keep
  **Select the mode again** at least a minute shorter than it.
- With **Mode while the consumer runs** set to *Discharge limit*, the
  battery keeps helping with the power in **Battery discharge limit**.
- The inverter reports the mode it is in, and with 0 W in one of the
  power fields that can be another mode than the one selected. With 0 W
  in **Battery PV charge limit**, *Block discharging* reads back as
  *Block charging*. The automation would not know such a mode as its
  own. It selects *Automatic* again, switches itself off and tells
  you. Switch it on again when the power fields are right. Do not
  change a power field to 0 W while the automation holds the battery:
  it then no longer knows the mode either, and gives the battery back
  only when the inverter has dropped the mode after its revert time.
- If the inverter does not take a mode, the automation tries twice
  more, 30 seconds apart, and then every five minutes for two hours,
  for as long as the mode is still wanted. You get a message after the
  third failure and when the automation gives up, if you chose
  somebody under **Notify**.

To use the battery for the consumer after all, switch the automation
off and select *Automatic*.

## Charge the battery from the grid while the price is low

This blueprint sets the **Battery mode** to *Charge from grid* while
the electricity price is below a threshold. It selects *Automatic*
again when the price is back above the threshold or when the battery
has reached the level you chose.

You need the **Battery mode** and the **Battery grid charge power**
entity, see [Fronius](fronius.md#what-you-get), and a sensor with the
current price. A sensor with the battery level is optional. Without
one the battery charges for as long as the price is low.

Before the first run:

1. Enter the charging power in **Battery grid charge power**. It
   starts at 0 W. With 0 W the automation does not start a charge and
   tells you so, once for every time the price goes low.
2. Allow charging from the grid in the inverter, see
   [Fronius, Before it works](fronius.md#before-it-works).

What the automation does:

- Charging stops at **Charge up to** and starts again when the level
  has fallen by more than **Start again below**. With the defaults the
  battery charges to 90 % and starts again below 85 %.
- It starts a charge only from *Automatic*. Another mode you chose by
  hand stays as it is.
- It selects the mode again every 3 minutes, for the same reason as the
  blueprint above.
- While the price sensor has no value, the automation selects nothing.
  After an hour without a value it selects *Automatic*.
- If the inverter does not take a mode, the automation tries again as
  described for the blueprint above.
- If the inverter reports another mode after *Charge from grid* was
  selected, the automation selects *Automatic* again, switches itself
  off and tells you. Switch it on again when the charging power is
  right.

**Do not use it together with a battery plan that charges.** Both use
*Charge from grid*, and the inverter does not tell who selected it. The
automation ends a charge of the battery plan, or one you started by
hand, when the price goes back above the threshold, when the battery
reaches **Charge up to**, after an hour without a price, and when Home
Assistant starts or the inverter answers again while the price is not
low or the battery is at that level.

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

The form takes one inverter. If you have more than one, make one
automation from the blueprint for each inverter. The title of a message
is the name of the device the sensor belongs to. Two inverters of the
same model have the same device names unless you gave them a **Sensor
name prefix**. If the names are the same, rename the devices in Home
Assistant, so that you can tell the messages apart.

Most inverters switch off at night and stop answering then. For that
reason the first case only counts while the sun is more than 10
degrees above the horizon. If the inverter has not come back when the
sun reaches that height in the morning, you get the message then. For
an inverter that runs day and night, such as one with a battery, enter
-90.

An inverter on a roof that is in the shade in the morning wakes up
late. If it wakes later than the sun reaches 10 degrees, raise the
value under **Only while the sun is above**.

Micro inverters behind an APsystems ECU are read through the ECU. One
automation with the **AC power** sensor of any one micro inverter tells
you when the ECU stops answering. The ECU that was tested answers at
night too, so enter -90 if you want to hear about a failure at night.
Leave the other two parts of the form empty: on that ECU the operating
state and the events never change, see
[What the ECU does not report](apsystems.md#what-the-ecu-does-not-report).

A second message follows when the problem is over. For an inverter
that answers again, it is only sent while the sun is above that height
too. You can turn the second message off under **More**. There you can
also add actions of your own, a light that turns red for example. They
run together with every message.

An event that is still raised when the inverter comes back from the
night is reported again. To silence one, enter its name under **Events
to ignore**.

The names of the states and events are the ones the sensor shows under
**Developer tools -> States**. The **Vendor events** sensors start
disabled. To have them watched, enable them in the entity list of the
device first.
