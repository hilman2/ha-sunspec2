# SAJ

This page is for SAJ R6 inverters (three-phase, 3 to 15 kW) with an
**eSolar AIO3** communication module. The AIO3 is the small module
plugged into the inverter's USB port that sends data to SAJ's cloud and
the Elekeeper app. Home Assistant talks to the module, and the module
answers for the inverter. The integration recognises an R6 by the
manufacturer and model name it reports, so there is nothing to select.

## Before it works

1. Give the AIO3 a fixed IP address in your router. It connects over
   Wi-Fi or Ethernet, the same way it reaches SAJ's cloud.
2. Use port `502` and unit ID `1`. Modbus is on from the factory, so
   there is no setting to change.
3. If the AIO3 sits on a separate network or VLAN for cloud devices,
   allow Home Assistant to reach it on TCP port 502.
4. Turn on **Inverter powers down when idle** in the options. The AIO3
   is powered by the panels, so it disappears from the network every
   night. With the option on, that is not reported as a fault.
5. Let **nothing else talk to the AIO3 over Modbus** while the
   integration runs. It stops answering for a few minutes after a burst
   of requests, and a second program polling it makes that likely.

## What you get

The usual sensors: AC power, voltage and current per phase, frequency,
temperatures, operating state and the energy produced.

Beyond that, the integration reads two values SAJ keeps in a place of
its own:

| Sensor | What it shows |
|---|---|
| **Energy produced today** | What the inverter has produced since midnight |
| **Energy produced this month** | What it has produced since the 1st |
| **DC voltage input 1, 2** | Voltage on each of the two solar inputs (MPPT) |
| **DC current input 1, 2** | Current on each input |
| **DC power input 1, 2** | Power on each input |

The input values are a few minutes behind the other sensors. The AIO3
refreshes them only when it prepares its next upload to SAJ's cloud.
Use them for comparing the two inputs, not for second-by-second
control.

## Things to know

- **The energy counter starts again on 1 January.** On the R6 the
  energy sensor counts the current year, not the inverter's lifetime.
  The Energy dashboard handles that. On the first morning of the year
  the sensor holds its old value for a poll or two before it accepts
  the new low one.
- **Model 160 (MPPT) shows zeros.** The AIO3 reports this model but
  leaves all its values at 0. The DC input sensors above take its
  place, so you can untick model 160 in the options.
- **The DC voltage of the inverter sensor is one input's voltage.** DC
  power and DC current there are the sum of both inputs.
- **Other SAJ inverters** (R5, Sununo, the H1 and H2 hybrids) are not
  covered by these extra sensors, because their registers are not
  known to be the same. They still get the usual sensors if their
  communication module speaks SunSpec. A report with a register dump is
  welcome.
