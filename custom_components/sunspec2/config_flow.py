"""Adds config flow for SunSpec."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import probatio
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .api import SETUP_TIMEOUT
from .api import SunSpecApiClient
from .const import CONF_BAUDRATE
from .const import CONF_CAPTURE_RAW
from .const import CONF_ENABLED_MODELS
from .const import CONF_FRONIUS_WEB_FORGET
from .const import CONF_FRONIUS_WEB_PASSWORD
from .const import CONF_FRONIUS_WEB_TOKEN
from .const import CONF_HOST
from .const import CONF_MAX_AC_POWER_KW
from .const import CONF_MODEL_READ_DELAY
from .const import CONF_PARITY
from .const import CONF_PORT
from .const import CONF_PREFIX
from .const import CONF_REARM_ON_CHANGE
from .const import CONF_SCAN_DELAY
from .const import CONF_SCAN_INTERVAL
from .const import CONF_SERIAL_PORT
from .const import CONF_STANDBY_WHEN_IDLE
from .const import CONF_TRANSPORT
from .const import CONF_UNIT_ID
from .const import DEFAULT_BAUDRATE
from .const import DEFAULT_MODEL_READ_DELAY_SECONDS
from .const import DEFAULT_MODELS
from .const import DEFAULT_SCAN_DELAY_SECONDS
from .const import DOMAIN
from .const import MAX_MODEL_READ_DELAY_SECONDS
from .const import MAX_SCAN_DELAY_SECONDS
from .const import MIN_MODEL_READ_DELAY_SECONDS
from .const import MIN_SCAN_DELAY_SECONDS
from .const import MIN_SCAN_INTERVAL_SECONDS
from .const import NAMEPLATE_FILTER_HEADROOM
from .const import PARITY_EVEN
from .const import PARITY_NONE
from .const import TRANSPORT_RTU
from .const import TRANSPORT_TCP
from .const import default_models_for
from .coordinator import SCAN_INTERVAL
from .coordinator import SunSpecDataUpdateCoordinator
from .discovery import SunSpecCandidate
from .discovery import async_discover_sunspec_candidates
from .discovery import async_get_default_subnet
from .errors import DeviceError
from .errors import ProtocolError
from .errors import TransientError
from .errors import TransportError
from .fronius_web import FroniusWebAuthError
from .fronius_web import FroniusWebError
from .fronius_web import async_mint_token
from .logger import redact_options
from .model_labels import sunspec_model_labels
from .vendors.sma import SUNSPEC_UNIT_ID as SMA_SUNSPEC_UNIT_ID

#: SMA Solar Technology's MAC prefix, the one DHCP discovery matches on.
SMA_OUI = "0015bb"

_LOGGER: logging.Logger = logging.getLogger(__package__)

# The web interface password is typed once and turned into the Digest
# secret in the same step. The field never has a stored value to show.
_PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)

# The optional peak AC power field has to be a selector. probatio cannot
# serialise a plain callable such as `probatio.Coerce(float)` or a
# custom validator when the frontend requests the options form: the
# POST to config/config_entries/options/flow raises and the form never
# renders.
_MAX_AC_POWER_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0.1,
        step=0.1,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="kW",
    )
)


# Shared by the setup form and the options form. The lower bound is not
# optional: a saved 0 stops polling silently and for good, and a
# negative value hot-loops it. See MIN_SCAN_INTERVAL_SECONDS.
_SCAN_INTERVAL_VALIDATOR = probatio.All(
    selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_SCAN_INTERVAL_SECONDS,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    ),
    probatio.Coerce(int),
)


def _model_selector(labels: dict[str, str]) -> selector.SelectSelector:
    """Select detected models without expanding every model into a checkbox."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=value, label=label)
                for value, label in labels.items()
            ],
            multiple=True,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _suggested_peak_power_kw(detected_kw: float | None) -> float | None:
    """Return the value to pre-fill the peak-power field with, headroom included.

    Returns ``None`` when no nameplate was detected.
    """
    if not detected_kw:
        return None
    # The plausibility filter grants NAMEPLATE_FILTER_HEADROOM on a
    # nameplate it detected and nothing on a number the user typed. The
    # frontend echoes a ``suggested_value`` back on submit, so a field
    # pre-filled with the raw nameplate turns the intended 1.2x ceiling
    # into a 1.0x one as soon as the form is submitted unchanged, and a
    # ceiling at exactly the nameplate makes ``DCW`` and ``VA`` read
    # "unknown" for most of a sunny day (#45). The value is rounded to
    # the selector's 0.1 kW step so the form accepts it back.
    return round(detected_kw * NAMEPLATE_FILTER_HEADROOM, 1)


# Pacing between models during the SunSpec scan (#17), see
# CONF_SCAN_DELAY in const.py. The delay is an option because the right
# value is a property of the device and the wiring: a KACO Powador on
# 100 Mbit wants the pacing, a modern inverter on gigabit does not. The
# selector is a BOX so the value is typed exactly, and it is capped at
# MAX_SCAN_DELAY_SECONDS because every 0.1 costs 0.1 per model on each
# scan of the tree.
_SCAN_DELAY_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=MIN_SCAN_DELAY_SECONDS,
        max=MAX_SCAN_DELAY_SECONDS,
        step=0.05,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="s",
    )
)

_MODEL_READ_DELAY_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=MIN_MODEL_READ_DELAY_SECONDS,
        max=MAX_MODEL_READ_DELAY_SECONDS,
        step=0.05,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="s",
    )
)


def set_connection_error(
    errors: dict[str, str],
    host: str | None,
    port: int | None,
    unit_id: int | None,
    err: Exception,
) -> None:
    """Map backend failures to user-visible config flow errors."""
    if isinstance(err, (TransientError, TimeoutError)):
        errors["base"] = "timeout"
        _LOGGER.warning(
            "Timeout while connecting to host %s:%s unit %s",
            host,
            port,
            unit_id,
        )
        return

    if isinstance(err, (TransportError, ProtocolError)):
        errors["base"] = "connection"
        _LOGGER.warning(
            "Connection failed for host %s:%s unit %s: %s",
            host,
            port,
            unit_id,
            err,
        )
        return

    if isinstance(err, DeviceError):
        errors["base"] = "device_error"
        _LOGGER.warning(
            "Device error for host %s:%s unit %s: %s",
            host,
            port,
            unit_id,
            err,
        )
        return

    errors["base"] = "device_error"
    _LOGGER.exception(
        "Unexpected error while connecting to host %s:%s unit %s",
        host,
        port,
        unit_id,
        exc_info=err,
    )


class SunSpecFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for sunspec."""

    # Every entry of this integration was created at version 2, so there is
    # no async_migrate_entry. A new version needs one.
    VERSION = 2

    def __init__(self) -> None:
        """Initialize."""
        self._errors: dict[str, str] = {}
        # The probe client. The two _test_connection_* methods assign it,
        # and _show_settings_form and _probe_nameplate reuse it, so a
        # successful probe leaves it open. _close_probe_client closes it
        # before each new probe and before the flow ends.
        self.client: SunSpecApiClient | None = None
        # The IP from a DHCP discovery or a network-scan pick. The
        # manual step uses it as the default of the host field.
        self._discovered_host: str | None = None
        self._discovered_unit_id: int | None = None
        # The scan result, kept so async_step_scan_results can render
        # the picker without a second scan.
        self._scan_results: list[SunSpecCandidate] = []

    @callback
    def async_remove(self) -> None:
        """Release a probe when Home Assistant removes or aborts this flow."""
        if self.client is not None:
            self.hass.async_create_task(
                self._close_probe_client(), "sunspec2 close config-flow probe"
            )

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """Handle a DHCP discovery for a known SunSpec inverter vendor.

        DHCP discovery rarely fires on a home network (long leases,
        inverters on a fixed IP). ``async_step_scan`` is the discovery
        path most users take.
        """
        host = discovery_info.ip
        _LOGGER.debug(
            "DHCP discovery for SunSpec inverter at %s (mac=%s, hostname=%s)",
            host,
            discovery_info.macaddress,
            discovery_info.hostname,
        )

        # The unique id of a configured entry is the device serial
        # number, which a DHCP lease does not carry. The host is
        # therefore the uniqueness key of a discovery, and a host that
        # already has an entry aborts the flow.
        await self.async_set_unique_id(f"dhcp:{host}")
        self._abort_if_unique_id_configured()
        for entry in self._async_current_entries():
            if entry.data.get(CONF_HOST) == host:
                return self.async_abort(reason="already_configured")

        self._discovered_host = host
        # An SMA answers SunSpec 123 unit ids above its own, 126 from
        # the factory; a form that says 1 costs the user a round trip.
        mac = discovery_info.macaddress.replace(":", "").lower()
        if mac.startswith(SMA_OUI) or discovery_info.hostname.lower().startswith("sma"):
            self._discovered_unit_id = SMA_SUNSPEC_UNIT_ID
        return await self.async_step_manual()

    def _get_unique_id(self, host: str, port: int, unit_id: int) -> str:
        """Build a stable unique ID even when device serial data is missing."""
        try:
            uid = self._device_info.getValue("SN")
        except KeyError:
            uid = None

        if uid in (None, ""):
            fallback_uid = f"{host}:{port}:{unit_id}"
            _LOGGER.info(
                "Device did not provide serial number during setup, using %s as unique ID",
                fallback_uid,
            )
            return fallback_uid

        return str(uid)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask how the inverter is connected.

        * **manual**: TCP, the user enters host, port and unit ID.
        * **scan**: a scan of the local subnet for Modbus TCP port 502
          that lists hosts with a SunSpec vendor's MAC prefix first.
        * **serial**: Modbus RTU over a serial port (RS-485, typically
          through a USB adapter). A serial bus cannot be scanned.
        """
        return self.async_show_menu(
            step_id="user",
            menu_options=["manual", "scan", "serial"],
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Set up a TCP device from host, port and unit ID.

        Reached from the user-step menu, from a network scan pick and
        from a DHCP discovery.
        """
        self._errors = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            unit_id = user_input.get(CONF_UNIT_ID) or user_input.get("slave_id", 1)
            valid = await self._test_connection_tcp(host, port, unit_id)
            if valid:
                uid = self._get_unique_id(host, port, unit_id)
                _LOGGER.debug(f"Sunspec device unique id: {uid}")
                await self.async_set_unique_id(uid)

                # The entry reloads through its update listener; reloading
                # here as well would run the reload twice (HA 2026.12 makes
                # the combination an error).
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: host, CONF_PORT: port, CONF_UNIT_ID: unit_id},
                    reload_on_update=False,
                )
                self.init_info = {
                    CONF_TRANSPORT: TRANSPORT_TCP,
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_UNIT_ID: unit_id,
                }
                return await self.async_step_settings()

            # The device is there but not SunSpec under this unit id.
            # An SMA answers SunSpec 123 unit ids above its own, a
            # mistake SMA owners make regularly (cjne/ha-sunspec #18,
            # #135, #269). If unit id 126 answers, the form comes back
            # with the error and the unit id already corrected.
            device_answered = self._errors.get("base") in ("device_error", "connection")
            if (
                unit_id != SMA_SUNSPEC_UNIT_ID
                and device_answered
                and await self._answers_sunspec(host, port, SMA_SUNSPEC_UNIT_ID)
            ):
                self._errors = {"base": "sma_unit_id"}
                user_input = {**user_input, CONF_UNIT_ID: SMA_SUNSPEC_UNIT_ID}

            return await self._show_config_form(user_input)

        return await self._show_config_form(user_input)

    async def async_step_serial(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Set up a Modbus RTU device from port, baudrate, parity and unit ID.

        Reached only from the user-step menu.
        """
        self._errors = {}
        if user_input is not None:
            serial_port = user_input[CONF_SERIAL_PORT]
            baudrate = user_input[CONF_BAUDRATE]
            parity = user_input[CONF_PARITY]
            unit_id = user_input[CONF_UNIT_ID]
            valid = await self._test_connection_rtu(serial_port, baudrate, parity, unit_id)
            if valid:
                # _get_unique_id reads the serial number from the
                # probed common model, whichever transport the probe ran
                # over.
                uid = self._get_unique_id(serial_port, baudrate, unit_id)
                _LOGGER.debug("Sunspec RTU device unique id: %s", uid)
                await self.async_set_unique_id(uid)
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_SERIAL_PORT: serial_port,
                        CONF_BAUDRATE: baudrate,
                        CONF_PARITY: parity,
                        CONF_UNIT_ID: unit_id,
                    },
                    reload_on_update=False,
                )
                self.init_info = {
                    CONF_TRANSPORT: TRANSPORT_RTU,
                    CONF_SERIAL_PORT: serial_port,
                    CONF_BAUDRATE: baudrate,
                    CONF_PARITY: parity,
                    CONF_UNIT_ID: unit_id,
                    # Host and port of a serial entry only feed the
                    # logger and the config-entry title. They are
                    # never used as network coordinates.
                    CONF_HOST: serial_port,
                    CONF_PORT: baudrate,
                }
                return await self.async_step_settings()
            return await self._show_serial_form(user_input)

        return await self._show_serial_form(user_input)

    async def _show_serial_form(self, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        """Render the serial-setup form with sensible defaults."""
        defaults = user_input or {
            CONF_SERIAL_PORT: "/dev/ttyUSB0",
            CONF_BAUDRATE: DEFAULT_BAUDRATE,
            CONF_PARITY: PARITY_NONE,
            CONF_UNIT_ID: 1,
        }
        return self.async_show_form(
            step_id="serial",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_SERIAL_PORT, default=defaults[CONF_SERIAL_PORT]): str,
                    probatio.Required(
                        CONF_BAUDRATE, default=defaults[CONF_BAUDRATE]
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=300,
                            max=921600,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    probatio.Required(
                        CONF_PARITY, default=defaults[CONF_PARITY]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=PARITY_NONE, label="None"),
                                selector.SelectOptionDict(value=PARITY_EVEN, label="Even"),
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key="parity",
                        )
                    ),
                    probatio.Required(CONF_UNIT_ID, default=defaults[CONF_UNIT_ID]): int,
                }
            ),
            errors=self._errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change host, port and unit ID of an existing entry.

        The new address is probed. If the device there answers with the
        serial number the entry already has as its unique id, the entry
        data is updated. The unique id stays the same, so the device
        entry, the entities and their history survive.
        """
        self._errors = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            unit_id = user_input[CONF_UNIT_ID]
            valid = await self._test_connection_tcp(host, port, unit_id)
            if valid:
                # A probe that returns another serial number than the
                # entry's unique id found a different inverter. The
                # change is refused, or the history of one device would
                # continue on another.
                probed_uid = self._get_unique_id(host, port, unit_id)
                if entry.unique_id is not None and probed_uid != entry.unique_id:
                    self._errors["base"] = "unique_id_mismatch"
                else:
                    # The probe gives its Modbus slot back before the
                    # reload builds the coordinator's own client, or a
                    # single-slot inverter refuses the new connection.
                    await self._close_probe_client()
                    # No reload here: the update listener reloads the
                    # entry when its data changed.
                    return self.async_update_and_abort(
                        entry,
                        data_updates={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_UNIT_ID: unit_id,
                        },
                    )

        defaults = {
            CONF_HOST: entry.data.get(CONF_HOST, ""),
            CONF_PORT: entry.data.get(CONF_PORT, 502),
            CONF_UNIT_ID: entry.data.get(CONF_UNIT_ID, 1),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_HOST, default=defaults[CONF_HOST]): str,
                    probatio.Required(CONF_PORT, default=defaults[CONF_PORT]): int,
                    probatio.Required(CONF_UNIT_ID, default=defaults[CONF_UNIT_ID]): int,
                }
            ),
            errors=self._errors,
        )

    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the subnet to scan, and scan it.

        The form is pre-filled with the default LAN subnet from
        ``homeassistant.components.network``.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            subnet = user_input["subnet"]
            try:
                self._scan_results = await async_discover_sunspec_candidates(self.hass, subnet)
            except ValueError as exc:
                _LOGGER.warning("Network scan rejected subnet %s: %s", subnet, exc)
                errors["base"] = "invalid_subnet"
            else:
                if not self._scan_results:
                    errors["base"] = "no_candidates"
                else:
                    return await self.async_step_scan_results()

        default_subnet = await async_get_default_subnet(self.hass) or "192.168.1.0/24"
        return self.async_show_form(
            step_id="scan",
            data_schema=probatio.Schema({probatio.Required("subnet", default=default_subnet): str}),
            errors=errors or None,
        )

    async def async_step_scan_results(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick a host from the scan result.

        The pick continues in the manual step with the IP pre-filled.
        """
        if user_input is not None:
            self._discovered_host = user_input["host"]
            return await self.async_step_manual()

        options: dict[str, str] = {}
        for cand in self._scan_results:
            label = cand.ip
            if cand.vendor_match and cand.mac is not None:
                label = f"{cand.ip} - {cand.mac} (SunSpec vendor)"
            elif cand.mac is not None:
                label = f"{cand.ip} - {cand.mac}"
            options[cand.ip] = label

        return self.async_show_form(
            step_id="scan_results",
            data_schema=probatio.Schema({probatio.Required("host"): probatio.In(options)}),
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._errors = {}
        if user_input is not None:
            # An empty model selection is rejected, as in the options
            # flow. The coordinator answers a stored
            # ``models_enabled: []`` by polling DEFAULT_MODELS, which is
            # not what the user picked.
            if not user_input.get(CONF_ENABLED_MODELS):
                self._errors["base"] = "no_models_selected"
                return await self._show_settings_form(user_input)

            self.init_info[CONF_PREFIX] = user_input[CONF_PREFIX]
            self.init_info[CONF_ENABLED_MODELS] = user_input[CONF_ENABLED_MODELS]
            self.init_info[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]
            # The peak AC power goes into the entry's options, not its
            # data, because the options flow edits it there. Without a
            # value the plausibility filter works from the detected
            # nameplate, see effective_peak_power_kw.
            options: dict[str, Any] = {}
            peak = user_input.get(CONF_MAX_AC_POWER_KW)
            if peak is not None:
                options[CONF_MAX_AC_POWER_KW] = peak
            host = self.init_info[CONF_HOST]
            port = self.init_info[CONF_PORT]
            unit_id = self.init_info[CONF_UNIT_ID]
            _LOGGER.debug("Creating entry with data %s, options %s", self.init_info, options)
            # The probe gives the inverter's Modbus slot back before
            # async_setup_entry opens the coordinator's client against
            # the same endpoint.
            await self._close_probe_client()
            return self.async_create_entry(
                title=f"{host}:{port}:{unit_id}",
                data=self.init_info,
                options=options,
            )

        return await self._show_settings_form(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SunSpecOptionsFlowHandler:
        return SunSpecOptionsFlowHandler()

    async def _show_config_form(self, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        """Show the manual configuration form to edit connection data."""
        defaults = user_input or {
            CONF_HOST: self._discovered_host or "",
            CONF_PORT: 502,
            CONF_UNIT_ID: self._discovered_unit_id or 1,
        }
        return self.async_show_form(
            step_id="manual",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_HOST, default=defaults[CONF_HOST]): str,
                    probatio.Required(CONF_PORT, default=defaults[CONF_PORT]): int,
                    probatio.Required(CONF_UNIT_ID, default=defaults[CONF_UNIT_ID]): int,
                }
            ),
            errors=self._errors,
        )

    async def _show_settings_form(self, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        """Show the settings form: prefix, scan interval, models and peak AC power.

        The peak AC power field is pre-filled from the device when it
        serves SunSpec model 120 ("WRtg") or 121 ("WMax").
        """
        # This step is only reached after a probe succeeded, and the
        # probe is what assigns self.client.
        assert self.client is not None
        models = set(await self.client.async_get_models())
        # sunspec_model_labels returns {model_id: "Group label (id)"},
        # so the multi-select shows "Inverter (Three Phase) (103)" and
        # not "103". It reads the bundled pysunspec2 JSON files, which
        # is sync IO and therefore runs in an executor.
        model_filter = await self.hass.async_add_executor_job(sunspec_model_labels, models)
        # default_enabled has to be a sorted list, not a set. The
        # frontend serialises ``default`` straight to JSON, and a set
        # does not come back in a form the multi-select can match
        # against its option keys: the field renders with no box
        # ticked.
        default_enabled = sorted(str(model) for model in default_models_for(models))

        suggested_peak = _suggested_peak_power_kw(await self._probe_nameplate(models))

        schema: dict[Any, Any] = {
            probatio.Optional(CONF_PREFIX, default=""): str,
            probatio.Optional(
                CONF_SCAN_INTERVAL, default=SCAN_INTERVAL.total_seconds()
            ): _SCAN_INTERVAL_VALIDATOR,
            probatio.Optional(
                CONF_ENABLED_MODELS,
                default=default_enabled,
            ): _model_selector(model_filter),
        }
        schema[
            probatio.Optional(
                CONF_MAX_AC_POWER_KW,
                description={"suggested_value": suggested_peak},
            )
        ] = _MAX_AC_POWER_SELECTOR

        return self.async_show_form(
            step_id="settings",
            data_schema=probatio.Schema(schema),
            errors=self._errors,
        )

    async def _probe_nameplate(self, available_models: set[int]) -> float | None:
        """Read the nameplate from SunSpec model 120 or 121 through the probe client.

        The probe's counterpart of
        ``SunSpecDataUpdateCoordinator._read_nameplate``. Returns the
        nameplate in kW, or ``None`` if neither model is present or both
        reads failed.
        """
        if self.client is None:
            return None
        for model_id, point_name, label in (
            (120, "WRtg", "model 120 WRtg"),
            (121, "WMax", "model 121 WMax"),
        ):
            if model_id not in available_models:
                continue
            try:
                wrapper = await self.client.async_get_data(model_id)
                value = wrapper.getValue(point_name)
            except Exception as exc:  # noqa: BLE001 - convenience read, never escalate
                _LOGGER.debug("Probe nameplate from %s failed (%s), trying next", label, exc)
                continue
            if isinstance(value, (int, float)) and value > 0:
                return float(value) / 1000.0
        return None

    async def _answers_sunspec(self, host: str, port: int, unit_id: int) -> bool:
        """Whether a one-shot probe finds common model 1 under ``unit_id``.

        The probe client is closed before and after, so a single-slot
        inverter never sees two sessions from this flow at once.
        """
        await self._close_probe_client()
        client = SunSpecApiClient(host, port, unit_id, self.hass)
        try:
            async with asyncio.timeout(SETUP_TIMEOUT):
                await client.async_get_device_info()
        except Exception:  # noqa: BLE001 - a probe that fails is the answer
            return False
        finally:
            await client.async_shutdown()
        return True

    async def _test_connection_tcp(self, host: str, port: int, unit_id: int) -> bool:
        """Probe a Modbus TCP inverter and cache its common-block info.

        The complete scan has a ``SETUP_TIMEOUT`` budget. Individual
        requests use Home Assistant's shared connection timeout.
        """
        _LOGGER.debug(f"Test TCP connection to {host}:{port} unit id {unit_id}")
        await self._close_probe_client()
        connected = False
        try:
            self.client = SunSpecApiClient(host, port, unit_id, self.hass)
            async with asyncio.timeout(SETUP_TIMEOUT):
                self._device_info = await self.client.async_get_device_info()
            _LOGGER.info(self._device_info)
            connected = True
            return True
        except Exception as err:
            set_connection_error(self._errors, host, port, unit_id, err)
        finally:
            if not connected:
                await self._close_probe_client()
        return False

    async def _test_connection_rtu(
        self, serial_port: str, baudrate: int, parity: str, unit_id: int
    ) -> bool:
        """Probe a Modbus RTU inverter and cache its common-block info.

        A failure sets the same error keys as the TCP probe.
        """
        _LOGGER.debug(
            "Test RTU connection on %s @ %d %s unit id %s",
            serial_port,
            baudrate,
            parity,
            unit_id,
        )
        await self._close_probe_client()
        connected = False
        try:
            self.client = SunSpecApiClient(
                host=serial_port,
                port=baudrate,
                unit_id=unit_id,
                hass=self.hass,
                transport=TRANSPORT_RTU,
                serial_port=serial_port,
                baudrate=baudrate,
                parity=parity,
            )
            async with asyncio.timeout(SETUP_TIMEOUT):
                self._device_info = await self.client.async_get_device_info()
            _LOGGER.info(self._device_info)
            connected = True
            return True
        except Exception as err:
            set_connection_error(self._errors, serial_port, baudrate, unit_id, err)
        finally:
            if not connected:
                await self._close_probe_client()
        return False

    async def _close_probe_client(self) -> None:
        """Release the probe client's Modbus session, if one is open.

        Every probe has to end here. A probe that is left open keeps its
        hold on the Modbus connection, and on a single-slot inverter
        that session occupies the only slot the device has. A
        successful probe is closed late, when the flow ends:
        ``_show_settings_form`` and ``_probe_nameplate`` keep using the
        client, and closing it earlier would force a second SunSpec
        scan inside the same flow.
        """
        client = getattr(self, "client", None)
        if client is None:
            return
        try:
            await client.async_shutdown()
        except Exception as exc:  # noqa: BLE001 - cleanup must never raise
            _LOGGER.debug("Probe client close raised %s, ignoring", exc)
        self.client = None


class SunSpecOptionsFlowHandler(config_entries.OptionsFlow):
    """Config flow options handler for sunspec."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize options flow."""
        self._errors: dict[str, str] = {}
        self.settings: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.coordinator: SunSpecDataUpdateCoordinator | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        self._errors = {}
        self.options = dict(self.config_entry.options)
        # runtime_data is missing while the entry is not loaded. The
        # hass.data lookup then finds nothing either, because no code
        # stores a coordinator there, and the coordinator stays None.
        self.coordinator = getattr(self.config_entry, "runtime_data", None)
        if self.coordinator is None:
            self.coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        return await self.async_step_host_options()

    async def async_step_host_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        if user_input is not None:
            self.settings.update(user_input)
            _LOGGER.debug("Sunspec host settings: %s", user_input)
            return await self.async_step_model_options()

        return await self.show_settings_form()

    async def show_settings_form(
        self,
        data: Mapping[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        settings = data or self.config_entry.data
        host = settings.get(CONF_HOST)
        port = settings.get(CONF_PORT)
        unit_id = settings.get(CONF_UNIT_ID) or settings.get("slave_id", 1)

        return self.async_show_form(
            step_id="host_options",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_HOST, default=host): str,
                    probatio.Required(CONF_PORT, default=port): int,
                    probatio.Required(CONF_UNIT_ID, default=unit_id): int,
                }
            ),
            errors=errors,
        )

    async def async_step_model_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Sections only organize the form. The stored options stay flat,
            # so existing installations and HACS rollbacks read the same keys.
            edit_power_limit = "advanced" in user_input
            user_input = dict(user_input)
            for name in ("advanced", "fronius"):
                user_input.update(user_input.pop(name, {}))
            # An empty model selection must not be saved. The form
            # comes back with an inline error instead.
            if not user_input.get(CONF_ENABLED_MODELS):
                errors["base"] = "no_models_selected"
            else:
                # The password goes no further than this method: it is
                # turned into the Digest secret below and only the secret
                # is stored. The password is popped before the update so
                # it cannot land in the options.
                password = user_input.pop(CONF_FRONIUS_WEB_PASSWORD, "")
                forget_web_login = user_input.pop(CONF_FRONIUS_WEB_FORGET, False)
                self.options.update(user_input)
                # A cleared optional field arrives without a key. Only a
                # submitted advanced section clears the stored peak power.
                # A submission without the section keeps it.
                if edit_power_limit and CONF_MAX_AC_POWER_KW not in user_input:
                    self.options.pop(CONF_MAX_AC_POWER_KW, None)
                if forget_web_login:
                    self.options.pop(CONF_FRONIUS_WEB_TOKEN, None)
                if password:
                    host = str(
                        self.settings.get(CONF_HOST) or self.config_entry.data.get(CONF_HOST)
                    )
                    try:
                        token = await async_mint_token(self.hass, host, password)
                    except FroniusWebAuthError:
                        errors["base"] = "web_auth_failed"
                    except FroniusWebError:
                        errors["base"] = "web_unreachable"
                    else:
                        self.options[CONF_FRONIUS_WEB_TOKEN] = token.as_dict()
                if not errors:
                    return await self._update_options()

        prefix = self.config_entry.options.get(CONF_PREFIX, self.config_entry.data.get(CONF_PREFIX))
        scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, self.config_entry.data.get(CONF_SCAN_INTERVAL)
        )
        capture_raw = self.config_entry.options.get(CONF_CAPTURE_RAW, False)
        standby_when_idle = self.config_entry.options.get(CONF_STANDBY_WHEN_IDLE, False)
        scan_delay = self.config_entry.options.get(CONF_SCAN_DELAY, DEFAULT_SCAN_DELAY_SECONDS)
        model_read_delay = self.config_entry.options.get(
            CONF_MODEL_READ_DELAY, DEFAULT_MODEL_READ_DELAY_SECONDS
        )
        # A configured peak power is shown exactly as stored, because
        # the filter uses it without headroom. Without one the field is
        # pre-filled from the nameplate the coordinator detected, with
        # the headroom the filter grants it (see
        # _suggested_peak_power_kw).
        max_ac_power_kw = self.config_entry.options.get(CONF_MAX_AC_POWER_KW)
        if max_ac_power_kw is None:
            max_ac_power_kw = _suggested_peak_power_kw(
                getattr(self.coordinator, "detected_max_ac_power_kw", None)
            )
        # The form works from the coordinator's state and never probes
        # the device: a probe would race the coordinator's session on a
        # single-slot inverter such as a KACO. While the coordinator is
        # failing, the host form comes back with a connection error
        # instead of an empty model list. The getattr default of True
        # covers a coordinator that is None, see below.
        if not getattr(self.coordinator, "last_update_success", True):
            return await self.show_settings_form(data=self.settings, errors={"base": "connection"})
        try:
            # detected_models is the coordinator's cache of the device's
            # full model list, filled by the first successful update
            # cycle. api.known_models() is only the fallback for a form
            # opened before that cycle: it reads the live client and
            # returns ``[]`` when there is none, before the first
            # connect and after a failed cycle tore the session down.
            models = set(getattr(self.coordinator, "detected_models", set()))
            # The coordinator is None when the entry is not loaded, and
            # the options flow can still be opened then.
            if not models and self.coordinator is not None:
                models = set(self.coordinator.api.known_models())
            if not models:
                # Nothing has polled the device: the entry never loaded, or
                # its first cycle has not finished. The form offers the
                # models the entry already carries. An empty multi-select
                # could not be submitted, because the no_models_selected
                # check at the top of this step rejects it, and a user who
                # came here to correct an unreachable host could not save
                # the new one.
                stored = self.config_entry.options.get(
                    CONF_ENABLED_MODELS, self.config_entry.data.get(CONF_ENABLED_MODELS, [])
                )
                models = {int(m) for m in stored} or set(DEFAULT_MODELS)
            model_filter = await self.hass.async_add_executor_job(sunspec_model_labels, models)
            # A sorted list, not a set: see _show_settings_form.
            default_enabled = sorted(str(m) for m in default_models_for(models))
            persisted = self.config_entry.options.get(CONF_ENABLED_MODELS, default_enabled)
            default_models = sorted(str(m) for m in persisted if int(m) in models)
            # An empty persisted selection is pre-filled with the
            # defaults.
            if not default_models:
                default_models = default_enabled

            vendor = getattr(self.coordinator, "vendor", None)
            rearm_on_change = self.config_entry.options.get(
                CONF_REARM_ON_CHANGE, vendor.rearm_by_default if vendor is not None else False
            )
            advanced: dict[Any, Any] = {
                probatio.Optional(CONF_PREFIX, default=prefix): str,
                probatio.Optional(
                    CONF_MAX_AC_POWER_KW,
                    description={"suggested_value": max_ac_power_kw},
                ): _MAX_AC_POWER_SELECTOR,
                probatio.Optional(CONF_STANDBY_WHEN_IDLE, default=standby_when_idle): bool,
                probatio.Optional(CONF_SCAN_DELAY, default=scan_delay): _SCAN_DELAY_SELECTOR,
                probatio.Optional(
                    CONF_MODEL_READ_DELAY, default=model_read_delay
                ): _MODEL_READ_DELAY_SELECTOR,
                probatio.Optional(CONF_CAPTURE_RAW, default=capture_raw): bool,
            }
            schema: dict[Any, Any] = {
                probatio.Optional(
                    CONF_SCAN_INTERVAL, default=scan_interval
                ): _SCAN_INTERVAL_VALIDATOR,
                probatio.Optional(
                    CONF_ENABLED_MODELS,
                    default=default_models,
                ): _model_selector(model_filter),
            }
            # Only a vendor that needs the off/on cycle for a new
            # export limit gets the field.
            if vendor is not None and vendor.enable_edge:
                advanced[probatio.Optional(CONF_REARM_ON_CHANGE, default=rearm_on_change)] = bool
            schema[probatio.Optional("advanced")] = section(
                probatio.Schema(advanced), {"collapsed": True}
            )
            # The web interface login, for a vendor whose web API the
            # integration speaks, on a TCP entry: the web page lives on
            # the same address as the Modbus port.
            if (
                vendor is not None
                and vendor.web_user
                and self.config_entry.data.get(CONF_TRANSPORT, TRANSPORT_TCP) == TRANSPORT_TCP
            ):
                web_schema: dict[Any, Any] = {
                    probatio.Optional(CONF_FRONIUS_WEB_PASSWORD): _PASSWORD_SELECTOR
                }
                if self.config_entry.options.get(CONF_FRONIUS_WEB_TOKEN):
                    web_schema[probatio.Optional(CONF_FRONIUS_WEB_FORGET, default=False)] = bool
                schema[probatio.Optional("fronius")] = section(
                    probatio.Schema(web_schema), {"collapsed": True}
                )

            return self.async_show_form(
                step_id="model_options",
                data_schema=probatio.Schema(schema),
                errors=errors or None,
            )
        except Exception as e:
            set_connection_error(
                self._errors,
                self.settings[CONF_HOST],
                self.settings[CONF_PORT],
                self.settings[CONF_UNIT_ID],
                e,
            )
            return await self.show_settings_form(data=self.settings, errors=self._errors)

    async def _update_options(self) -> ConfigFlowResult:
        """Update config entry options."""
        title = (
            f"{self.settings[CONF_HOST]}:{self.settings[CONF_PORT]}:{self.settings[CONF_UNIT_ID]}"
        )
        _LOGGER.debug(
            "Saving config entry with title %s, data: %s options %s",
            title,
            self.settings,
            redact_options(self.options),
        )
        # The data is merged, not replaced. self.settings only holds what
        # the host form asked for: host, port and unit id. A serial entry
        # keeps its transport, serial_port, baudrate and parity in data and
        # nowhere else, so writing settings as the whole of data would turn
        # it into a TCP entry aimed at a host that does not exist.
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, **self.settings},
            title=title,
        )
        return self.async_create_entry(title="", data=self.options)
