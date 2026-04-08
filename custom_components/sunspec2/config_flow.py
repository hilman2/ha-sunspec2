"""Adds config flow for SunSpec."""

import logging
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import SCAN_INTERVAL
from .api import SETUP_TIMEOUT
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
from .discovery import SunSpecCandidate
from .discovery import async_discover_sunspec_candidates
from .discovery import async_get_default_subnet
from .errors import DeviceError
from .errors import ProtocolError
from .errors import TransientError
from .errors import TransportError
from .model_labels import sunspec_model_labels

_LOGGER: logging.Logger = logging.getLogger(__package__)

# Number selector for the optional peak AC power field. Using a selector
# (rather than a plain callable like `vol.Coerce(float)` or a custom
# validator) is required so voluptuous_serialize can serialise the schema
# when the frontend requests the options form - otherwise the POST to
# config/config_entries/options/flow raises and the form never renders.
_MAX_AC_POWER_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0.1,
        step=0.1,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="kW",
    )
)


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
        # Pre-filled IP from a DHCP discovery or a network-scan pick.
        # The manual step uses this as the default for the host field so
        # the user just needs to confirm port and unit ID.
        self._discovered_host: str | None = None
        # Cached scan result so async_step_scan_results can render the
        # picker without re-scanning.
        self._scan_results: list[SunSpecCandidate] = []

    async def async_step_dhcp(self, discovery_info):
        """Handle a DHCP discovery for a known SunSpec inverter vendor.

        Best-effort second path: most home routers hand out 8 h+
        leases and most users put their inverter on a fixed IP, so
        DHCP discovery rarely fires in practice. The active
        ``async_step_scan`` path is the one that actually works for
        the typical home setup. We keep DHCP around because when it
        does fire it costs the user zero clicks.
        """
        host = discovery_info.ip
        _LOGGER.debug(
            "DHCP discovery for SunSpec inverter at %s (mac=%s, hostname=%s)",
            host,
            discovery_info.macaddress,
            discovery_info.hostname,
        )

        # Bail if we already have a config entry for this exact host -
        # the user has already set this device up. ``unique_id`` for an
        # already-configured entry is the device serial number, which
        # we cannot derive from a DHCP lease alone, so use the host as
        # the discovery uniqueness key instead.
        await self.async_set_unique_id(f"dhcp:{host}")
        self._abort_if_unique_id_configured()
        for entry in self._async_current_entries():
            if entry.data.get(CONF_HOST) == host:
                return self.async_abort(reason="already_configured")

        self._discovered_host = host
        return await self.async_step_manual()

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
        """Initial entry: ask whether to enter the IP manually or scan.

        Manual is the safe default for users who already know their
        inverter's IP (the typical case for fixed-IP setups). The
        scan path scans the local subnet for hosts with Modbus TCP
        port 502 open and returns a pickable list, optionally
        prioritised by SunSpec vendor MAC OUI matches.
        """
        return self.async_show_menu(
            step_id="user",
            menu_options=["manual", "scan"],
        )

    async def async_step_manual(self, user_input=None):
        """Manual setup: enter host, port and unit ID by hand.

        This is the original ``async_step_user`` body from before
        v0.8.1, kept intact and renamed. Reachable from the user-step
        menu, from a successful network scan, and from a DHCP
        discovery.
        """
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

    async def async_step_reconfigure(self, user_input=None):
        """Gold rule reconfiguration-flow: change host / port / unit ID
        on an existing config entry without losing the device or its
        sensor history.

        The user reaches this from *Settings → Devices & Services →
        SunSpec → three-dot menu → Reconfigure*. We probe the new
        connection details, and if it answers we update the entry's
        data block in place and reload it. The unique_id (the
        inverter's serial from common model 1) stays the same so the
        device entry and the entire sensor entity registry survive.
        """
        self._errors = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            unit_id = user_input[CONF_UNIT_ID]
            valid = await self._test_connection(host, port, unit_id)
            if valid:
                # The probe must come back with a serial that matches
                # the existing entry's unique_id - otherwise this is
                # a different inverter on the same IP and we refuse
                # the change to avoid silently swapping a user's
                # device history onto a stranger's hardware.
                probed_uid = self._get_unique_id(host, port, unit_id)
                if entry.unique_id is not None and probed_uid != entry.unique_id:
                    self._errors["base"] = "unique_id_mismatch"
                else:
                    return self.async_update_reload_and_abort(
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
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=defaults[CONF_HOST]): str,
                    vol.Required(CONF_PORT, default=defaults[CONF_PORT]): int,
                    vol.Required(CONF_UNIT_ID, default=defaults[CONF_UNIT_ID]): int,
                }
            ),
            errors=self._errors,
        )

    async def async_step_scan(self, user_input=None):
        """Subnet entry form for the active network scan.

        Pre-fills the user's default LAN subnet from
        ``homeassistant.components.network`` so the typical home user
        only has to click "submit". Power users on multi-subnet LANs
        can override the value.
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
            data_schema=vol.Schema({vol.Required("subnet", default=default_subnet): str}),
            errors=errors or None,
        )

    async def async_step_scan_results(self, user_input=None):
        """Pick a host from the cached scan result list.

        Vendor-matched candidates are listed first (the scan helper
        already sorts that way). Selecting one routes to the manual
        step with the IP pre-filled, so the user only confirms port
        and unit ID.
        """
        if user_input is not None:
            self._discovered_host = user_input["host"]
            return await self.async_step_manual()

        # Build a {ip: human label} mapping for the dropdown.
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
            data_schema=vol.Schema({vol.Required("host"): vol.In(options)}),
        )

    async def async_step_settings(self, user_input=None):
        self._errors = {}
        if user_input is not None:
            # Reject empty model selections in the SETUP flow too. The
            # OPTIONS flow has had this guard since v0.7.6, but the
            # initial setup was missing it - if the user clicked
            # through the form without ticking anything they would
            # silently persist ``models_enabled: []`` to disk and the
            # next coordinator reload would poll zero models, killing
            # every sensor on the integration before they ever existed.
            if not user_input.get(CONF_ENABLED_MODELS):
                self._errors["base"] = "no_models_selected"
                return await self._show_settings_form(user_input)

            self.init_info[CONF_PREFIX] = user_input[CONF_PREFIX]
            self.init_info[CONF_ENABLED_MODELS] = user_input[CONF_ENABLED_MODELS]
            self.init_info[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]
            # Peak AC power survives onto the new entry as an option,
            # not as data, so it lines up with where the options-flow
            # form writes it on later edits. None / empty disables the
            # plausibility filter.
            options: dict[str, Any] = {}
            peak = user_input.get(CONF_MAX_AC_POWER_KW)
            if peak is not None:
                options[CONF_MAX_AC_POWER_KW] = peak
            host = self.init_info[CONF_HOST]
            port = self.init_info[CONF_PORT]
            unit_id = self.init_info[CONF_UNIT_ID]
            _LOGGER.debug("Creating entry with data %s, options %s", self.init_info, options)
            return self.async_create_entry(
                title=f"{host}:{port}:{unit_id}",
                data=self.init_info,
                options=options,
            )

        return await self._show_settings_form(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SunSpecOptionsFlowHandler()

    async def _show_config_form(self, user_input):
        """Show the manual configuration form to edit connection data."""
        defaults = user_input or {
            CONF_HOST: self._discovered_host or "",
            CONF_PORT: 502,
            CONF_UNIT_ID: 1,
        }
        return self.async_show_form(
            step_id="manual",
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
        """Show the configuration form to edit settings data.

        Includes the optional peak AC power field directly here so the
        user gets a single chance to set their inverter's nameplate
        during initial setup, instead of having to discover it in the
        options flow later. We try to suggest a value from the device
        itself: if the inverter exposes SunSpec model 120 ("WRtg") or
        121 ("WMax") the probe client picks it up and the field is
        pre-filled, otherwise the user enters it by hand or leaves it
        empty to disable the plausibility filter.
        """
        models = set(await self.client.async_get_models())
        # Resolve {model_id: "Group label (id)"} so the multi-select
        # shows "Inverter (Three Phase) (103)" instead of just "103".
        # Reading the bundled pysunspec2 JSON files is sync IO so it
        # runs in an executor.
        model_filter = await self.hass.async_add_executor_job(sunspec_model_labels, models)
        # Default-enabled MUST be a sorted list, not a set: HA's
        # frontend serialises ``default`` straight to JSON and Python
        # sets do not survive that round-trip in a way the multi-select
        # widget can match against its option keys, so the field
        # would render with no boxes ticked even though the right
        # values are technically there. Sorted list = stable order
        # in the UI and reliable pre-selection.
        default_enabled = sorted(model for model in DEFAULT_MODELS if model in models)
        # Preserve the user's previous picks across re-entry into the
        # settings step (e.g. when we bounce them back with the
        # no_models_selected error). Falling back to default_enabled
        # otherwise.
        if user_input is not None and user_input.get(CONF_ENABLED_MODELS):
            current_selection = sorted(m for m in user_input[CONF_ENABLED_MODELS] if m in models)
            default_enabled = current_selection or default_enabled

        suggested_peak = await self._probe_nameplate(models)

        schema: dict[Any, Any] = {
            vol.Optional(CONF_PREFIX, default=""): str,
            vol.Optional(CONF_SCAN_INTERVAL, default=SCAN_INTERVAL.total_seconds()): int,
            vol.Optional(
                CONF_ENABLED_MODELS,
                default=default_enabled,
            ): cv.multi_select(model_filter),
        }
        schema[
            vol.Optional(
                CONF_MAX_AC_POWER_KW,
                description={"suggested_value": suggested_peak},
            )
        ] = _MAX_AC_POWER_SELECTOR

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(schema),
            errors=self._errors,
        )

    async def _probe_nameplate(self, available_models: set[int]) -> float | None:
        """Read SunSpec model 120 / 121 from the probe client, best-effort.

        Mirrors ``SunSpecDataUpdateCoordinator._read_nameplate`` but
        runs against the live probe client instead of the coordinator
        (which doesn't exist yet at config-flow time). Returns the
        nameplate in kW, or ``None`` if neither model is present or
        both reads failed - the user can still type a value by hand
        in that case.
        """
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

    async def _test_connection(self, host, port, unit_id):
        """Return true if credentials is valid.

        Uses ``SETUP_TIMEOUT`` instead of the steady-state ``TIMEOUT``
        because the very first ``client.scan()`` walks every SunSpec
        model on the device, which can be 16+ models deep on a fully
        featured inverter and is much slower than a single read in
        steady state. The 10s steady-state ceiling is too tight for
        the initial walk on slower devices (notably KACO Powador on
        100 Mbit), so the config-flow probe gets the longer 60s
        envelope.
        """
        _LOGGER.debug(f"Test connection to {host}:{port} unit id {unit_id}")
        try:
            self.client = SunSpecApiClient(host, port, unit_id, self.hass, timeout=SETUP_TIMEOUT)
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
        # Read the coordinator from entry.runtime_data (Bronze rule
        # runtime-data). Falls back to the legacy hass.data lookup
        # for the test stub coordinator, which doesn't go through
        # the real async_setup_entry path.
        self.coordinator = getattr(self.config_entry, "runtime_data", None)
        if self.coordinator is None:
            self.coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
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
        errors: dict[str, str] = {}

        if user_input is not None:
            # Reject empty model selections - silently saving
            # ``models_enabled: []`` was the v0.7.3 -> v0.7.5 regression
            # that wiped every sensor on the next reload. Show the form
            # again with an inline error so the user has a chance to
            # actually pick something.
            if not user_input.get(CONF_ENABLED_MODELS):
                errors["base"] = "no_models_selected"
            else:
                self.options.update(user_input)
                return await self._update_options()

        prefix = self.config_entry.options.get(CONF_PREFIX, self.config_entry.data.get(CONF_PREFIX))
        scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, self.config_entry.data.get(CONF_SCAN_INTERVAL)
        )
        capture_raw = self.config_entry.options.get(CONF_CAPTURE_RAW, False)
        # User-set value wins. If the user has not configured a peak
        # power yet, fall back to the value the coordinator auto-
        # detected from SunSpec model 120 / 121 on the first cycle.
        # That way the form opens with a sensible default for the
        # plausibility filter without the user having to type the
        # inverter's nameplate by hand.
        max_ac_power_kw = self.config_entry.options.get(CONF_MAX_AC_POWER_KW)
        if max_ac_power_kw is None:
            max_ac_power_kw = getattr(self.coordinator, "detected_max_ac_power_kw", None)
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
            # Read the inverter's full model list from the coordinator's
            # cache (populated during the first successful update cycle).
            # Falling back to api.known_models() preserves behaviour for
            # the test stub coordinator that doesn't carry the cache, and
            # for the very first options-form open before any update has
            # happened. NEVER call api.known_models() unconditionally:
            # ``api._client`` is closed at the end of every cycle, so the
            # call returns ``[]`` between cycles and the form would render
            # an empty multi-select.
            models = set(getattr(self.coordinator, "detected_models", set()))
            if not models:
                models = set(self.coordinator.api.known_models())
            # Resolve {model_id: "Group label (id)"} so the multi-select
            # shows "Inverter (Three Phase) (103)" instead of just
            # "103". Reading the bundled pysunspec2 JSON files is
            # sync IO so it runs in an executor.
            model_filter = await self.hass.async_add_executor_job(sunspec_model_labels, models)
            # Sorted list, not set - see comment in
            # _show_settings_form for why this matters for the
            # frontend's pre-selection logic.
            default_enabled = sorted(m for m in DEFAULT_MODELS if m in models)
            persisted = self.config_entry.options.get(CONF_ENABLED_MODELS, default_enabled)
            default_models = sorted(m for m in persisted if m in models)
            # If the persisted selection is empty (e.g. corrupted by the
            # v0.7.3 regression), pre-fill the multi-select with the
            # defaults so the user doesn't have to start from scratch.
            if not default_models:
                default_models = default_enabled

            schema: dict[Any, Any] = {
                vol.Optional(CONF_PREFIX, default=prefix): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=scan_interval): int,
                vol.Optional(
                    CONF_ENABLED_MODELS,
                    default=default_models,
                ): cv.multi_select(model_filter),
                vol.Optional(CONF_CAPTURE_RAW, default=capture_raw): bool,
            }
            # Use suggested_value (not default) for the optional float so
            # the form field can stay genuinely empty - an empty value
            # disables the plausibility filter rather than coercing to 0.
            # The value is stored as None when the user clears the field.
            schema[
                vol.Optional(
                    CONF_MAX_AC_POWER_KW,
                    description={"suggested_value": max_ac_power_kw},
                )
            ] = _MAX_AC_POWER_SELECTOR

            return self.async_show_form(
                step_id="model_options",
                data_schema=vol.Schema(schema),
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
