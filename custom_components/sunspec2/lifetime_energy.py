"""A lifetime total for devices that only report the yield of the current day.

An APsystems ECU serves the SunSpec point ``WH`` as a counter of one day.
SunSpec defines ``WH`` as a lifetime counter, so the generic sensor shows
the daily value as "Energy produced today" and there is nothing to bind to
for a total. This module adds the total by counting: it keeps the energy of
all finished days and the last daily value it accepted.

The counter does not restart at midnight. It keeps yesterday's value
through the night and starts again at zero when the micro inverter first
reports in the morning (#160). A reading therefore belongs to the day of
the counter, which ``counter_day`` tells, rather than to the date it was
taken on.

The same rhythm decides what "today" is. Energy produced today is the rise
of the counted total since midnight, not the counter itself: until the
counter restarts the total stands still and today reads 0, also on a day
without production, and a counter that runs on past midnight under a
midnight sun adds only what it makes after it. Home Assistant's statistics
count every rise of that sensor, and each rise is a rise of the total, so
the sensor can book nothing that the total does not book.

The daily counter is read through a gateway that now and then reports a
reading that does not fit a counter going up, and it serves that reading
for a whole collection round. So a reading is not taken at face value when
it is a drop or a large jump, and only a reading from a later round decides
about it (``CONFIRM_SECONDS``):

* A drop counts only from below ``RESTART_SHARE`` of the last value. Anything
  above that is noise and is left out: with polls running, a restart shows
  as a low reading, so a counter that merely ends under ``last`` is no new
  day. Only an outage of ``OUTAGE_SECONDS`` makes a smaller drop on a later
  counter day a possible restart nobody saw.
* A drop waits. If a later round is back at the old level, the low reading
  was a stray. If a later round is higher than the low one but still a
  drop, a new day has started and the old day is finished. A restart at
  zero is therefore booked once the counter climbs, also when Home
  Assistant was off while it restarted, and also when it has climbed past
  ``last`` by the time polls come back ``SETTLED_SECONDS`` later. A drop
  that stays flat for ``SETTLED_SECONDS`` after an outage that ended on a
  later counter day is a new day as well: an outage that ends in the evening
  finds a counter at rest.
* A jump of more than ``MAX_STEP_WH`` waits as well, and counts once a later
  round is at least as high, so a bad high reading of one round does not
  stick. It starts a new day only if the counter day has changed since
  ``last``: a Home Assistant that comes back at night, after missing the
  afternoon, finds the same day's counter, only higher.
* A smaller rise waits too while the micro inverter produces nothing, as
  there is nothing to count then.
* A smaller rise on a later counter day counts as a jump after an outage:
  today's counter has passed yesterday's value. Without an outage it is the
  same counter, which goes on past midnight while a midnight sun keeps the
  micro inverter reporting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import SunSpecDataUpdateCoordinator
from . import get_sunspec_unique_id
from .entity import SunSpecEntity
from .models import SunSpecModelWrapper
from .sensor import SunSpecSensor

#: Revision of the stored shape. A payload with another revision is dropped
#: and counting starts again, rather than misread.
STORAGE_REVISION = 1

#: No micro inverter produces 50 kWh in a day: a higher counter is a bad reading.
MAX_DAILY_WH = 50_000.0

#: Largest rise between two polls that counts without a confirming reading.
#: A micro inverter makes about 100 Wh in the ECU's 5 minute rhythm, so this
#: leaves room for a Home Assistant restart of a few hours.
MAX_STEP_WH = 2_000.0

#: Seconds after a reading that does not fit until a reading decides about
#: it. The ECU serves one value for its whole 5 minute round, so the polls
#: inside a round repeat a stray; this is one round and a margin for a micro
#: inverter that the ECU reaches a little later than the round before.
CONFIRM_SECONDS = 330

#: Seconds a low reading has to stand before it is taken for a restart without
#: having climbed from it. Six rounds: a stray goes with its round, and one
#: that outlasts the next round must not book the whole day a second time.
SETTLED_SECONDS = 1800

#: Seconds without any reading after which the counter may have restarted
#: unseen. Long enough that a restart of Home Assistant under a midnight sun,
#: with a counter that goes on past midnight, is no such outage.
OUTAGE_SECONDS = 3 * 3600

#: Share of the last value below which a drop can be the restart of the
#: counter. Home Assistant reads a drop of a total_increasing sensor the
#: same way.
RESTART_SHARE = 0.9

#: AC power above which a micro inverter counts as producing. At night the
#: ECU can go on showing 1 W with 0 V and 0 Hz.
PRODUCING_MIN_W = 5.0

#: Local hour from which a reading belongs to today's counter even while the
#: micro inverter produces nothing: by noon it has reported, and the counter
#: has started again, on any day it produced at all.
COUNTER_DAY_HOUR = 12


def is_producing(wrapper: SunSpecModelWrapper, model_index: int) -> bool:
    """Whether the inverter model in ``wrapper`` reports AC power above PRODUCING_MIN_W."""
    try:
        power = wrapper.getValue("W", model_index)
    except (KeyError, AttributeError, OverflowError):
        return False
    if isinstance(power, bool) or not isinstance(power, (int, float)):
        return False
    return power > PRODUCING_MIN_W


def counter_day(now: datetime, producing: bool) -> str:
    """The day the daily counter's reading belongs to.

    The counter starts again when the micro inverter first reports in the
    morning, so until then it still holds yesterday. A reading is today's
    while the micro inverter produces, or from noon on; before that it is
    yesterday's.

    Args:
        now (datetime): The moment of the reading, in local time.
        producing (bool): Whether the micro inverter produces at that moment.

    Returns:
        str: The day, as ``YYYY-MM-DD``.
    """
    day = now.date()
    if not producing and now.hour < COUNTER_DAY_HOUR:
        day -= timedelta(days=1)
    return day.isoformat()


@dataclass
class DailyCounterLedger:
    """Energy of one daily counter, in Wh."""

    base: float = 0.0
    """Energy of all finished days."""
    last: float | None = None
    """Last accepted value of the daily counter."""
    last_day: str | None = None
    """The day ``last`` belongs to, as ``counter_day`` gives it."""
    seen_at: float | None = None
    """When the counter was last read, as a Unix time, whether or not the
    reading was accepted."""
    seen_day: str | None = None
    """The counter day of that last reading."""
    outage: bool = False
    """Whether an outage may have hidden a restart of the counter. Ends with
    the next accepted reading."""
    flat_since: float | None = None
    """When an outage that may have hidden a restart ended, if the reading
    that ended it already fell on a later counter day than ``last``, as a
    Unix time. Ends with the next accepted reading."""
    pending: float | None = None
    """A reading that waits for a later round: a drop or a large jump."""
    pending_day: str | None = field(default=None, compare=False)
    """The day ``pending`` was read on. Not stored: after a restart the
    confirming reading's day stands alone."""
    pending_at: float | None = None
    """When ``pending`` was first read, as a Unix time."""
    date: str | None = None
    """The local date ``total_at_midnight`` belongs to, as ``YYYY-MM-DD``."""
    total_at_midnight: float | None = None
    """The total before the first reading on ``date``."""

    @property
    def total(self) -> float | None:
        """Lifetime energy in Wh, or None before the first reading."""
        if self.last is None:
            return None
        return self.base + self.last

    @property
    def today(self) -> float | None:
        """Energy since local midnight in Wh, or None before the first reading."""
        total = self.total
        if total is None or self.total_at_midnight is None:
            return None
        return total - self.total_at_midnight

    def record(self, watt_hours: float, now: datetime, producing: bool) -> None:
        """Take one reading of the daily counter.

        Args:
            watt_hours (float): The reading, in Wh.
            now (datetime): The moment of the reading, in local time.
            producing (bool): Whether the micro inverter produces at that
                moment, see ``counter_day``.
        """
        if not math.isfinite(watt_hours) or not 0 <= watt_hours <= MAX_DAILY_WH:
            return
        date = now.date().isoformat()
        if date != self.date:
            self.date = date
            self.total_at_midnight = self.total
        day = counter_day(now, producing)
        if self.last_day is not None and day < self.last_day:
            # A cloud before noon can take the power under PRODUCING_MIN_W
            # after the counter was already seen on its new day. The day
            # does not go back for that, or a confirmed jump would book
            # ``last`` a second time.
            day = self.last_day
        at = now.timestamp()
        # The counter restarts in the morning of ``date``. An outage can only
        # hide that if the last poll before it still read an earlier counter
        # day, so one that begins after the restart, or in the evening before
        # and ends the same evening, hides nothing.
        if (
            self.seen_at is not None
            and at - self.seen_at >= OUTAGE_SECONDS
            and self.seen_day != date
        ):
            self.outage = True
            if day != self.last_day:
                self.flat_since = at
        self.seen_at = at
        self.seen_day = day
        self._take(watt_hours, day, at, producing)
        if self.total_at_midnight is None:
            # The very first reading. It is today's energy unless the counter
            # still holds yesterday, and then today has not started.
            self.total_at_midnight = self.base if day == date else self.total

    def _take(self, watt_hours: float, day: str, at: float, producing: bool) -> None:
        """Accept a reading, hold it back, or let it decide about the held one."""
        last = self.last
        if last is None:
            self._accept(watt_hours, day)
            return
        pending, pending_day, pending_at = self.pending, self.pending_day, self.pending_at
        if pending is not None and pending_at is None:
            # Stored by a release that did not keep the time.
            self.pending_at = pending_at = at
        waited = 0.0 if pending_at is None else at - pending_at
        if (
            pending is not None
            and pending < last < watt_hours <= last + MAX_STEP_WH
            and producing
            and day != self.last_day
            and waited >= SETTLED_SECONDS
        ):
            # The restart was seen, and then the polls stopped until the
            # counter had climbed past yesterday's value. A stray is gone
            # within a round, so it cannot have waited this long, and this
            # is the first reading on the new counter day.
            self._release()
            self.base += last
            self._accept(watt_hours, day)
            return
        if self._fits(watt_hours, last, day, producing):
            self._release()
            self._accept(watt_hours, day)
        elif pending is None:
            self._hold(watt_hours, last, day, at)
        elif waited < CONFIRM_SECONDS:
            return
        elif pending > last:
            # A large jump: real if the counter kept going from there.
            self._release()
            if watt_hours < pending:
                self._hold(watt_hours, last, day, at)
                return
            # A counter that stands still belongs to the day it was seen
            # on. A confirming reading can fall on the next day, by noon or
            # the first production, and must not book ``last`` a second
            # time for that. A cloud can do the same the other way round.
            if pending_day is not None and (watt_hours == pending or pending_day > day):
                day = pending_day
            if self.last_day != day:
                self.base += last
            self._accept(watt_hours, day)
        elif watt_hours > last or not self._is_drop(watt_hours, last, day):
            # Back near the old level or above it: the low reading was a stray.
            self._release()
            self._hold(watt_hours, last, day, at)
        elif watt_hours > pending or (
            watt_hours == pending
            and self.flat_since is not None
            and at - self.flat_since >= SETTLED_SECONDS
        ):
            # Running up again from the low value, or at rest since an
            # outage ended on a later counter day: a new day has started.
            self._release()
            self.base += last
            self._accept(watt_hours, day)
        elif watt_hours < pending:
            self._release()
            self._hold(watt_hours, last, day, at)

    def _fits(self, watt_hours: float, last: float, day: str, producing: bool) -> bool:
        """Whether a reading goes on from ``last`` as a counter on one day does."""
        if watt_hours == last:
            return True
        if not producing or not last < watt_hours <= last + MAX_STEP_WH:
            return False
        return not self._outage_over(day)

    def _is_drop(self, watt_hours: float, last: float, day: str) -> bool:
        """Whether a reading below ``last`` can be the restart of the counter."""
        return watt_hours < RESTART_SHARE * last or self._outage_over(day)

    def _outage_over(self, day: str) -> bool:
        """Whether an outage can hide a restart of the counter on counter day ``day``."""
        return self.outage and day != self.last_day

    def _hold(self, watt_hours: float, last: float, day: str, at: float) -> None:
        """Keep a reading for a later round, unless it is a dip of no meaning."""
        if watt_hours > last or self._is_drop(watt_hours, last, day):
            self.pending = watt_hours
            self.pending_day = day
            self.pending_at = at

    def _release(self) -> None:
        self.pending = None
        self.pending_day = None
        self.pending_at = None

    def _accept(self, watt_hours: float, day: str) -> None:
        self.last = watt_hours
        self.last_day = day
        self.outage = False
        self.flat_since = None

    def as_dict(self) -> dict[str, Any]:
        """The stored form."""
        return {
            "revision": STORAGE_REVISION,
            "base": self.base,
            "last": self.last,
            "last_day": self.last_day,
            "seen_at": self.seen_at,
            "seen_day": self.seen_day,
            "outage": self.outage,
            "flat_since": self.flat_since,
            "pending": self.pending,
            "pending_at": self.pending_at,
            "date": self.date,
            "total_at_midnight": self.total_at_midnight,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DailyCounterLedger:
        """Read the stored form; anything unreadable starts a fresh ledger."""
        if not data or data.get("revision") != STORAGE_REVISION:
            return cls()
        keys = ("base", "last", "seen_at", "flat_since", "pending", "pending_at")
        values = {key: data.get(key) for key in (*keys, "total_at_midnight")}
        for value in values.values():
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                return cls()
        last_day, seen_day = data.get("last_day"), data.get("seen_day")
        date = data.get("date")
        seen_at, midnight = values["seen_at"], values["total_at_midnight"]
        flat_since, pending_at = values["flat_since"], values["pending_at"]
        return cls(
            base=float(values["base"] or 0.0),
            last=None if values["last"] is None else float(values["last"]),
            last_day=last_day if isinstance(last_day, str) else None,
            seen_at=None if seen_at is None else float(seen_at),
            seen_day=seen_day if isinstance(seen_day, str) else None,
            outage=data.get("outage") is True,
            flat_since=None if flat_since is None else float(flat_since),
            pending=None if values["pending"] is None else float(values["pending"]),
            pending_at=None if pending_at is None else float(pending_at),
            date=date if isinstance(date, str) else None,
            total_at_midnight=None if midnight is None else float(midnight),
        )


@dataclass
class LedgerExtraData(ExtraStoredData):
    """What Home Assistant keeps of the sensor across a restart."""

    ledger: DailyCounterLedger

    def as_dict(self) -> dict[str, Any]:
        """The ledger in its stored form."""
        return self.ledger.as_dict()


class _LedgerFed(SunSpecEntity, RestoreEntity):
    """A sensor that feeds a daily counter to a ledger it keeps across restarts.

    Each sensor keeps its own ledger, so disabling one of them in the entity
    settings leaves the other one complete.
    """

    _ledger: DailyCounterLedger
    _counter_model_id: int
    _counter_model_index: int
    _counter_key: str

    async def async_added_to_hass(self) -> None:
        """Restore the ledger, then take the reading the coordinator holds."""
        await super().async_added_to_hass()
        if (stored := await self.async_get_last_extra_data()) is not None:
            self._ledger = DailyCounterLedger.from_dict(stored.as_dict())
        self._record()

    @property
    def extra_restore_state_data(self) -> LedgerExtraData:
        """The ledger, kept across a restart."""
        return LedgerExtraData(self._ledger)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._record()
        super()._handle_coordinator_update()

    def _record(self) -> None:
        """Feed the daily counter's current value to the ledger."""
        # After a failed poll the coordinator still holds the last values.
        # Taking one of them again would pass for a later ECU round and
        # confirm the stray it may be.
        if self.coordinator.consecutive_failed_cycles:
            return
        wrapper = (
            self.coordinator.data.get(self._counter_model_id) if self.coordinator.data else None
        )
        if wrapper is None:
            return
        value = wrapper.getValue(self._counter_key, self._counter_model_index)
        # SunSpec reserves 0 of an acc32 for "not implemented", and the
        # library reads it as None. The counter sits at exactly that value
        # from its restart in the morning until the first watt-hour.
        reading = 0.0 if value is None else value
        if isinstance(reading, bool) or not isinstance(reading, (int, float)):
            return
        self._ledger.record(
            float(reading), dt_util.now(), is_producing(wrapper, self._counter_model_index)
        )


class CountedDailyEnergySensor(_LedgerFed, SunSpecSensor):
    """Energy produced today, counted from a daily energy point.

    The sensor keeps the unique id, name and ``total_increasing`` state class
    of the plain sensor of the point. It shows the rise of the counted total
    since midnight rather than the counter, see the module docstring.
    """

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        data: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, config_entry, data)
        self._ledger = DailyCounterLedger()
        self._counter_model_id = data["model_id"]
        self._counter_model_index = data["model_index"]
        self._counter_key = data["key"]

    @property
    def native_value(self) -> float | None:
        """Energy since local midnight in Wh."""
        return self._ledger.today


class CountedLifetimeEnergySensor(_LedgerFed, SensorEntity):
    """The lifetime total counted from a daily energy point.

    Counts from the moment the sensor is added, including what the daily
    counter shows at that moment. It is the sensor for the Energy dashboard
    on a device whose own counter starts again every day.
    """

    _attr_translation_key = "counted_lifetime_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: SunSpecDataUpdateCoordinator,
        config_entry: ConfigEntry,
        device_info: SunSpecModelWrapper,
        wrapper: SunSpecModelWrapper,
        prefix: str,
        model_id: int,
        model_index: int,
        key: str,
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            device_info,
            wrapper.getGroupMeta(),
            prefix=prefix,
            model_id=model_id,
        )
        self._ledger = DailyCounterLedger()
        self._counter_model_id = model_id
        self._counter_model_index = model_index
        self._counter_key = key
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, f"lifetime:{key}", model_id, model_index
        )

    @property
    def native_value(self) -> float | None:
        """Lifetime energy in kWh."""
        total = self._ledger.total
        return None if total is None else round(total / 1000, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The point the total is counted from."""
        return {"counted_from": self._counter_key}
