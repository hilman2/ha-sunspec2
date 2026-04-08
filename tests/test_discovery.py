"""Unit tests for the active SunSpec network discovery helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from custom_components.sunspec2.discovery import SUNSPEC_VENDOR_OUIS
from custom_components.sunspec2.discovery import SunSpecCandidate
from custom_components.sunspec2.discovery import _is_vendor_match
from custom_components.sunspec2.discovery import async_discover_sunspec_candidates


def test_vendor_match_recognises_known_oui():
    """A MAC whose first three bytes are in our SunSpec OUI list matches."""
    # Pull a fresh OUI from the live set so this test stays in sync if
    # the list is reordered. We just need any one entry.
    sample_oui = next(iter(SUNSPEC_VENDOR_OUIS))
    fake_mac = f"{sample_oui}:de:ad:be"
    assert _is_vendor_match(fake_mac) is True


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
