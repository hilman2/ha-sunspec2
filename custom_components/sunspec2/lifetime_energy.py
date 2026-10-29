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
taken on, and between midnight and that first report today's energy is
still zero, which ``DailyCounterDisplay`` shows.

The daily counter is read through a gateway that now and then reports a
reading that does not fit a counter going up, so a reading is not taken at
face value when it is a drop or a large jump:

* A lower reading waits. If the next reading is back at the old level, the
  low one was a stray. If the next reading is higher than the low one, a
  new day has started and the old day is finished. A restart at zero is
  therefore booked once the counter climbs, also when Home Assistant was
  off while it restarted.
* A jump of more than ``MAX_STEP_WH`` waits as well, and counts once the
  next reading is at least as high, so one bad high reading never sticks.
  It starts a new day only if it is confirmed on another counter day: a
  Home Assistant that comes back at night, after missing the afternoon,
  finds the same day's counter, only higher.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
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

#: Revision of the stored shape. A payload with another revision is dropped
#: and counting starts again, rather than misread.
STORAGE_REVISION = 1

#: No micro inverter produces 50 kWh in a day: a higher counter is a bad reading.
MAX_DAILY_WH = 50_000.0

#: Largest rise between two polls that counts without a confirming reading.
#: A micro inverter makes about 100 Wh in the ECU's 5 minute rhythm, so this
#: leaves room for a Home Assistant restart of a few hours.
MAX_STEP_WH = 2_000.0

#: AC power above which a micro inverter counts as producing. At night the
#: ECU can go on showing 1 W with 0 V and 0 Hz.
PRODUCING_MIN_W = 5.0

#: Local hour from which a reading belongs to today's counter even while the
#: micro inverter produces nothing: by noon it has reported, and the counter
#: has started again, on any day it produced at all.
COUNTER_DAY_HOUR = 12


def _is_producing(wrapper: SunSpecModelWrapper, model_index: int) -> bool:
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
    pending: float | None = None
    """A reading that waits for the next one: a drop or a large jump."""

    @property
    def total(self) -> float | None:
        """Lifetime energy in Wh, or None before the first reading."""
        if self.last is None:
            return None
        return self.base + self.last

    def record(self, watt_hours: float, day: str) -> None:
        """Take one reading of the daily counter.

        Args:
            watt_hours (float): The reading, in Wh.
            day (str): The day the reading belongs to, as ``counter_day``
                gives it. A jump that is confirmed on another day than the
                last accepted reading is a new day whose value already is
                higher than yesterday's.
        """
        if not math.isfinite(watt_hours) or not 0 <= watt_hours <= MAX_DAILY_WH:
            return
        pending, self.pending = self.pending, None
        last = self.last
        if last is None:
            self._accept(watt_hours, day)
        elif pending is None:
            if last <= watt_hours <= last + MAX_STEP_WH:
                self._accept(watt_hours, day)
            else:
                self.pending = watt_hours
        elif pending > last:
            # A large jump: real if the counter kept going from there.
            if watt_hours < pending:
                self.record(watt_hours, day)
                return
            if self.last_day != day:
                self.base += last
            self._accept(watt_hours, day)
        elif watt_hours >= last:
            # Back at the old level: the drop was a stray reading.
            self.record(watt_hours, day)
        elif watt_hours > pending:
            # Running up again from the low value: a new day has started.
            self.base += last
            self._accept(watt_hours, day)
        else:
            self.pending = watt_hours

    def _accept(self, watt_hours: float, day: str) -> None:
        self.last = watt_hours
        self.last_day = day

    def as_dict(self) -> dict[str, Any]:
        """The stored form."""
        return {
            "revision": STORAGE_REVISION,
            "base": self.base,
            "last": self.last,
            "last_day": self.last_day,
            "pending": self.pending,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DailyCounterLedger:
        """Read the stored form; anything unreadable starts a fresh ledger."""
        if not data or data.get("revision") != STORAGE_REVISION:
            return cls()
        values = {key: data.get(key) for key in ("base", "last", "pending")}
        for value in values.values():
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                return cls()
        last_day = data.get("last_day")
        return cls(
            base=float(values["base"] or 0.0),
            last=None if values["last"] is None else float(values["last"]),
            last_day=last_day if isinstance(last_day, str) else None,
            pending=None if values["pending"] is None else float(values["pending"]),
        )


@dataclass
class DailyCounterDisplay:
    """Today's energy as the daily counter shows it: zero while it still holds yesterday.

    The counter keeps yesterday's value until the micro inverter first
    reports in the morning. A reading that has not moved since before
    midnight is that value, and today's energy is zero until the counter
    moves. Any movement ends it, the restart at zero as much as a climb.

    The test is equality with the last reading of the day before, not the
    time or the power. A counter that shows zero and then yesterday's value
    again would count that value twice in the long-term statistics, and
    only an exact match rules a flip like that out. A reading that differs
    is shown as it is, also when it is yesterday's counter seen for the
    first time after an outage.
    """

    last: float | None = None
    """The last reading."""
    last_date: str | None = None
    """Local date of the last reading, as ``YYYY-MM-DD``."""
    carried: float | None = None
    """Yesterday's value while the counter still shows it, else None."""

    def record(self, watt_hours: float, date: str) -> None:
        """Take one reading of the daily counter, taken on the local ``date``."""
        if self.last_date is not None and date != self.last_date and watt_hours == self.last:
            if watt_hours > 0:
                self.carried = watt_hours
        elif self.carried is not None and watt_hours != self.carried:
            self.carried = None
        self.last = watt_hours
        self.last_date = date

    def shown(self, watt_hours: float) -> float:
        """The value to show for the reading last recorded."""
        return 0.0 if self.carried is not None and watt_hours == self.carried else watt_hours

    def as_dict(self) -> dict[str, Any]:
        """The stored form."""
        return {
            "revision": STORAGE_REVISION,
            "last": self.last,
            "last_date": self.last_date,
            "carried": self.carried,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DailyCounterDisplay:
        """Read the stored form; anything unreadable starts afresh, showing what is read."""
        if not data or data.get("revision") != STORAGE_REVISION:
            return cls()
        last, carried = data.get("last"), data.get("carried")
        for value in (last, carried):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                return cls()
        last_date = data.get("last_date")
        return cls(
            last=None if last is None else float(last),
            last_date=last_date if isinstance(last_date, str) else None,
            carried=None if carried is None else float(carried),
        )


@dataclass
class DisplayExtraData(ExtraStoredData):
    """What Home Assistant keeps of the daily sensor across a restart."""

    display: DailyCounterDisplay

    def as_dict(self) -> dict[str, Any]:
        """The display state in its stored form."""
        return self.display.as_dict()


@dataclass
class LedgerExtraData(ExtraStoredData):
    """What Home Assistant keeps of the sensor across a restart."""

    ledger: DailyCounterLedger

    def as_dict(self) -> dict[str, Any]:
        """The ledger in its stored form."""
        return self.ledger.as_dict()


class CountedLifetimeEnergySensor(SunSpecEntity, SensorEntity, RestoreEntity):
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
        self._counted_model_id = model_id
        self._model_index = model_index
        self._key = key
        self._ledger = DailyCounterLedger()
        self._attr_unique_id = get_sunspec_unique_id(
            config_entry.entry_id, f"lifetime:{key}", model_id, model_index
        )

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
        wrapper = (
            self.coordinator.data.get(self._counted_model_id) if self.coordinator.data else None
        )
        if wrapper is None:
            return
        value = wrapper.getValue(self._key, self._model_index)
        # SunSpec reserves 0 of an acc32 for "not implemented", and the
        # library reads it as None. The counter sits at exactly that value
        # from its restart in the morning until the first watt-hour.
        reading = 0.0 if value is None else value
        if isinstance(reading, bool) or not isinstance(reading, (int, float)):
            return
        self._ledger.record(
            float(reading), counter_day(dt_util.now(), _is_producing(wrapper, self._model_index))
        )

    @property
    def native_value(self) -> float | None:
        """Lifetime energy in kWh."""
        total = self._ledger.total
        return None if total is None else round(total / 1000, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The point the total is counted from."""
        return {"counted_from": self._key}
