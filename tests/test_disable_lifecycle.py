"""Connection lifetime when Home Assistant disables a config entry."""

import asyncio
import struct
from typing import cast
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.config_entries import ConfigEntryState

from custom_components.sunspec2.api import SunSpecApiClient
from custom_components.sunspec2.errors import TransportError

from . import create_mock_sunspec_config_entry
from .const import MOCK_CONFIG
from .fake_unit import register_image


@pytest.fixture
async def tcp_device(hass, socket_enabled):
    """A device that silently ignores reads outside its SunSpec register map."""
    registers = register_image("./tests/test_data/inverter_fronius.json")
    writers = set()
    reads = []
    peer_closed = asyncio.Event()

    async def serve(reader, writer):
        writers.add(writer)
        try:
            while True:
                header = await reader.readexactly(7)
                transaction, protocol, length, unit_id = struct.unpack(">HHHB", header)
                body = await reader.readexactly(length - 1)
                function, address, count = struct.unpack(">BHH", body)
                assert function == 3
                reads.append((unit_id, address, count))
                if any(address + offset not in registers for offset in range(count)):
                    continue
                values = [registers[address + offset] for offset in range(count)]
                response = struct.pack(">BB", function, count * 2) + struct.pack(
                    f">{count}H", *values
                )
                writer.write(
                    struct.pack(">HHHB", transaction, protocol, len(response) + 1, unit_id)
                )
                writer.write(response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            writers.discard(writer)
            if not writers:
                peer_closed.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, registers, reads, writers, peer_closed
    finally:
        server.close()
        await server.wait_closed()
        for writer in list(writers):
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in list(writers)))


def _fast_client(*args, **kwargs):
    kwargs["scan_delay"] = 0
    return SunSpecApiClient(*args, **kwargs)


async def test_disabling_last_entry_closes_its_tcp_connection(hass, tcp_device):
    port, _, _, writers, peer_closed = tcp_device
    entry = create_mock_sunspec_config_entry(
        hass, data={**MOCK_CONFIG, "host": "127.0.0.1", "port": port}
    )
    with patch("custom_components.sunspec2.SunSpecApiClient", _fast_client):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(writers) == 1
    assert entry.state == ConfigEntryState.LOADED

    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()

    assert cast(ConfigEntryState, entry.state) == ConfigEntryState.NOT_LOADED
    async with asyncio.timeout(1):
        await peer_closed.wait()
    assert not writers


async def test_disable_during_retry_does_not_reopen_the_connection(hass, tcp_device):
    port, _, _, writers, _ = tcp_device
    entry = create_mock_sunspec_config_entry(
        hass, data={**MOCK_CONFIG, "host": "127.0.0.1", "port": port}
    )
    with patch("custom_components.sunspec2.SunSpecApiClient", _fast_client):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    cycle = coordinator._run_one_update_cycle
    sleeping = asyncio.Event()
    resume = asyncio.Event()
    sleep = asyncio.sleep
    attempts = 0

    async def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransportError("lost connection")
        return await cycle()

    async def pause_retry(seconds):
        if seconds == 1234:
            sleeping.set()
            await resume.wait()
        else:
            await sleep(seconds)

    with (
        patch.object(coordinator, "_run_one_update_cycle", fail_once),
        patch("custom_components.sunspec2.INTERVAL_RETRY_DELAY_SECONDS", 1234),
        patch("custom_components.sunspec2.asyncio.sleep", pause_retry),
    ):
        refresh = asyncio.create_task(coordinator.async_refresh())
        try:
            async with asyncio.timeout(1):
                await sleeping.wait()
            assert await hass.config_entries.async_set_disabled_by(
                entry.entry_id, ConfigEntryDisabler.USER
            )
            resume.set()
            async with asyncio.timeout(1):
                await refresh
            assert entry.state == ConfigEntryState.NOT_LOADED
            assert not writers
        finally:
            resume.set()
            if not refresh.done():
                refresh.cancel()
                await asyncio.gather(refresh, return_exceptions=True)
            await coordinator.api.async_shutdown()
