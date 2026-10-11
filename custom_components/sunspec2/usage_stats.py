"""A daily count of active installations, sent to the author's Matomo.

Once a day, one request tells the author's server that an installation is
alive. The request carries a random install ID, the integration version,
the Home Assistant version, the vendor profiles in use and the number of
entries. It never carries a host, a serial number, a model, a location or a
reading. ``CONF_USAGE_STATS`` switches it off; the README says what is sent.

One request covers the whole Home Assistant instance, however many entries
it has: the entries share the timer's result through ``hass.data``, and a
second ping within ``MIN_GAP`` is skipped.

A failure never reaches the caller. The request runs in the background, has
a short timeout, and ends in a debug line.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONF_USAGE_STATS
from .const import DOMAIN
from .const import VERSION

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

STATS_URL = "https://analytics.fs-sys.de/matomo.php"
STATS_SITE_ID = 6
PAGE_URL = "https://github.com/hilman2/ha-sunspec2"
"""Matomo wants a page URL; this is the one the counts are filed under."""

FIRST_PING_DELAY = timedelta(minutes=2)
"""Wait after setup, so the ping never competes with the first poll."""
PING_INTERVAL = timedelta(hours=24)
MIN_GAP = timedelta(hours=20)
"""Least time between two pings of one instance, across entries and restarts."""
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

STORAGE_KEY = f"{DOMAIN}.usage_stats"
STORAGE_REVISION = 1
_INSTALL_ID = re.compile(r"[0-9a-f]{16}")
"""The shape Matomo accepts for a visitor ID."""

_STATE_KEY = f"{DOMAIN}_usage_stats"


def usage_stats_enabled(entry: ConfigEntry) -> bool:
    """Whether an entry takes part; on unless the user turned it off."""
    return bool(entry.options.get(CONF_USAGE_STATS, True))


def async_start_usage_stats(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Schedule the daily ping for an entry, and stop it when the entry unloads."""
    if not usage_stats_enabled(entry):
        return

    async def _ping(_now: datetime) -> None:
        await async_ping(hass)

    entry.async_on_unload(async_call_later(hass, FIRST_PING_DELAY, _ping))
    entry.async_on_unload(async_track_time_interval(hass, _ping, PING_INTERVAL))


async def async_ping(hass: HomeAssistant) -> None:
    """Send one ping for the instance, unless one went out recently."""
    state: dict[str, Any] = hass.data.setdefault(_STATE_KEY, {})
    now = dt_util.utcnow()
    last = state.get("last")
    if last is not None and now - last < MIN_GAP:
        return
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and usage_stats_enabled(entry)
    ]
    if not entries:
        return
    # Set before the request, so a slow or failing server is asked once a
    # day and not once per entry.
    state["last"] = now
    vendors = sorted(
        {
            getattr(getattr(entry.runtime_data, "vendor", None), "slug", None) or "generic"
            for entry in entries
        }
    )
    install_id = await _async_install_id(hass)
    params = {
        "idsite": str(STATS_SITE_ID),
        "rec": "1",
        "apiv": "1",
        "send_image": "0",
        "_id": install_id,
        "rand": secrets.token_hex(4),
        "action_name": "sunspec2 ping",
        "url": f"{PAGE_URL}/ping?v={VERSION}&ha={HA_VERSION}&entries={len(entries)}",
        "e_c": "ping",
        "e_a": VERSION,
        "e_n": ",".join(vendors),
        "ua": f"sunspec2/{VERSION}",
    }
    try:
        session = async_get_clientsession(hass)
        async with session.get(STATS_URL, params=params, timeout=REQUEST_TIMEOUT) as response:
            _LOGGER.debug("Usage statistics ping answered %s", response.status)
    except (aiohttp.ClientError, TimeoutError, OSError) as err:
        _LOGGER.debug("Usage statistics ping failed: %s", err)


async def _async_install_id(hass: HomeAssistant) -> str:
    """The random ID of this installation; made once and kept."""
    store: Store[dict[str, Any]] = Store(hass, STORAGE_REVISION, STORAGE_KEY)
    data = await store.async_load()
    if isinstance(data, dict):
        stored = data.get("install_id")
        if isinstance(stored, str) and _INSTALL_ID.fullmatch(stored):
            return stored
    install_id = secrets.token_hex(8)
    await store.async_save({"install_id": install_id})
    return install_id
