# APsystems

The ECU-R and ECU-C gateways from APsystems answer SunSpec Modbus for
the micro inverters behind them (DS3, QS1, QT2, YC). Each micro
inverter is its own SunSpec device on its own unit id, so one config
entry per inverter. The integration recognises the gateway by the
manufacturer name it reports, nothing to configure.

## Before it works

1. Check the gateway supports it: ECU-R with a serial starting 2160 on
   firmware 1.3.7 or later, or 2162 on 2.0.2 or later; ECU-C 215...
   on C1.1.3 or later. The ECU-B does not.
2. In EMA Manager open *Workspace -> Modbus Configuration*, switch
   SunSpec Modbus on and set the address of each inverter. Those
   addresses are the unit ids to enter here.
3. Port 502 over TCP, or the RS485 port of the ECU.

## What SunSpec has

Models 1, 101 or 103, 111 to 113 and 123 (the controls), as the
APsystems document lists them. These become the usual sensors.

## What this integration adds

The DC side of each micro inverter is not in a SunSpec model. APsystems
keeps it in a block of its own, labelled model 114, of 8 float32
values each for DC voltage, current and power, starting at register
40214. The integration reads it from there and adds
*DC voltage input n*, *DC current input n* and *DC power input n*.

A micro inverter has fewer than 8 inputs (a DS3 has 2) and reports 0
for the rest, so only inputs 1 and 2 are on. The others are in the
entity list, off; turn on as many as your inverter has.

## Limiting the output

On ECU-R firmware 1.3.23 the header of the controls model (123) reads
114, with 48 registers instead of 24. The integration reads the
registers behind it as 123 all the same, so the power limit and the
on/off switch are there. They follow [write-controls.md](write-controls.md):
switched off until you enable the experimental export controls in the
integration's options.

What you get: *Export limit* in whole percent and *Export limit
enabled*. The ECU has no revert time and no power factor, so those
entities are not built. *Inverter grid connection* is in the entity
list, off by default.

An ECU-R shows an export limit of 30 % with the limit enabled before
anyone sets one, while the inverters run at full power. Take that for a
default, not for a limit.

The limit works: on eight DS3 behind one ECU-R, 10 % held every
inverter at about 65 W and 100 % released them. One ECU holds one
setting for all its inverters, so writing it to one unit id is enough,
and every unit id reads it back. The ECU polls the inverters over
Zigbee about every 5 minutes, so it takes that long for the first
inverter to follow and up to about 13 minutes for eight.

## Energy

*Energy produced today* (SunSpec point `WH`) is the yield of the
current day, not a lifetime total. It starts again at zero every night.

Source: APsystems, SunSpec Modbus, rev 3.3
(https://global.apsystems.com/wp-content/uploads/2025/01/SunSpec-Modbus.pdf).
