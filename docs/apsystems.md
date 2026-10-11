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
2. In EMA Manager open *Workspace -> Modbus Configuration*. Switch
   **SunSpec Modbus** on and give each micro inverter an address. Those
   addresses are the unit IDs you enter in Home Assistant.
3. Use port 502 over the network (TCP), or the RS485 port of the ECU.
   Over the network, Modbus only works on the **wired** port, so give
   the ECU an Ethernet cable and a fixed IP address.
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
polling interval. Daily totals can differ by about 1 % from the EMA app.

## Energy produced today

**Energy produced today** is the yield of the current day. It is not a
lifetime total, and it starts again at zero every night.

## Limiting what you feed in

The integration can limit the output of your micro inverters. This is
off until you switch on the experimental export controls in the
integration's options. How that works, and the risks, are in
[Battery and export control](write-controls.md).

What you get:

| Entity | What it does |
|---|---|
| **Export limit** | Caps the output in whole percent |
| **Export limit enabled** | The limit only applies while this is on |
| **Inverter grid connection** | Disconnects the inverter from the grid. In the entity list, off by default |

The ECU has no timer after which the limit lapses and no power factor
setting. So those entities do not exist for it.

**Take the shown value with a grain of salt.** An ECU-R shows an export
limit of 30 % with the limit enabled before anyone has set one, while
the micro inverters run at full power. Treat that as the ECU's default
display, not as an active limit.

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
- At 0 % each micro inverter still delivers about 20 to 30 W.
- Just before dusk the ECU can show 30 % for a single reading.

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
  defines it as a lifetime counter, the ECU delivers a daily value
  that resets every night.
- **ECU-R firmware 1.3.23:** the header of the controls model (123)
  reads 114, with 48 registers instead of 24. The integration reads the
  registers behind the header as model 123.
- **Controls:** *Export limit* is `WMaxLimPct`, *Export limit enabled*
  is `WMaxLim_Ena`, *Inverter grid connection* is `Conn`. The ECU has no
  `WMaxLimPct_RvrtTms` and no `OutPFSet`, so those entities are not
  built.

Source: APsystems, SunSpec Modbus, rev 3.3
(https://global.apsystems.com/wp-content/uploads/2025/01/SunSpec-Modbus.pdf).
