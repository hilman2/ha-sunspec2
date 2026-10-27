# FranklinWH

The integration recognises a FranklinWH aGate gateway and adds battery
controls and the manufacturer's extra measurements. The battery's
existing SunSpec entities and their history stay in place.

## Before it works

Enable Modbus TCP on the aGate and add its address to SunSpec Modbus.
The profile recognises manufacturer names beginning with `FranklinWH`
and model names beginning with `aGate`.

The register knowledge comes from readbacks of an aGate X on
V10R01B04D00 and V12R02B60D00, and from a user report of an aGate X on
V10R11B40D00. Other firmware versions need a comparison with the
device's diagnostics and measurements. The V10R11B40D00 site has the
SPAN panel's Modbus link switched off. Whether the extra sensors read
the same with it switched on is unknown.

## What you get

The profile reads the battery's capacity, charge level, state of
health, DC power and lifetime energy counters whenever those models
are present. It also shows battery activity as charging, discharging
or idle, derived from the reported DC power.

The extra sensors show local and remote solar power, home consumption,
the aGate's native operating mode and the reserve of the active mode.
They sit on a device of their own, named after the aGate with
**Gateway** appended. Up to 2026.10.12 they sat on SunSpec model
devices, mostly the solar module's. Their entity ids and history stay
as they were. If you had put that device in an area, assign the
Gateway device to it as well.

The reserve of the active mode is confirmed on V12R02B60D00 and
V10R11B40D00, and not yet checked on V10R01B04D00.

Home consumption combines two readings. **Home load** is about 1 W
fine on V10R01B04D00 and V12R02B60D00, but moved in 1000 W steps on
V10R11B40D00.
**Home load, coarse reading** moves in 100 W steps on V10. Home
consumption uses the coarse reading until the fine one has shown a
value between the 100 W steps, and the fine one from then on.

Additional comparisons start disabled in the entity list: the raw
home-consumption readings, solar totals from extension registers, the
self-consumption reserve and the reported TOU reserve. On V10R01B04D00
the TOU reserve repeated the self-consumption reserve. On V10R11B40D00
both repeated the active reserve in every mode.

## Set battery power

Use **Battery power target**:

| Value | Request |
|---|---|
| Positive watts, for example 2000 | Discharge at 2000 W |
| Negative watts, for example -1500 | Charge at 1500 W |
| 0 | Hold the battery at zero power while retaining control |

The allowed range comes from the device's charge and discharge ratings.
The profile converts ratings reported in 100 W units to watts.
A rating below 1000 W is treated as missing, and the device's active
power rating is used instead. If no usable rating remains, the power
control is not offered. The profile
sets watt mode and the target together, enables control if necessary,
then reads the settings back. A rejected write or mismatching readback
is reported as an error. Changing an active target keeps control enabled.

**Hold battery in standby** requests zero watts. **Release battery
control** disables the power command and clears the watt and percent
targets, so the aGate resumes its native operating mode. It does not
request zero power first.

These actions verify the settings, not the measured power response.
The battery can take several seconds to reach the target. Compare
**Battery activity** and the DC power measurement with the target.

## Firmware differences

V12R02B60D00 accepted the tested positive watt targets up to 5000 W.
It refused the tested negative targets at -500 W and -5 percent while
control was disabled. This does not establish that every charge request
is refused. A charging request can therefore fail on that firmware.

An aGate X with two aPower 2 batteries, reportedly on V10R11B40D00
while SunSpec identifies it as V10R01B04D00, reports charge/discharge
ratings in 100 W units. The profile converts 160 and 192 to 16000 W
and 19200 W for both sensors and control limits. Its allowed target
range is therefore -16000 to 19200 W. Ratings already in watts, such
as the reference aGate's 5000 W, retain their values.

The native operating mode and reserve extensions are read-only here.
On V12, writes were acknowledged without changing the settings. Values
outside the observed native modes, Backup, Self-Consumption and TOU,
remain unknown.

The reference tests found no reliable hardware command timeout. A watt
target can remain active after Home Assistant stops. **Release battery
control** is the action for handing control back. The profile does not
start a command or rewrite one periodically on setup or reload.

The reference project's virtual self-consumption, peak-shaving and TOU
loops are software automations. They are separate from the aGate's
native operating mode. Smart circuits, generator and V2L controls have
no verified Modbus register mapping in the source used here.

For slow actions, see [Model read pause](troubleshooting.md#write-actions-or-updates-take-several-seconds).

## Sources and testing

The profile adapts the register map and command sequences from
[david2069/franklinwh-modbus, commit 9afbb9f](https://github.com/david2069/franklinwh-modbus/tree/9afbb9f2c1b81cdea42784dd879e3fe4f75e892d),
under its MIT license. Attribution is included in this repository's
LICENSE. The reference's controller and watt-command tests provide
the command sequence. Its
[V12 snapshot](https://github.com/david2069/franklinwh-modbus/blob/9afbb9f2c1b81cdea42784dd879e3fe4f75e892d/docs/SUNSPEC_SNAPSHOT_2026-10-20_V12R02B60D00.md)
and [conformance results](https://github.com/david2069/franklinwh-modbus/blob/9afbb9f2c1b81cdea42784dd879e3fe4f75e892d/docs/SUNSPEC_M704_V12_CONFORMANCE_RESULTS.md)
provide the measured values and firmware limitations.

Our tests use selected snapshot values and a simulated Modbus unit
under the real async SunSpec client. They check decoded values, actual
register writes, refusal and ignored-write handling, power limits and
gateway locking. They do not establish that the standby power spikes
reported in [issue #145](https://github.com/hilman2/ha-sunspec2/issues/145)
are resolved. That still needs a comparison on the affected installation.
