# APsystems

This page is for APsystems micro inverters (DS3, QS1, QT2, YC) that
report to an ECU-R or ECU-C gateway. Home Assistant does not talk to
the micro inverters directly. It talks to the gateway, which answers
for each of them. The integration recognises the gateway by the
manufacturer name it reports, so there is nothing to select.

Each micro inverter counts as its own device. Add one entry per micro
inverter, all with the gateway's IP address and a different **unit ID**
(the number that tells the gateway which micro inverter you mean).

## Before it works

1. Check that your gateway supports it:
   - ECU-R with a serial number starting 2160, firmware 1.3.7 or later
   - ECU-R with a serial number starting 2162, firmware 2.0.2 or later
   - ECU-C with a serial number starting 215, firmware C1.1.3 or later

   The ECU-B does not support it.

   Users of the other integration report that an ECU-R Pro (firmware
   2.1.32A) and an ECU-C work with DS3, QS1 and YC600 micro inverters.
2. In EMA Manager open *Workspace -> Modbus Configuration*. Switch
   **SunSpec Modbus** on and give each micro inverter an address. Those
   addresses are the unit IDs you enter in Home Assistant.
3. Use port 502 over the network (TCP), or the RS485 port of the ECU.
   On an ECU-R, Modbus over the network only works on the **wired**
   port, so give it an Ethernet cable and a fixed IP address. Users
   report that an ECU-R Pro and an ECU-C also answer over Wi-Fi.
4. Modbus is off in the factory settings, and the setting is in the
   installer part of EMA Manager. If you have no installer account, use
   the ECU's own access point: install EMA Manager on your phone, press
   the **AP button** on the side of the ECU, connect the phone to the
   Wi-Fi it starts (`ECU_R_2160...`) and choose **Local Access** in the
   app. The Modbus settings are under *Workspace*. Depending on the
   app version they are called *Modbus settings* or *Modbus
   Configuration*.
5. Testers report that APsystems features such as Dynamic Tariff stop
   working while Modbus is on. That is the ECU's behaviour, not
   something the integration can change.
6. Let **nothing else talk to the ECU over Modbus** while the
   integration runs. In one test a second connection made the ECU stop
   answering the integration.

## What you get

The usual sensors for each micro inverter: AC power, voltage, current,
frequency, temperature, operating state and the energy it produced.

Beyond that, the integration adds the values for each solar panel input
of the micro inverter, which APsystems keeps in a place of its own:

| Sensor | What it shows |
|---|---|
| **DC voltage input n** | Voltage coming from the panel on input n |
| **DC current input n** | Current coming from the panel on input n |
| **DC power input n** | Power coming from the panel on input n |

A micro inverter has fewer than 8 inputs (a DS3 has 2). The gateway
reports 0 for the inputs that do not exist. So only inputs 1 and 2 are
switched on. The others are in the entity list, switched off. Turn on
as many as your micro inverter has.

## How often the values change

The ECU collects the values from the micro inverters over Zigbee about
every 5 minutes, and Modbus only reads what the ECU has collected. Polling
more often does not make the values fresher. With many micro inverters it
overloads the ECU: testers with eight micro inverters behind one ECU-R
got timeouts at a 20 second interval. If you see timeouts, raise the
polling interval. One tester's ECU-R also became stable and fast again
after the router stopped giving its wired connection internet access
(the Wi-Fi connection can stay, so the EMA app keeps working). That is
a single observation. Daily totals can differ by about 1 % from the EMA app.

The ECU starts a round on the clock, about 10 seconds past every fifth
minute, and works through the micro inverters one by one. With many of
them the last one is up to a few minutes behind the first. A reply can
take close to a minute when the ECU is busy, and a request you give up
on keeps it busy.

The integration allows for that. It waits up to a minute for each
answer. If an answer does not come, it sends nothing more to the ECU
until about a minute into the ECU's next round, for every micro inverter
behind it. In that time the sensors keep their last value for a few
polls and then go unavailable, as they do when the ECU is off. Changing
the export limit in that time fails with a message that says when to
try again.

## When the ECU stops answering until you restart it

One user's ECU-R stopped answering for a whole day, while it went on
collecting from the micro inverters and reporting to the EMA portal.
Only a restart of the ECU brought it back. Shorter outages of about
half an hour ended on their own.

If this happens to you, a switchable plug in front of the ECU can do the
restart. This automation switches it off for a minute once a sensor of
the micro inverter has been unavailable for 45 minutes. Replace the two
entity ids with yours:

```yaml
automation:
  - alias: "Restart the APsystems ECU when it stops answering"
    triggers:
      - trigger: state
        entity_id: sensor.ecu_inverter_single_phase_watts
        to: unavailable
        for: "00:45:00"
    actions:
      - action: switch.turn_off
        target:
          entity_id: switch.ecu_plug
      - delay: "00:01:00"
      - action: switch.turn_on
        target:
          entity_id: switch.ecu_plug
```

A restart clears an export limit on the micro inverters, see
[Limiting what you feed in](#limiting-what-you-feed-in). If you use one,
set it again after the restart.

## What the ECU does not report

The following comes from one tester with eight DS3 on an ECU-R
(firmware 1.3.23). It is not in the APsystems document, so treat it as
an observation:

- **Operating state** always reads MPPT, also at night, and the **event
  flags** always read 0. Do not build an automation on either.
- A **DS3-L** and a **DS3** both report the model DS3, so the integration
  cannot tell them apart.
- There is no timestamp or round counter. A value that did not change
  can be new or old.
- At night a micro inverter can keep showing 1 W with 0 V and 0 Hz.

## Energy produced today

**Energy produced today** is the yield of the current day. It is not a
lifetime total.

The ECU does not start it again at midnight. It keeps yesterday's yield
through the night and starts again at zero when the micro inverter
first reports in the morning. The integration shows 0 from midnight
until then.

After an outage, the first value after midnight can still be yesterday's
and higher than the last one Home Assistant saw. It is shown as it
comes, so the Energy dashboard books that energy to the hour it arrived
in. You can move it to the right day under *Developer tools ->
Statistics*.

## A total for the Energy dashboard

The ECU has no lifetime counter, so the integration counts one itself:
**Lifetime energy produced (counted)**. It adds up the daily values of
the micro inverter, day by day, and keeps the sum across restarts. Use it
as solar production in the Energy dashboard if you want a total. The
daily sensor also works there, because Home Assistant reads the drop to
zero as the start of a new day.

What to expect:

- It starts when the sensor is added, with what the micro inverter has
  made so far that day. Your total in the EMA app is not part of it.
- A reading that does not fit a counter that only goes up waits for the
  next one. A single wrong reading from the ECU therefore never ends up
  in the total.
- If Home Assistant is off over a night, the part of the day it missed
  is not counted.
- Every micro inverter has its own total, because every micro inverter
  is its own entry.

## Limiting what you feed in

The integration offers controls to limit the output of your micro
inverters. How that works, and the risks, are in
[Battery and export control](write-controls.md).

What you get:

| Entity | What it does |
|---|---|
| **Export limit** | Caps the output in whole percent |
| **Export limit enabled** | The limit only applies while this is on |
| **Inverter grid connection** | Disconnects the inverter from the grid. In the entity list, off by default. An ECU-R reportedly takes it back, see below |

The ECU has no timer after which the limit lapses and no power factor
setting. So those entities do not exist for it.

**An ECU-R reportedly takes Inverter grid connection back.** One tester
reports this from an ECU-R (firmware 1.3.23) with DS3 micro inverters.
Writing 0 stops the micro inverters within half a minute, and the ECU
keeps reading back 1. At its next round, within five minutes, the ECU
turns them on again. To keep them off, the 0 has to be written again
about every five minutes, before each round. The integration does not
do that for you. Use the export limit for anything that must last.

**Take the shown value with a grain of salt.** An ECU-R shows an export
limit of 30 % with the limit enabled before anyone has set one, while
the micro inverters run at full power. Treat that as the ECU's default
display, not as an active limit. Users of the other integration report
that the ECU can show it again after a power cut.

**An ECU that does not pass the limit on.** An early tester measured this
on one ECU-R (firmware 1.3.23) with eight DS3 micro inverters. It is not
in the APsystems document, so treat it as an observation:

- The ECU only passes a limit on to the micro inverters when the value
  *changes*. If one micro inverter misses a change, the ECU does not send
  it again. Writing the same value again does nothing. Write another
  value first (99 instead of 100), wait a few minutes, then the value
  you want.
- Restarting the ECU clears the limit on the micro inverters, while the
  ECU keeps showing the old value.
- At 0 % each micro inverter still delivers about 15 to 31 W. A user
  of an ECU-R Pro reports that it turns them fully off at 0 %, and that
  output takes about 5 minutes to return after the limit is raised. At 20 %
  a DS3-L delivered up to 17 W more than 20 % of its rating. Read the
  limit as a ceiling with some give, not as an exact value.
- Just before dusk the ECU can show 30 % for a single reading.
- Turning **Export limit enabled** on while it is already on stopped the
  ECU from answering at all, twice, until it was switched off and on.
  Switch it on only when it shows off. The `set_export_limit` action
  sets the flag too. In an automation that runs while the limit is
  already on, pass `enable: false`.

If a micro inverter stays limited, set 100 %. If that does not help,
switch the ECU off for a minute. In the tester's runs this cleared the
limit on all micro inverters. Before you remove the integration, set
100 % and check that every micro inverter delivers power again.

**One setting for all.** One ECU holds one limit for all its micro
inverters. Writing it to one unit ID is enough, and every unit ID shows
it. The ECU asks the micro inverters about every 5 minutes, so it takes
that long for the first micro inverter to follow. With eight micro
inverters it takes up to about 13 minutes for all of them.

**It works.** On eight DS3 behind one ECU-R, 10 % held every micro
inverter at about 65 W, and 100 % released them.

### ECU-R firmware 1.3.23 does not follow the standard

SunSpec is the standard that all inverters of this kind are meant to
follow. It describes the power control data as a block with a fixed
number (123) and a fixed length (24 values). ECU-R firmware 1.3.23
does it differently: it labels that block with a different number (114)
and makes it twice as long (48 values). The label 114 is also in use for
something else on these ECUs, the panel inputs above, so a program that
trusts the label gets confused.

You do not have to do anything. The integration notices this, ignores
the wrong label and reads the data as the power control block anyway.
The export limit and the on/off switch work as described above.

If you see odd behaviour with this firmware, include the diagnostics
download in your report (see [Troubleshooting](troubleshooting.md)).

---

## Technical details

- **Models the gateway offers**, as the APsystems document lists them:
  1, 101 or 103, 111 to 113 and 123 (the controls).
- **The panel inputs** are not in a SunSpec model. APsystems keeps them in
  a block of its own, labelled model 114, with 8 float32 values each
  for DC voltage, current and power, starting at register 40214. The
  integration reads it from there.
- **Energy produced today** is the SunSpec point `WH`. The standard
  defines it as a lifetime counter, the ECU delivers a daily value. It
  starts again when the micro inverter first reports in the morning and
  holds yesterday's value until then.
- **Polling:** a request may take 60 seconds. After a request without an
  answer nothing goes to the ECU, from any entry behind it, until 60
  seconds after the next five-minute mark that is at least a minute
  away. The polls in that time send nothing and count as failed. The
  diagnostics download shows the end of such a rest as
  `gateway_rest_until`.
- **ECU-R firmware 1.3.23:** the header of the controls model (123)
  reads 114, with 48 registers instead of 24. The integration reads the
  registers behind the header as model 123.
- **Writes** go out as function code 16, "write multiple registers",
  also for a single register. An ECU-R ignores function code 6 without
  an error (issue 1 of the repository named below).
- **ECU-R Pro:** the register addresses are the same. The header of the
  controls reads 123 with 24 registers, so no relabelling is needed, the
  enable flag reads as a plain 0 or 1 (an ECU-R reads back the ASCII
  digit), and the version in model 1 holds the micro inverter firmware
  (`V626003`). Source: the register dump of an
  ECU-R Pro in discussion 2 of that repository.
- **Controls:** *Export limit* is `WMaxLimPct`, *Export limit enabled*
  is `WMaxLim_Ena`, *Inverter grid connection* is `Conn`. The ECU has no
  `WMaxLimPct_RvrtTms` and no `OutPFSet`, so those entities are not
  built.

Source: APsystems, SunSpec Modbus, rev 3.3
(https://global.apsystems.com/wp-content/uploads/2025/01/SunSpec-Modbus.pdf).
The 15 to 31 W at 0 % and the 17 W at 20 % are measurements on DS3-L
micro inverters by the author of
[homeassistant-apsystems_modbus](https://github.com/christianvaes/homeassistant-apsystems_modbus),
version 0.4.0. The round timing, the readings the ECU gets wrong and
the flag that silenced the ECU are that author's observations too
(issue 1 and the code of version 0.4.12 in that repository, ECU-R
firmware 1.3.23). None of them is in the APsystems
document, and we have not reproduced them. Also from that
repository, and equally unreproduced here: the behaviour of *Inverter
grid connection* (the code of versions 0.5.0 to 0.5.6), the ignored
function code 6 and the 30 % after a power cut (issue 1), and what
users of an ECU-R Pro and an ECU-C report about the models and firmware,
Wi-Fi, the register layout of the ECU-R Pro and that it turns the micro
inverters fully off at 0 % and needs about 5 minutes to bring them back
(discussion 2). The router tip for an unstable ECU-R is from the same
discussion.

The morning restart of the daily counter and the ECU that stopped
answering until it was restarted are from the history attached to
[issue #160](https://github.com/hilman2/ha-sunspec2/issues/160).
