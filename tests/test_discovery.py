"""Unit tests for the active SunSpec network discovery helpers."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from custom_components.sunspec2.discovery import SUNSPEC_VENDOR_OUIS
from custom_components.sunspec2.discovery import SunSpecCandidate
from custom_components.sunspec2.discovery import _arp_lookup
from custom_components.sunspec2.discovery import _is_vendor_match
from custom_components.sunspec2.discovery import _probe_port_502
from custom_components.sunspec2.discovery import async_discover_sunspec_candidates
from custom_components.sunspec2.discovery import async_get_default_subnet


def test_vendor_match_recognises_known_oui():
    """A MAC whose first three bytes are in our SunSpec OUI list matches."""
    # Pull a fresh OUI from the live set so this test stays in sync if
    # the list is reordered. We just need any one entry.
    sample_oui = next(iter(SUNSPEC_VENDOR_OUIS))
    fake_mac = f"{sample_oui}:de:ad:be"
    assert _is_vendor_match(fake_mac) is True


def test_manifest_dhcp_ouis_match_the_scan_list():
    """Passive DHCP discovery and the active scan know the same vendors."""
    manifest = json.loads(
        (Path(__file__).parent.parent / "custom_components/sunspec2/manifest.json").read_text()
    )
    manifest_ouis = {
        f"{mac[0:2]}:{mac[2:4]}:{mac[4:6]}".lower()
        for mac in (item["macaddress"].removesuffix("*") for item in manifest["dhcp"])
    }
    assert manifest_ouis == SUNSPEC_VENDOR_OUIS


def test_vendor_match_rejects_unknown_oui():
    """A MAC outside the OUI list (e.g. an Intel NUC) does not match."""
    # Intel: 00:1b:21 - definitely not in our SunSpec list.
    assert _is_vendor_match("00:1b:21:aa:bb:cc") is False


def test_vendor_match_handles_none_mac():
    """ARP lookup may return None on container installs - that's not a match."""
    assert _is_vendor_match(None) is False


def test_vendor_match_is_case_insensitive():
    """MAC strings from different sources may use upper or lower case."""
    sample_oui = next(iter(SUNSPEC_VENDOR_OUIS))
    upper = f"{sample_oui.upper()}:DE:AD:BE"
    lower = f"{sample_oui.lower()}:de:ad:be"
    assert _is_vendor_match(upper) is True
    assert _is_vendor_match(lower) is True


async def test_discover_rejects_oversized_subnet(hass):
    """Asking for a /16 (65k hosts) must be refused, not silently scanned.

    Without the soft cap a misconfigured user could trigger a 65k-host
    parallel TCP scan that takes minutes and may upset other devices.
    """
    with pytest.raises(ValueError, match="refusing to scan"):
        await async_discover_sunspec_candidates(hass, "10.0.0.0/16")


async def test_discover_rejects_malformed_subnet(hass):
    """Garbage in the CIDR field must raise ValueError, not crash."""
    with pytest.raises(ValueError, match="Invalid subnet"):
        await async_discover_sunspec_candidates(hass, "not a cidr")


async def test_discover_returns_open_hosts_sorted_vendor_first(hass):
    """Vendor-matched hosts must be sorted before non-matched ones.

    Mocks the per-host probe and ARP lookup so the test runs without
    touching the network. Verifies the sort order: vendor matches
    first, then by IP for stability.
    """
    # Two hosts in the /30: 192.168.1.1 (the gateway) and 192.168.1.2.
    # Both will respond to the probe, only one matches a vendor OUI.
    sample_oui = next(iter(SUNSPEC_VENDOR_OUIS))
    vendor_mac = f"{sample_oui}:11:22:33"
    other_mac = "00:1b:21:aa:bb:cc"

    async def fake_probe(ip, _semaphore):
        return ip in {"192.168.1.1", "192.168.1.2"}

    async def fake_arp(ip):
        return {
            "192.168.1.1": other_mac,
            "192.168.1.2": vendor_mac,
        }.get(ip)

    with (
        patch("custom_components.sunspec2.discovery._probe_port_502", new=fake_probe),
        patch(
            "custom_components.sunspec2.discovery._arp_lookup",
            new=AsyncMock(side_effect=fake_arp),
        ),
    ):
        candidates = await async_discover_sunspec_candidates(hass, "192.168.1.0/30")

    assert len(candidates) == 2
    # Vendor match first, even though its IP sorts after the other.
    assert candidates[0] == SunSpecCandidate(ip="192.168.1.2", mac=vendor_mac, vendor_match=True)
    assert candidates[1] == SunSpecCandidate(ip="192.168.1.1", mac=other_mac, vendor_match=False)


async def test_discover_returns_empty_when_nothing_responds(hass):
    """A subnet with nobody on port 502 returns an empty list, not an error."""

    async def fake_probe(_ip, _semaphore):
        return False

    with patch("custom_components.sunspec2.discovery._probe_port_502", new=fake_probe):
        candidates = await async_discover_sunspec_candidates(hass, "192.168.1.0/30")

    assert candidates == []


# ---------- the subnet offered for the scan ----------------------------------


def _adapter(*addresses, enabled=True, default=True):
    """One entry of Home Assistant's adapter list, with ``(address, prefix)`` pairs."""
    return {
        "enabled": enabled,
        "default": default,
        "ipv4": [{"address": address, "network_prefix": prefix} for address, prefix in addresses],
    }


async def _default_subnet(hass, adapters):
    with patch(
        "custom_components.sunspec2.discovery.async_get_adapters",
        new=AsyncMock(return_value=adapters),
    ):
        return await async_get_default_subnet(hass)


async def test_default_subnet_is_the_network_of_the_default_routed_adapter(hass):
    adapters = [
        _adapter(("10.9.9.9", 24), enabled=False),
        _adapter(("172.17.0.1", 24), default=False),
        _adapter(("192.168.1.37", 24)),
    ]

    assert await _default_subnet(hass, adapters) == "192.168.1.0/24"


@pytest.mark.parametrize(
    "unusable",
    [("192.168.5.2", None), (None, 24), ("not-an-address", 24), ("192.168.5.2", 99)],
)
async def test_default_subnet_skips_an_address_it_cannot_make_a_network_of(hass, unusable):
    adapters = [_adapter(unusable, ("192.168.7.9", 24))]

    assert await _default_subnet(hass, adapters) == "192.168.7.0/24"


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        (22, "192.168.4.0/22"),
        (21, None),
        (16, None),
    ],
)
async def test_default_subnet_is_not_offered_for_a_network_wider_than_the_scan_takes(
    hass, prefix, expected
):
    """A /22 is the largest scan the cap allows; a wider network is not "my LAN"."""
    assert await _default_subnet(hass, [_adapter(("192.168.4.10", prefix))]) == expected


@pytest.mark.parametrize(
    "adapters",
    [
        [],
        [_adapter(("192.168.1.37", 24), default=False)],
        [_adapter()],
    ],
)
async def test_default_subnet_is_none_without_a_default_adapter_that_has_an_address(hass, adapters):
    assert await _default_subnet(hass, adapters) is None


async def test_the_default_subnet_is_one_the_scan_accepts(hass):
    """What the form pre-fills must not be refused by the scan it is pre-filled for."""
    subnet = await _default_subnet(hass, [_adapter(("192.168.4.10", 22))])
    assert subnet is not None

    async def nobody_home(_ip, _semaphore):
        return False

    with patch("custom_components.sunspec2.discovery._probe_port_502", new=nobody_home):
        assert await async_discover_sunspec_candidates(hass, subnet) == []


# ---------- the probe ---------------------------------------------------------


async def test_probe_finds_a_listening_port_and_sends_it_nothing(socket_enabled):
    """The probe is a TCP handshake and a close, so a single-slot device loses no session."""
    received = []
    closed = asyncio.Event()

    async def serve(reader, writer):
        received.append(await reader.read())
        writer.close()
        closed.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with patch("custom_components.sunspec2.discovery.MODBUS_TCP_PORT", port):
            found = await _probe_port_502("127.0.0.1", asyncio.Semaphore(1))
        async with asyncio.timeout(5):
            await closed.wait()
    finally:
        server.close()
        await server.wait_closed()

    assert found is True
    # read() returned at end of stream: the probe hung up without a byte.
    assert received == [b""]


async def test_probe_reports_a_port_nobody_listens_on_as_closed(socket_enabled):
    with socket.socket() as placeholder:
        placeholder.bind(("127.0.0.1", 0))
        free_port = placeholder.getsockname()[1]

    with patch("custom_components.sunspec2.discovery.MODBUS_TCP_PORT", free_port):
        assert await _probe_port_502("127.0.0.1", asyncio.Semaphore(1)) is False


async def test_probe_gives_up_on_a_host_that_does_not_answer():
    """A host that drops the SYN must cost the per-host timeout, not hold the scan."""

    async def never_connects(_host, _port):
        await asyncio.Event().wait()

    with (
        patch("custom_components.sunspec2.discovery.PROBE_TIMEOUT_SECONDS", 0.01),
        patch("custom_components.sunspec2.discovery.asyncio.open_connection", new=never_connects),
    ):
        async with asyncio.timeout(5):
            assert await _probe_port_502("192.0.2.1", asyncio.Semaphore(1)) is False


async def test_scan_knocks_on_port_502_of_every_host_and_never_on_more_than_the_cap_at_once(hass):
    knocked = []
    in_flight = 0
    peak = 0

    async def refuse(host, port):
        nonlocal in_flight, peak
        knocked.append((host, port))
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        raise ConnectionRefusedError

    with (
        patch("custom_components.sunspec2.discovery.MAX_CONCURRENT_PROBES", 2),
        patch("custom_components.sunspec2.discovery.asyncio.open_connection", new=refuse),
    ):
        candidates = await async_discover_sunspec_candidates(hass, "192.168.1.0/29")

    assert candidates == []
    assert sorted(knocked) == [(f"192.168.1.{last}", 502) for last in range(1, 7)]
    assert peak == 2


# ---------- the MAC lookup ----------------------------------------------------


def _ip_neigh(stdout):
    """Stand in for the ``ip neigh show`` process: it prints ``stdout`` and exits."""
    process = Mock()
    process.communicate = AsyncMock(return_value=(stdout, None))
    return patch(
        "custom_components.sunspec2.discovery.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    )


async def test_arp_lookup_asks_the_kernel_and_returns_the_mac_in_lower_case():
    with _ip_neigh(b"192.168.1.42 dev eth0 lladdr 00:03:AC:DD:EE:FF REACHABLE\n") as run:
        assert await _arp_lookup("192.168.1.42") == "00:03:ac:dd:ee:ff"

    assert run.await_args.args == ("ip", "neigh", "show", "192.168.1.42")


@pytest.mark.parametrize(
    "stdout",
    [
        # No entry at all.
        b"",
        # An entry the kernel could not resolve carries no lladdr.
        b"192.168.1.42 dev eth0 FAILED\n",
        b"192.168.1.42 dev eth0 INCOMPLETE\n",
    ],
)
async def test_arp_lookup_has_no_mac_for_a_host_the_kernel_has_not_resolved(stdout):
    with _ip_neigh(stdout):
        assert await _arp_lookup("192.168.1.42") is None


@pytest.mark.parametrize("already_gone", [False, True])
async def test_arp_lookup_kills_a_lookup_that_does_not_come_back(already_gone):
    async def hangs():
        await asyncio.Event().wait()

    process = Mock()
    process.communicate = hangs
    # The process may have exited between the timeout and the kill.
    process.kill = Mock(side_effect=ProcessLookupError if already_gone else None)

    with (
        patch("custom_components.sunspec2.discovery.ARP_LOOKUP_TIMEOUT_SECONDS", 0.01),
        patch(
            "custom_components.sunspec2.discovery.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
    ):
        async with asyncio.timeout(5):
            assert await _arp_lookup("192.168.1.42") is None

    process.kill.assert_called_once_with()


@pytest.mark.parametrize("error", [FileNotFoundError("ip"), PermissionError("ip")])
async def test_a_scan_that_cannot_run_ip_neigh_lists_the_hosts_without_a_vendor(hass, error):
    """A container without iproute2 still finds the open ports; it just cannot rank them."""

    async def fake_probe(ip, _semaphore):
        return ip == "192.168.1.2"

    with (
        patch("custom_components.sunspec2.discovery._probe_port_502", new=fake_probe),
        patch(
            "custom_components.sunspec2.discovery.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=error),
        ),
    ):
        candidates = await async_discover_sunspec_candidates(hass, "192.168.1.0/30")

    assert candidates == [SunSpecCandidate(ip="192.168.1.2", mac=None, vendor_match=False)]


async def test_a_scan_ranks_a_host_by_the_mac_the_kernel_reports(hass):
    """End to end over the lookup: an upper-case Fronius MAC from ``ip neigh`` is a vendor match."""

    async def fake_probe(ip, _semaphore):
        return ip == "192.168.1.2"

    with (
        patch("custom_components.sunspec2.discovery._probe_port_502", new=fake_probe),
        _ip_neigh(b"192.168.1.2 dev eth0 lladdr 00:03:AC:DD:EE:FF STALE\n"),
    ):
        candidates = await async_discover_sunspec_candidates(hass, "192.168.1.0/30")

    assert candidates == [
        SunSpecCandidate(ip="192.168.1.2", mac="00:03:ac:dd:ee:ff", vendor_match=True)
    ]
