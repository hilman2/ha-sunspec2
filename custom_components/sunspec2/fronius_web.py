"""The Fronius web interface: what a GEN24 tells its own web UI and not Modbus.

Temperatures, the battery's identity and cell temperature, where the
smart meter sits, the grid charging flags that ``ChaGriSet`` is
AND-linked with, whether Modbus control is allowed, and a reset of the
Modbus control state. None of that is in a SunSpec register. The
inverter serves it over plain HTTP on its LAN address, behind a Digest
login as the local "customer" user, and this module speaks that.

The protocol is not documented by Fronius. What is here comes from the
inverter's own web UI as the callifo/fronius_modbus integration traced
it: ``/api/status/common`` says which hash the customer secret uses,
``/api/commands/Login`` hands out the Digest challenge, and the
``/api/config`` and ``/api/components`` endpoints carry the data. Each
request is challenged and answered once; the inverter keeps no session.

What Home Assistant stores is not the password. It is the Digest
secret, the hash of ``customer:realm:password``, which the login
computes once when the password is entered. That secret logs in to
this inverter's web interface and nothing else, so a leaked Home
Assistant configuration does not leak a password the user may have
reused, and it is redacted from the diagnostics download.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN

_LOGGER: logging.Logger = logging.getLogger(__package__)

#: The local login of the inverter's web interface. Fixed by Fronius;
#: the "technician" login is a different secret and not used here.
WEB_USER = "customer"
WEB_TIMEOUT = aiohttp.ClientTimeout(total=10)
WEB_POLL_INTERVAL = timedelta(seconds=60)

STATUS_PATH = "/api/status/common"
LOGIN_PATH = "/api/commands/Login"
INVERTER_PATH = "/api/components/inverter/readable"
BATTERY_PATH = "/api/components/BatteryManagementSystem/readable"
METERS_PATH = "/api/components/PowerMeter/readable"
BATTERIES_CONFIG_PATH = "/api/config/batteries"
MODBUS_CONFIG_PATH = "/api/config/modbus"
MODBUS_RESET_PATH = "/api/commands/ModbusReset"

#: The two flags behind grid charging. ``ChaGriSet`` over Modbus is
#: AND-linked with the first; the web UI sets the second along with it.
CHARGE_FROM_GRID = "HYB_EVU_CHARGEFROMGRID"
CHARGE_FROM_AC = "HYB_BM_CHARGEFROMAC"

#: Where the first smart meter sits on the Modbus side, the inverter's
#: default "meter address". Meter n answers under this plus n - 1.
METER_UNIT_ID_BASE = 200


class FroniusWebError(Exception):
    """The web interface did not answer, or answered with an error."""


class FroniusWebAuthError(FroniusWebError):
    """The web interface refused the login."""


@dataclass(frozen=True)
class WebToken:
    """The Digest secret of the customer login, and what it was made with.

    Args:
        realm (str): The realm the inverter named in its challenge.
        secret (str): Hex digest of ``customer:realm:password``.
        algorithm (str): ``"md5"`` or ``"sha256"``, whichever the
            inverter said the customer secret is hashed with.
    """

    realm: str
    secret: str
    algorithm: str

    def as_dict(self) -> dict[str, str]:
        return {"realm": self.realm, "secret": self.secret, "algorithm": self.algorithm}

    @classmethod
    def from_dict(cls, data: Any) -> WebToken | None:
        """The token stored in the config entry, or None for anything else."""
        if not isinstance(data, dict):
            return None
        realm, secret, algorithm = data.get("realm"), data.get("secret"), data.get("algorithm")
        if isinstance(realm, str) and isinstance(secret, str) and algorithm in ("md5", "sha256"):
            return cls(realm, secret, algorithm)
        return None


def _hash(algorithm: str, text: str) -> str:
    if algorithm == "md5":
        return hashlib.md5(text.encode()).hexdigest()
    return hashlib.sha256(text.encode()).hexdigest()


def parse_challenge(header: str) -> dict[str, str]:
    """The fields of a ``Digest`` challenge header, quotes removed."""
    if not header:
        return {}
    body = re.sub(r"^\s*Digest\s+", "", header, flags=re.IGNORECASE)
    fields: dict[str, str] = {}
    for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,]*))', body):
        quoted, bare = match.group(2), match.group(3)
        fields[match.group(1)] = quoted if quoted is not None else bare.strip()
    return fields


def digest_uri(path: str, query: str) -> str:
    """What the digest covers as the uri.

    The inverter computes the login's digest over the bare path and
    every other request's over path and query. Getting this wrong is
    a 401 with no further hint.
    """
    if path == LOGIN_PATH or not query:
        return path
    return f"{path}?{query}"


def authorization_header(
    token: WebToken,
    method: str,
    uri: str,
    challenge: Mapping[str, str],
    nonce_count: int,
    cnonce: str,
) -> str:
    """The ``Authorization`` header that answers ``challenge``.

    Fronius' Digest is RFC 7616 with one deviation: whatever hashed the
    secret, the second hash and the response are SHA-256.
    """
    nonce = challenge["nonce"]
    qop = challenge.get("qop", "auth").split(",")[0].strip()
    nc = f"{nonce_count:08x}"
    ha2 = _hash("sha256", f"{method.upper()}:{uri}")
    response = _hash("sha256", f"{token.secret}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    parts = [
        f'username="{WEB_USER}"',
        f'realm="{challenge.get("realm", token.realm)}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
        f"qop={qop}",
        f"nc={nc}",
        f'cnonce="{cnonce}"',
    ]
    if challenge.get("opaque"):
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)


def first_device(payload: Any) -> dict[str, Any]:
    """Attributes and channels of the first device in a ``/readable`` answer."""
    nodes = _body_data(payload)
    device = next(iter(nodes.values()), None) if nodes else None
    if not isinstance(device, dict):
        return {"attributes": {}, "channels": {}}
    attributes = device.get("attributes")
    channels = device.get("channels")
    return {
        "attributes": attributes if isinstance(attributes, dict) else {},
        "channels": channels if isinstance(channels, dict) else {},
    }


def _body_data(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    body = payload.get("Body")
    if not isinstance(body, dict):
        meter = payload.get("meter")
        body = meter.get("Body") if isinstance(meter, dict) else None
    data = body.get("Data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else None


def parse_meters(payload: Any) -> list[dict[str, Any]]:
    """The smart meters the inverter lists, primary first.

    Each meter: ``address`` on the inverter's meter bus, ``unit_id``
    it answers under on Modbus, ``model``, ``primary``, ``location``
    (0 at the feed-in point, 1 in the consumption path), ``phases``.
    """
    nodes = _body_data(payload)
    if not nodes:
        return []
    meters: list[dict[str, Any]] = []
    for node in nodes.values():
        attributes = node.get("attributes") if isinstance(node, dict) else None
        if not isinstance(attributes, dict):
            continue
        address = _as_int(attributes.get("addr"))
        if address is None or address <= 0:
            continue
        model = str(attributes.get("model") or "").strip() or None
        location = _as_int(attributes.get("meter-location"))
        label = str(attributes.get("label") or "").strip().lower()
        meters.append(
            {
                "address": address,
                "unit_id": METER_UNIT_ID_BASE + address - 1,
                "model": model,
                "primary": label == "<primary>" or location == 0,
                "location": location,
                "phases": _as_int(attributes.get("phaseCnt")),
            }
        )
    meters.sort(key=lambda meter: (not meter["primary"], meter["address"]))
    return meters


def battery_identity(attributes: Mapping[str, Any]) -> dict[str, str | None]:
    """Manufacturer, model and serial of the battery, from its attributes.

    The GEN24 puts them in a JSON string under ``nameplate`` and some
    of them again as plain attributes; either place counts.
    """
    nameplate: dict[str, Any] = {}
    raw = attributes.get("nameplate")
    if isinstance(raw, dict):
        nameplate = raw
    elif isinstance(raw, str):
        try:
            import json

            parsed = json.loads(raw)
            nameplate = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            nameplate = {}

    def pick(*keys: str) -> str | None:
        for source in (attributes, nameplate):
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    return {
        "manufacturer": pick("manufacturer"),
        "model": pick("model", "DisplayName"),
        "serial": pick("serial"),
    }


def is_on(value: Any) -> bool | None:
    """A flag as the inverter spells it, or None for a value that is neither."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "on", "yes", "enabled"):
            return True
        if text in ("0", "false", "off", "no", "disabled"):
            return False
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


class FroniusWebClient:
    """One inverter's web interface, over Home Assistant's aiohttp session.

    Args:
        session (aiohttp.ClientSession): The session to send through.
        host (str): The inverter's address, the same the Modbus side uses.
        token (WebToken|None): The stored login secret.
        password (str|None): The customer password, to mint a token from.
            Only the options flow passes one; it is kept for the
            lifetime of this object and nowhere else.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        token: WebToken | None = None,
        password: str | None = None,
    ) -> None:
        self._session = session
        self._base = f"http://{host}"
        self._token = token
        self._password = password
        self._algorithm: str | None = None
        self._last_nonce = ""
        self._nonce_count = 0

    @property
    def token(self) -> WebToken | None:
        """The secret the last successful login used, minted or stored."""
        return self._token

    async def _secret_algorithm(self) -> str:
        """Which hash the inverter expects for the customer secret."""
        if self._algorithm is None:
            try:
                payload = await self._request("GET", STATUS_PATH, challenge=False)
            except FroniusWebError:
                payload = {}
            digest = (payload.get("authenticationOptions") or {}).get("digest") or {}
            version = digest.get(f"{WEB_USER}HashingVersion") if isinstance(digest, dict) else None
            self._algorithm = "md5" if version == 1 else "sha256"
        return self._algorithm

    async def _token_for(self, realm: str) -> WebToken:
        if self._password is not None:
            algorithm = await self._secret_algorithm()
            secret = _hash(algorithm, f"{WEB_USER}:{realm}:{self._password}")
            return WebToken(realm, secret, algorithm)
        if self._token is None:
            raise FroniusWebAuthError("no web interface login stored")
        return self._token

    def _next_nonce_count(self, nonce: str) -> int:
        if nonce == self._last_nonce:
            self._nonce_count += 1
        else:
            self._last_nonce = nonce
            self._nonce_count = 1
        return self._nonce_count

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        challenge: bool = True,
    ) -> dict[str, Any]:
        """Send, answer the Digest challenge once, and return the JSON body.

        ``challenge`` False sends once and takes the answer as it is;
        the status endpoint is read that way while working out which
        hash the secret needs, so a 401 there cannot recurse into
        another status read.
        """
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                method, url, params=params, json=json, timeout=WEB_TIMEOUT
            ) as response:
                if response.status != 401 or not challenge:
                    return await self._payload(path, response)
                header = (
                    response.headers.get("X-WWW-Authenticate")
                    or response.headers.get("WWW-Authenticate")
                    or ""
                )
                fields = parse_challenge(header)
            if "nonce" not in fields or "realm" not in fields:
                raise FroniusWebAuthError(f"{path}: refused without a Digest challenge")
            token = await self._token_for(fields["realm"])
            header = authorization_header(
                token,
                method,
                digest_uri(path, urlencode(params) if params else ""),
                fields,
                self._next_nonce_count(fields["nonce"]),
                secrets.token_hex(8),
            )
            async with self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers={"Authorization": header},
                timeout=WEB_TIMEOUT,
            ) as answered:
                payload = await self._payload(path, answered)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise FroniusWebError(f"{method} {path}: {exc}") from exc
        # A login that worked is the secret to keep, whether it came
        # from the password just entered or from storage.
        self._token = token
        return payload

    async def _payload(self, path: str, response: aiohttp.ClientResponse) -> dict[str, Any]:
        if response.status in (401, 403):
            raise FroniusWebAuthError(f"{path}: the inverter rejected the web interface login")
        if response.status >= 400:
            raise FroniusWebError(f"{path}: HTTP {response.status}")
        try:
            payload = await response.json(content_type=None)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    async def login(self) -> WebToken:
        """Log in and return the secret that worked.

        Raises:
            FroniusWebAuthError: The password or stored secret is wrong.
            FroniusWebError: The web interface did not answer.
        """
        await self._request("GET", LOGIN_PATH, params={"user": WEB_USER})
        if self._token is None:
            raise FroniusWebAuthError("the inverter answered the login without a challenge")
        return self._token

    async def read_inverter(self) -> dict[str, Any]:
        return first_device(await self._request("GET", INVERTER_PATH))

    async def read_battery(self) -> dict[str, Any]:
        return first_device(await self._request("GET", BATTERY_PATH))

    async def read_meters(self) -> list[dict[str, Any]]:
        return parse_meters(await self._request("GET", METERS_PATH))

    async def read_battery_config(self) -> dict[str, Any]:
        return await self._request("GET", BATTERIES_CONFIG_PATH)

    async def read_modbus_config(self) -> dict[str, Any]:
        return await self._request("GET", MODBUS_CONFIG_PATH)

    async def write_battery_config(self, fields: Mapping[str, Any]) -> None:
        """Set the given keys of the battery configuration; the rest stays."""
        await self._request("POST", BATTERIES_CONFIG_PATH, json=dict(fields))

    async def allow_modbus_control(self, allowed: bool) -> None:
        """Flip "inverter control via Modbus", leaving the rest of the Modbus setup as it is.

        Read, patch one flag, write back: the endpoint takes the whole
        Modbus configuration, and writing a made-up one would change
        the port, the SunSpec mode or the meter address underneath a
        running connection.
        """
        config = await self.read_modbus_config()
        slave = config.get("slave")
        if not isinstance(slave, dict):
            raise FroniusWebError(f"{MODBUS_CONFIG_PATH}: no Modbus configuration in the answer")
        ctr = slave.get("ctr")
        if not isinstance(ctr, dict):
            ctr = {}
            slave["ctr"] = ctr
        ctr["on"] = allowed
        await self._request("POST", MODBUS_CONFIG_PATH, json=config)

    async def reset_modbus(self) -> None:
        await self._request("POST", MODBUS_RESET_PATH)


async def async_mint_token(hass: HomeAssistant, host: str, password: str) -> WebToken:
    """Log in with the password once and return the secret to store.

    Raises:
        FroniusWebAuthError: The inverter rejected the password.
        FroniusWebError: The web interface did not answer.
    """
    client = FroniusWebClient(async_get_clientsession(hass), host, password=password)
    return await client.login()


@dataclass
class FroniusWebData:
    """One poll of the web interface.

    Args:
        inverter_temperature_c (float|None): Ambient temperature inside
            the inverter.
        battery (dict): ``manufacturer``, ``model`` and ``serial`` of the
            battery, each possibly None.
        battery_cell_temperature_c (float|None): The battery's cell
            temperature.
        battery_config (dict): ``/api/config/batteries`` as the inverter
            returned it.
        modbus (dict): The Modbus setup: ``mode``, ``port``,
            ``sunspec_mode``, ``control_allowed``, ``restriction_on``,
            ``restriction_ip``, ``meter_address``.
        meters (list): The smart meters, see ``parse_meters``.
    """

    inverter_temperature_c: float | None
    battery: dict[str, str | None]
    battery_cell_temperature_c: float | None
    battery_config: dict[str, Any]
    modbus: dict[str, Any]
    meters: list[dict[str, Any]]

    @property
    def primary_meter(self) -> dict[str, Any] | None:
        return self.meters[0] if self.meters else None

    def flag(self, key: str) -> bool | None:
        return is_on(self.battery_config.get(key))


def _modbus_summary(config: Mapping[str, Any]) -> dict[str, Any]:
    slave = config.get("slave")
    slave = slave if isinstance(slave, dict) else {}
    ctr = slave.get("ctr")
    ctr = ctr if isinstance(ctr, dict) else {}
    restriction = ctr.get("restriction")
    restriction = restriction if isinstance(restriction, dict) else {}
    return {
        "mode": slave.get("mode"),
        "port": _as_int(slave.get("port")),
        "sunspec_mode": slave.get("sunspecMode"),
        "meter_address": _as_int(slave.get("meterAddress")),
        "control_allowed": is_on(ctr.get("on")),
        "restriction_on": is_on(restriction.get("on")),
        "restriction_ip": restriction.get("ip"),
    }


class FroniusWebCoordinator(DataUpdateCoordinator[FroniusWebData]):
    """Polls the web interface once a minute and carries the writes.

    Separate from the Modbus coordinator: another transport, another
    failure mode, and a web interface that is down must not take the
    Modbus entities with it.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, host: str, token: WebToken) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} web {host}",
            update_interval=WEB_POLL_INTERVAL,
            config_entry=entry,
        )
        self.host = host
        self.client = FroniusWebClient(async_get_clientsession(hass), host, token)

    async def _async_update_data(self) -> FroniusWebData:
        try:
            inverter = await self._optional(self.client.read_inverter())
            battery = await self._optional(self.client.read_battery())
            battery_config = await self._optional(self.client.read_battery_config())
            modbus = await self._optional(self.client.read_modbus_config())
            meters = await self._optional(self.client.read_meters())
        except FroniusWebAuthError as exc:
            raise UpdateFailed(
                f"The web interface at {self.host} rejected the stored login; "
                f"enter the password again in the integration options ({exc})"
            ) from exc
        inverter = inverter or {"attributes": {}, "channels": {}}
        battery = battery or {"attributes": {}, "channels": {}}
        return FroniusWebData(
            inverter_temperature_c=_as_float(
                inverter["channels"].get("DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32")
            ),
            battery=battery_identity(battery["attributes"]),
            battery_cell_temperature_c=_as_float(
                battery["channels"].get("BAT_TEMPERATURE_CELL_F64")
            ),
            battery_config=battery_config or {},
            modbus=_modbus_summary(modbus or {}),
            meters=meters or [],
        )

    async def _optional(self, read: Any) -> Any:
        """One endpoint's answer, or None when it fails for anything but the login.

        A device without a battery has no battery endpoint, and an
        older firmware may lack another; the rest of the poll still
        counts. A rejected login stops the poll, because every other
        endpoint would fail the same way.
        """
        try:
            return await read
        except FroniusWebAuthError:
            raise
        except FroniusWebError as exc:
            _LOGGER.debug("Web interface at %s: %s", self.host, exc)
            return None

    async def async_set_charge_sources(self, grid: bool | None, ac: bool | None) -> None:
        """Set the grid charging flags the inverter's web UI has, then refresh."""
        fields: dict[str, bool] = {}
        if grid is not None:
            fields[CHARGE_FROM_GRID] = grid
        if ac is not None:
            fields[CHARGE_FROM_AC] = ac
        if fields:
            await self.client.write_battery_config(fields)
        await self.async_request_refresh()

    async def async_allow_grid_charging(self) -> None:
        """Both flags on: what "Charge from grid" needs on the web side."""
        await self.async_set_charge_sources(grid=True, ac=True)

    async def async_allow_modbus_control(self, allowed: bool) -> None:
        await self.client.allow_modbus_control(allowed)
        await self.async_request_refresh()

    async def async_reset_modbus(self) -> None:
        await self.client.reset_modbus()
        await self.async_request_refresh()
