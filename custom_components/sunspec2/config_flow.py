"""Adds config flow for SunSpec."""

import logging
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from . import SCAN_INTERVAL
from .api import SunSpecApiClient
from .const import CONF_CAPTURE_RAW
from .const import CONF_ENABLED_MODELS
from .const import CONF_HOST
from .const import CONF_MAX_AC_POWER_KW
from .const import CONF_PORT
from .const import CONF_PREFIX
from .const import CONF_SCAN_INTERVAL
from .const import CONF_UNIT_ID
from .const import DEFAULT_MODELS
from .const import DOMAIN
from .errors import DeviceError
from .errors import ProtocolError
from .errors import TransientError
from .errors import TransportError

_LOGGER: logging.Logger = logging.getLogger(__package__)


def _optional_positive_float(value):
    """Coerce empty values to None and validate positive floats otherwise."""
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as err:
        raise vol.Invalid("must be a number") from err
    if result <= 0:
        raise vol.Invalid("must be greater than zero")
    return result


def set_connection_error(errors, host, port, unit_id, err):
    """Map backend failures to user-visible config flow errors."""
    if isinstance(err, TransientError):
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

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        """Initialize."""
        self._errors = {}

    def _get_unique_id(self, host, port, unit_id):
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

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        self._errors = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            unit_id = user_input.get(CONF_UNIT_ID) or user_input.get("slave_id", 1)
            valid = await self._test_connection(host, port, unit_id)
            if valid:
                uid = self._get_unique_id(host, port, unit_id)
                _LOGGER.debug(f"Sunspec device unique id: {uid}")
                await self.async_set_unique_id(uid)

                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: host, CONF_PORT: port, CONF_UNIT_ID: unit_id}
                )
                self.init_info = user_input
                return await self.async_step_settings()

            return await self._show_config_form(user_input)

        return await self._show_config_form(user_input)

    async def async_step_settings(self, user_input=None):
        self._errors = {}
        if user_input is not None:
            self.init_info[CONF_PREFIX] = user_input[CONF_PREFIX]
            self.init_info[CONF_ENABLED_MODELS] = user_input[CONF_ENABLED_MODELS]
            self.init_info[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]
            host = self.init_info[CONF_HOST]
            port = self.init_info[CONF_PORT]
            unit_id = self.init_info[CONF_UNIT_ID]
            _LOGGER.debug("Creating entry with data %s", self.init_info)
            return self.async_create_entry(title=f"{host}:{port}:{unit_id}", data=self.init_info)

        return await self._show_settings_form(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SunSpecOptionsFlowHandler()

    async def _show_config_form(self, user_input):
        """Show the configuration form to edit connection data."""
        defaults = user_input or {CONF_HOST: "", CONF_PORT: 502, CONF_UNIT_ID: 1}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=defaults[CONF_HOST]): str,
                    vol.Required(CONF_PORT, default=defaults[CONF_PORT]): int,
                    vol.Required(CONF_UNIT_ID, default=defaults[CONF_UNIT_ID]): int,
                }
            ),
            errors=self._errors,
        )

    async def _show_settings_form(self, user_input):
        """Show the configuration form to edit settings data."""
        models = set(await self.client.async_get_models())
        model_filter = {model for model in sorted(models)}
        default_enabled = {model for model in DEFAULT_MODELS if model in models}
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PREFIX, default=""): str,
                    vol.Optional(CONF_SCAN_INTERVAL, default=SCAN_INTERVAL.total_seconds()): int,
                    vol.Optional(
                        CONF_ENABLED_MODELS,
                        default=default_enabled,
                    ): cv.multi_select(model_filter),
                }
            ),
            errors=self._errors,
        )

    async def _test_connection(self, host, port, unit_id):
        """Return true if credentials is valid."""
        _LOGGER.debug(f"Test connection to {host}:{port} unit id {unit_id}")
        try:
            self.client = SunSpecApiClient(host, port, unit_id, self.hass)
            self._device_info = await self.client.async_get_device_info()
            _LOGGER.info(self._device_info)
            return True
        except Exception as err:
            set_connection_error(self._errors, host, port, unit_id, err)
        return False


class SunSpecOptionsFlowHandler(config_entries.OptionsFlow):
    """Config flow options handler for sunspec."""

    VERSION = 1

    def __init__(self):
        """Initialize options flow."""
        self._errors = {}
        self.settings = {}
        self.options = {}
        self.coordinator = None

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        self._errors = {}
        self.options = dict(self.config_entry.options)
        self.coordinator = self.hass.data[DOMAIN][self.config_entry.entry_id]
        return await self.async_step_host_options()

    async def async_step_host_options(self, user_input=None):
        """Handle a flow initialized by the user."""
        if user_input is not None:
            self.settings.update(user_input)
            _LOGGER.debug("Sunspec host settings: %s", user_input)
            return await self.async_step_model_options()

        return await self.show_settings_form()

    async def show_settings_form(self, data=None, errors=None):
        settings = data or self.config_entry.data
        host = settings.get(CONF_HOST)
        port = settings.get(CONF_PORT)
        unit_id = settings.get(CONF_UNIT_ID) or settings.get("slave_id", 1)

        return self.async_show_form(
            step_id="host_options",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=host): str,
                    vol.Required(CONF_PORT, default=port): int,
                    vol.Required(CONF_UNIT_ID, default=unit_id): int,
                }
            ),
            errors=errors,
        )

    async def async_step_model_options(self, user_input=None):
        """Handle a flow initialized by the user."""
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        prefix = self.config_entry.options.get(CONF_PREFIX, self.config_entry.data.get(CONF_PREFIX))
        scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, self.config_entry.data.get(CONF_SCAN_INTERVAL)
        )
        capture_raw = self.config_entry.options.get(CONF_CAPTURE_RAW, False)
        max_ac_power_kw = self.config_entry.options.get(CONF_MAX_AC_POWER_KW)
        # Phase 4 hot-reload fix: instead of forcing a fresh probe (which
        # raced with the coordinator's active socket on single-slot
        # inverters like KACO), surface the coordinator's current state.
        # If the coordinator is currently failing, the user already sees
        # an unavailable inverter; we show a warning here too rather
        # than silently rendering an empty model list. The getattr()
        # default of True is for the test stub coordinator which does
        # not inherit from DataUpdateCoordinator and lacks the attribute.
        if not getattr(self.coordinator, "last_update_success", True):
            return await self.show_settings_form(data=self.settings, errors={"base": "connection"})
        try:
            # known_models() reads what the live client has already
            # discovered during async_setup_entry; it never opens a new
            # TCP connection.
            models = set(self.coordinator.api.known_models())
            model_filter = {model for model in sorted(models)}
            default_enabled = {model for model in DEFAULT_MODELS if model in models}
            default_models = self.config_entry.options.get(CONF_ENABLED_MODELS, default_enabled)

            default_models = {model for model in default_models if model in models}

            schema: dict[Any, Any] = {
                vol.Optional(CONF_PREFIX, default=prefix): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=scan_interval): int,
                vol.Optional(
                    CONF_ENABLED_MODELS,
                    default=default_models,
                ): cv.multi_select(model_filter),
                vol.Optional(CONF_CAPTURE_RAW, default=capture_raw): bool,
            }
            # Use suggested_value (not default) for the optional float so the
            # form field can stay genuinely empty - an empty value disables
            # the plausibility filter rather than coercing to 0.
            schema[
                vol.Optional(
                    CONF_MAX_AC_POWER_KW,
                    description={"suggested_value": max_ac_power_kw},
                )
            ] = _optional_positive_float

            return self.async_show_form(
                step_id="model_options",
                data_schema=vol.Schema(schema),
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

    async def _update_options(self):
        """Update config entry options."""
        title = (
            f"{self.settings[CONF_HOST]}:{self.settings[CONF_PORT]}:{self.settings[CONF_UNIT_ID]}"
        )
        _LOGGER.debug(
            "Saving config entry with title %s, data: %s options %s",
            title,
            self.settings,
            self.options,
        )
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=self.settings, title=title
        )
        return self.async_create_entry(title="", data=self.options)
