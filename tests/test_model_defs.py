"""The model definitions are read from disk once, from a thread, never on the event loop.

Home Assistant's blocking-call detector reported ``open()`` from
``get_model_def`` during the first scan on v2026.10.0b1: with the read
path on the event loop, the lazy definition loads landed there too.
"""

import threading
from unittest.mock import patch

import pytest

from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.pysunspec2 import device
from custom_components.sunspec2.pysunspec2.mdef import ModelDefinitionError

from .fake_unit import FakeConnection
from .fake_unit import FakeUnit
from .fake_unit import register_image


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Start and end with an empty cache, so no other test sees the preloaded state."""
    device.clear_model_defs_cache()
    yield
    device.clear_model_defs_cache()


def test_preloaded_definitions_come_from_the_cache_and_unknown_ids_touch_no_file():
    device.preload_model_defs()
    assert device.model_defs_preloaded

    with (
        patch(
            "custom_components.sunspec2.pysunspec2.mdef.from_json_file",
            side_effect=AssertionError("opened a file"),
        ),
        patch(
            "custom_components.sunspec2.pysunspec2.smdx.from_smdx_file",
            side_effect=AssertionError("opened a file"),
        ),
    ):
        assert device.get_model_def(103)["id"] == 103
        assert device.get_model_def(160)["id"] == 160
        with pytest.raises(ModelDefinitionError, match="not found for model 64110"):
            device.get_model_def(64110)

    # A second preload is free.
    device.preload_model_defs()


def test_clearing_the_cache_makes_the_loader_lazy_again():
    device.preload_model_defs()
    device.clear_model_defs_cache()
    assert not device.model_defs_preloaded
    assert device.get_model_def(1)["id"] == 1


async def test_the_first_connect_reads_the_definitions_off_the_event_loop(hass):
    """Every definition file is opened in a worker thread, none on the loop."""
    threads = []
    original = device.mdef.from_json_file

    def recording(filename):
        threads.append(threading.current_thread())
        return original(filename)

    api = SunSpecApiClient(host="test", port=502, unit_id=1, hass=hass)
    connection = FakeConnection(
        {1: FakeUnit(register_image("./tests/test_data/inverter_fronius.json"))}
    )
    with (
        patch.object(api, "_get_connection", return_value=connection),
        patch("custom_components.sunspec2.pysunspec2.mdef.from_json_file", recording),
    ):
        assert await api.async_get_models() == [1, 123, 124, 160]

    assert device.model_defs_preloaded
    assert threads, "the preload read nothing"
    assert all(thread is not threading.main_thread() for thread in threads)
