"""The Fronius web interface: the Digest login, the parsers, the coordinator and its entities.

The inverter is played by Home Assistant's aiohttp mocker. Every
authenticated endpoint answers 401 with a challenge first and the
payload to the signed request after, the way a GEN24 does.
"""

import hashlib
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.sunspec2.const import CONF_ENABLED_MODELS
from custom_components.sunspec2.const import CONF_FRONIUS_WEB_FORGET
from custom_components.sunspec2.const import CONF_FRONIUS_WEB_PASSWORD
from custom_components.sunspec2.const import CONF_FRONIUS_WEB_TOKEN
from custom_components.sunspec2.const import CONF_SCAN_INTERVAL
from custom_components.sunspec2.fronius_web import FroniusWebAuthError
from custom_components.sunspec2.fronius_web import FroniusWebClient
from custom_components.sunspec2.fronius_web import WebToken
from custom_components.sunspec2.fronius_web import authorization_header
from custom_components.sunspec2.fronius_web import battery_identity
from custom_components.sunspec2.fronius_web import digest_uri
from custom_components.sunspec2.fronius_web import parse_challenge
from custom_components.sunspec2.fronius_web import parse_meters
from custom_components.sunspec2.fronius_web_entities import MeterLocationSensor
from custom_components.sunspec2.fronius_web_entities import ModbusResetButton
from custom_components.sunspec2.fronius_web_entities import WebSwitch
from custom_components.sunspec2.fronius_web_entities import WebTemperatureSensor
from custom_components.sunspec2.storage_modes import StorageModeSelect

from . import create_mock_sunspec_config_entry
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG_STEP_1
from .const import MOCK_CONFIG_WRITE

HOST = "test_host"
BASE = f"http://{HOST}"
CHALLENGE = 'Digest realm="Fronius", nonce="n0nce", qop="auth", opaque="0paque"'
TOKEN = WebToken("Fronius", "a" * 64, "sha256")

INVERTER = {
    "Body": {
        "Data": {
            "0": {
                "attributes": {},
                "channels": {"DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 41.5},
            }
        }
    }
}
BATTERY: dict[str, Any] = {
    "Body": {
        "Data": {
            "0": {
                "attributes": {
                    "nameplate": '{"manufacturer": "BYD", "model": "HVS 10.2", "serial": "S123"}'
                },
                "channels": {"BAT_TEMPERATURE_CELL_F64": 23.25},
            }
        }
    }
}
METERS = {
    "Body": {
        "Data": {
            "0": {
                "attributes": {
                    "addr": "1",
                    "model": "Smart Meter TS 65A-3",
                    "label": "<primary>",
                    "meter-location": "0",
                    "phaseCnt": "3",
                }
            },
            "1": {
                "attributes": {
                    "addr": "2",
                    "model": "Smart Meter TS 65A-3",
                    "label": "Heat pump",
                    "meter-location": "1",
                    "phaseCnt": "1",
                }
            },
        }
    }
}
BATTERIES = {"HYB_EVU_CHARGEFROMGRID": False, "HYB_BM_CHARGEFROMAC": True, "BAT_M0_SOC_MIN": 10}
MODBUS = {
    "slave": {
        "mode": "tcp",
        "port": 502,
        "sunspecMode": "int",
        "meterAddress": 200,
        "ctr": {"on": True, "restriction": {"on": False}},
    }
}


def challenged(payload, *, status=200):
    """An endpoint that challenges every first request and answers the signed one."""
    calls = {"n": 0}

    async def side_effect(method, url, data):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            return AiohttpClientMockResponse(
                method, url, status=401, headers={"X-WWW-Authenticate": CHALLENGE}
            )
        return AiohttpClientMockResponse(method, url, status=status, json=payload)

    return side_effect


def _mock_web(aioclient_mock, *, status=200):
    aioclient_mock.get(
        f"{BASE}/api/components/inverter/readable", side_effect=challenged(INVERTER, status=status)
    )
    aioclient_mock.get(
        f"{BASE}/api/components/BatteryManagementSystem/readable",
        side_effect=challenged(BATTERY, status=status),
    )
    aioclient_mock.get(
        f"{BASE}/api/components/PowerMeter/readable", side_effect=challenged(METERS, status=status)
    )
    aioclient_mock.get(
        f"{BASE}/api/config/batteries", side_effect=challenged(BATTERIES, status=status)
    )
    aioclient_mock.get(f"{BASE}/api/config/modbus", side_effect=challenged(MODBUS, status=status))
    aioclient_mock.post(f"{BASE}/api/config/batteries", side_effect=challenged({}))
    aioclient_mock.post(f"{BASE}/api/config/modbus", side_effect=challenged({}))
    aioclient_mock.post(f"{BASE}/api/commands/ModbusReset", side_effect=challenged({}))


def _posts(aioclient_mock, path):
    """The JSON bodies posted to ``path``, in order."""
    return [
        call[2]
        for call in aioclient_mock.mock_calls
        if call[0].lower() == "post" and call[1].path == path
    ]


def _entities(hass, platform, cls):
    component = hass.data.get("entity_components", {}).get(platform)
    return [e for e in component.entities if isinstance(e, cls)] if component else []


# ---------- the Digest login ---------------------------------------------------


def test_the_challenge_header_is_parsed_quotes_and_all():
    assert parse_challenge(CHALLENGE) == {
        "realm": "Fronius",
        "nonce": "n0nce",
        "qop": "auth",
        "opaque": "0paque",
    }
    assert parse_challenge("") == {}


def test_the_login_digest_covers_the_bare_path_and_others_the_query_too():
    assert digest_uri("/api/commands/Login", "user=customer") == "/api/commands/Login"
    assert digest_uri("/api/config/batteries", "") == "/api/config/batteries"
    assert digest_uri("/api/components/inverter/readable", "a=1") == (
        "/api/components/inverter/readable?a=1"
    )


def test_the_response_is_sha256_whatever_hashed_the_secret():
    token = WebToken("Fronius", "s3cret", "md5")
    header = authorization_header(
        token, "get", "/api/config/batteries", parse_challenge(CHALLENGE), 1, "c"
    )
    ha2 = hashlib.sha256(b"GET:/api/config/batteries").hexdigest()
    expected = hashlib.sha256(f"s3cret:n0nce:00000001:c:auth:{ha2}".encode()).hexdigest()
    assert header.startswith("Digest ")
    assert f'response="{expected}"' in header
    assert 'username="customer"' in header
    assert 'realm="Fronius"' in header
    assert "nc=00000001" in header
    assert 'opaque="0paque"' in header


async def test_login_answers_the_challenge_and_mints_the_secret(hass, aioclient_mock):
    """Hashing version 1 means an MD5 secret; the login's uri has no query."""
    aioclient_mock.get(
        f"{BASE}/api/status/common",
        json={"authenticationOptions": {"digest": {"customerHashingVersion": 1}}},
    )
    aioclient_mock.get(f"{BASE}/api/commands/Login?user=customer", side_effect=challenged({}))

    client = FroniusWebClient(async_get_clientsession(hass), HOST, password="secret")
    token = await client.login()

    assert token == WebToken("Fronius", hashlib.md5(b"customer:Fronius:secret").hexdigest(), "md5")
    signed = aioclient_mock.mock_calls[-1]
    assert signed[1].path == "/api/commands/Login"
    assert 'uri="/api/commands/Login"' in signed[3]["Authorization"]


async def test_a_rejected_password_is_an_auth_error(hass, aioclient_mock):
    aioclient_mock.get(f"{BASE}/api/status/common", json={})
    aioclient_mock.get(
        f"{BASE}/api/commands/Login?user=customer", side_effect=challenged({}, status=401)
    )
    client = FroniusWebClient(async_get_clientsession(hass), HOST, password="wrong")
    with pytest.raises(FroniusWebAuthError):
        await client.login()


async def test_a_stored_secret_logs_in_without_a_password(hass, aioclient_mock):
    aioclient_mock.get(f"{BASE}/api/config/batteries", side_effect=challenged(BATTERIES))
    client = FroniusWebClient(async_get_clientsession(hass), HOST, token=TOKEN)
    assert await client.read_battery_config() == BATTERIES
    assert "Authorization" in aioclient_mock.mock_calls[-1][3]


# ---------- the parsers ------------------------------------------------------


def test_meters_are_listed_primary_first_with_their_modbus_unit_ids():
    meters = parse_meters(METERS)
    assert [meter["unit_id"] for meter in meters] == [200, 201]
    assert meters[0]["primary"] and meters[0]["location"] == 0 and meters[0]["phases"] == 3
    assert not meters[1]["primary"] and meters[1]["location"] == 1
    assert parse_meters({}) == []


def test_the_battery_identity_comes_from_the_nameplate_json():
    assert battery_identity(BATTERY["Body"]["Data"]["0"]["attributes"]) == {
        "manufacturer": "BYD",
        "model": "HVS 10.2",
        "serial": "S123",
    }
    assert battery_identity({"model": "Battery", "serial": " 1 "}) == {
        "manufacturer": None,
        "model": "Battery",
        "serial": "1",
    }


# ---------- the coordinator and its entities ----------------------------------


async def _web_entry(hass, options=None):
    """A Fronius entry with the stored web login, or with ``options`` as given."""
    if options is None:
        options = {CONF_FRONIUS_WEB_TOKEN: TOKEN.as_dict()}
    entry = create_mock_sunspec_config_entry(hass, data=MOCK_CONFIG_WRITE, options=options)
    await setup_mock_sunspec_config_entry(hass, config_entry=entry)
    return entry


async def test_a_stored_login_adds_the_web_entities(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    _mock_web(aioclient_mock)
    entry = await _web_entry(hass)
    web = entry.runtime_data.web
    assert web is not None and web.last_update_success

    temps = {s.translation_key: s for s in _entities(hass, "sensor", WebTemperatureSensor)}
    assert temps["inverter_temperature"].native_value == 41.5
    assert temps["battery_cell_temperature"].native_value == 23.25
    assert temps["battery_cell_temperature"].extra_state_attributes == {
        "manufacturer": "BYD",
        "model": "HVS 10.2",
        "serial": "S123",
    }
    location = _entities(hass, "sensor", MeterLocationSensor)[0]
    assert location.native_value == "feed_in_point"
    assert [m["unit_id"] for m in location.extra_state_attributes["meters"]] == [200, 201]

    switches = {s.translation_key: s for s in _entities(hass, "switch", WebSwitch)}
    assert switches["battery_charge_from_grid"].is_on is False
    assert switches["battery_charge_from_ac"].is_on is True
    assert switches["modbus_control_allowed"].is_on is True
    assert len(_entities(hass, "button", ModbusResetButton)) == 1


async def test_without_a_stored_login_there_is_no_web_side(hass, sunspec_fronius_client_mock):
    entry = await _web_entry(hass, options={})
    assert entry.runtime_data.web is None
    assert _entities(hass, "sensor", WebTemperatureSensor) == []
    assert _entities(hass, "switch", WebSwitch) == []
    assert _entities(hass, "button", ModbusResetButton) == []


async def test_a_login_the_inverter_rejects_leaves_the_modbus_side_alone(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    _mock_web(aioclient_mock, status=401)
    entry = await _web_entry(hass)
    web = entry.runtime_data.web
    assert web is not None and not web.last_update_success
    assert "rejected the stored login" in str(web.last_exception)
    # The entry loaded and the Modbus entities are there.
    assert entry.runtime_data.last_update_success
    assert len(_entities(hass, "select", StorageModeSelect)) == 1
    temps = _entities(hass, "sensor", WebTemperatureSensor)
    assert temps and all(not t.available for t in temps)


async def test_the_grid_charging_switch_sets_both_flags(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    _mock_web(aioclient_mock)
    await _web_entry(hass)
    switches = {s.translation_key: s for s in _entities(hass, "switch", WebSwitch)}

    await switches["battery_charge_from_grid"].async_turn_on()
    assert _posts(aioclient_mock, "/api/config/batteries")[-1] == {
        "HYB_EVU_CHARGEFROMGRID": True,
        "HYB_BM_CHARGEFROMAC": True,
    }

    await switches["battery_charge_from_grid"].async_turn_off()
    assert _posts(aioclient_mock, "/api/config/batteries")[-1] == {"HYB_EVU_CHARGEFROMGRID": False}


async def test_charge_from_grid_mode_enables_the_web_flags_first(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    """ChaGriSet is AND-linked with the web flag; the mode sets the flag before the registers."""
    _mock_web(aioclient_mock)
    entry = await _web_entry(hass)
    select = _entities(hass, "select", StorageModeSelect)[0]

    with patch.object(entry.runtime_data.api, "async_write_points") as write:
        await select.async_select_option("charge_from_grid")

    assert _posts(aioclient_mock, "/api/config/batteries")[-1] == {
        "HYB_EVU_CHARGEFROMGRID": True,
        "HYB_BM_CHARGEFROMAC": True,
    }
    assert write.call_count == 2


async def test_modbus_control_is_patched_into_the_existing_setup(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    """Only the one flag changes; port, mode and meter address go back as they came."""
    _mock_web(aioclient_mock)
    await _web_entry(hass)
    switches = {s.translation_key: s for s in _entities(hass, "switch", WebSwitch)}

    await switches["modbus_control_allowed"].async_turn_off()

    posted = _posts(aioclient_mock, "/api/config/modbus")[-1]
    assert posted["slave"]["ctr"]["on"] is False
    assert posted["slave"]["port"] == 502
    assert posted["slave"]["sunspecMode"] == "int"
    assert posted["slave"]["meterAddress"] == 200


async def test_the_reset_button_posts_the_command(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    _mock_web(aioclient_mock)
    await _web_entry(hass)
    await _entities(hass, "button", ModbusResetButton)[0].async_press()
    # The challenged request and the signed one, both without a body.
    assert _posts(aioclient_mock, "/api/commands/ModbusReset") == [None, None]


# ---------- the options flow -------------------------------------------------


async def _model_options(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "host_options"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_STEP_1
    )
    assert result["step_id"] == "model_options"
    return result


async def test_the_password_becomes_a_stored_secret_and_is_not_kept(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    _mock_web(aioclient_mock)
    entry = await _web_entry(hass, options={})
    result = await _model_options(hass, entry)
    fields = {key.schema for key in result["data_schema"].schema}
    assert CONF_FRONIUS_WEB_PASSWORD in fields
    assert CONF_FRONIUS_WEB_FORGET not in fields

    with patch(
        "custom_components.sunspec2.config_flow.async_mint_token", return_value=TOKEN
    ) as mint:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_ENABLED_MODELS: ["160"],
                CONF_SCAN_INTERVAL: 10,
                CONF_FRONIUS_WEB_PASSWORD: "secret",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mint.assert_called_once_with(hass, HOST, "secret")
    assert entry.options[CONF_FRONIUS_WEB_TOKEN] == TOKEN.as_dict()
    assert CONF_FRONIUS_WEB_PASSWORD not in entry.options
    assert entry.runtime_data.web is not None


async def test_a_rejected_password_shows_the_error(hass, sunspec_fronius_client_mock):
    entry = await _web_entry(hass, options={})
    result = await _model_options(hass, entry)

    with patch(
        "custom_components.sunspec2.config_flow.async_mint_token",
        side_effect=FroniusWebAuthError("no"),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_ENABLED_MODELS: ["160"],
                CONF_SCAN_INTERVAL: 10,
                CONF_FRONIUS_WEB_PASSWORD: "wrong",
            },
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "web_auth_failed"}
    assert CONF_FRONIUS_WEB_TOKEN not in entry.options


async def test_forgetting_the_login_drops_the_secret(
    hass, sunspec_fronius_client_mock, aioclient_mock
):
    _mock_web(aioclient_mock)
    entry = await _web_entry(hass)
    result = await _model_options(hass, entry)
    assert CONF_FRONIUS_WEB_FORGET in {key.schema for key in result["data_schema"].schema}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_ENABLED_MODELS: ["160"],
            CONF_SCAN_INTERVAL: 10,
            CONF_FRONIUS_WEB_FORGET: True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_FRONIUS_WEB_TOKEN not in entry.options
    assert entry.runtime_data.web is None


async def test_the_password_field_is_not_offered_for_other_vendors(hass, sunspec_write_client_mock):
    entry = await _web_entry(hass, options={})
    result = await _model_options(hass, entry)
    assert CONF_FRONIUS_WEB_PASSWORD not in {key.schema for key in result["data_schema"].schema}
