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

## What does not work yet

On ECU-R firmware 1.3.23 the controls model (123, on/off and power
limit) is announced as model 114, so it is not found as 123 and has no
entities. The APsystems document puts 123 there with 24 registers,
and the registers behind the header are laid out like it, but this is
one report and writing to registers on a guess is not something we do.
If you have an APsystems gateway and want those controls, say so in
an issue with a register dump from 40180 on.

Source: APsystems, SunSpec Modbus, rev 3.3
(https://global.apsystems.com/wp-content/uploads/2025/01/SunSpec-Modbus.pdf).
