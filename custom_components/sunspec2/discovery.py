"""Active network discovery for SunSpec inverters.

Why this exists alongside the DHCP discovery in manifest.json:

DHCP discovery requires HA to actually see a fresh DHCP lease land in
the network. With the typical home setup that's roughly never:

* most home routers hand out 8 h+ leases, so a renewal arrives at most
  every ~4 h. If HA is not running at exactly that moment, no event.
* every inverter installation guide tells the user to give the device
  a static IP or a DHCP reservation. Static IPs never trigger any
  DHCP packet at all, ever, so DHCP discovery is dead by design.
* on a fresh HA install the user pays for one full lease cycle of
  uncertainty before discovery has a chance to fire.

The active scan does not depend on any of that. The user explicitly
clicks "Scan network" in the config flow, we open a TCP probe to
port 502 on every host in the chosen subnet (no Modbus bytes sent,
just is-the-port-open?), and then ARP-look-up each responder against
the same SunSpec vendor OUI list that the manifest's dhcp section
uses. Hosts whose MAC matches a known vendor float to the top of
the result list as "almost certainly a SunSpec inverter"; the rest
go below as "modbus device, could be SunSpec".

Risk model: a TCP SYN/ACK/FIN cycle on port 502 is not Modbus traffic
and will not steal any single-slot device's connection in the way an
actual modbus_read would. The probe is bounded by a per-host timeout
and a global concurrency cap so a /24 finishes in well under 10 s.
The whole thing only runs on explicit user click, never automatic.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
from dataclasses import dataclass

from homeassistant.components.network import async_get_adapters
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Vendor MAC OUIs that signal a SunSpec-capable inverter. Mirrors the
# DHCP discovery list in manifest.json, normalised to lowercase
# colon-separated form so they compare cleanly against the output of
# `ip neigh show` and friends. If you add an OUI here, add it in
# manifest.json too (and the other way round) so passive DHCP and
# active scan stay in sync.
SUNSPEC_VENDOR_OUIS: frozenset[str] = frozenset(
    {
        "00:27:02",  # SolarEdge Technologies
        "84:d6:c5",  # SolarEdge Technologies (second range)
        "00:15:bb",  # SMA Solar Technology AG
        "00:03:ac",  # Fronius
        "ac:19:9f",  # Sungrow Power Supply
        "f0:c1:ce",  # GoodWe Technologies
        "00:21:48",  # KACO Solar Korea
        "24:0b:b1",  # Kostal Industrie Elektrik (DE)
        "18:97:f1",  # Kostal Shanghai Mgmt
        "f0:7f:0c",  # Leopold Kostal (parent company)
        "00:06:6e",  # Delta Electronics
        "00:18:23",  # Delta Electronics
        "5c:8d:e5",  # Delta Electronics
        "20:f8:5e",  # Delta Electronics
        "00:0a:5b",  # Power-One (Norway)
        "dc:57:26",  # Power-One (Italy, ABB Solar)
        "00:22:f2",  # SunPower Corp
        "34:ad:e4",  # Chint Power Systems
        # Microchip Technology - generic embedded ethernet/wifi chip
        # OUI used by KACO Powador (DE / Siemens) ethernet loggers,
        # plus a few other inverter brands and a long tail of unrelated
        # IoT devices. Match is therefore not specific to inverters,
        # but on a typical home LAN the false-positive rate is low and
        # for KACO users it is the only OUI that catches their device.
        # The user picks from the candidate list anyway, so a stray
        # Microchip-based device showing up is harmless.
        "00:1e:c0",
    }
)

MODBUS_TCP_PORT = 502
PROBE_TIMEOUT_SECONDS = 1.5
MAX_CONCURRENT_PROBES = 50
ARP_LOOKUP_TIMEOUT_SECONDS = 2.0
# Soft cap. /24 = 254, /23 = 510. Anything bigger than /22 (1022 hosts)
# starts to feel slow even at 50 parallel probes and rarely matches
# the user's intent for a "scan my LAN" click.
MAX_HOSTS_PER_SCAN = 1022


@dataclass(frozen=True)
class SunSpecCandidate:
    """One host that responded on Modbus TCP port 502."""

    ip: str
    mac: str | None  # ``None`` if the ARP lookup failed (e.g. container)
    vendor_match: bool  # True if MAC's OUI is in SUNSPEC_VENDOR_OUIS


async def async_get_default_subnet(hass: HomeAssistant) -> str | None:
    """Return the user's default LAN subnet in CIDR notation, or None.

    Walks HA's network adapter list, picks the default-routed enabled
    adapter, and turns its first IPv4 address + prefix into a CIDR
    string. Sanity-clamps anything wider than /16 to None so we
    never offer to scan a million addresses by accident.
    """
    adapters = await async_get_adapters(hass)
    for adapter in adapters:
        if not adapter.get("enabled") or not adapter.get("default"):
            continue
        for v4 in adapter.get("ipv4", []):
            address = v4.get("address")
            prefix = v4.get("network_prefix")
            if address is None or prefix is None:
                continue
            try:
                net = ipaddress.IPv4Network(f"{address}/{prefix}", strict=False)
            except ValueError:
                continue
            # Refuse to suggest scanning a corporate /16 - that's not
            # what a home user means by "my LAN" and would take ages.
            if net.prefixlen < 22:
                continue
            return str(net)
    return None


async def _probe_port_502(ip: str, semaphore: asyncio.Semaphore) -> bool:
    """Return True if a TCP connect to ip:502 succeeds within timeout.

    No Modbus bytes are sent. The connection is closed immediately
    after the kernel-level handshake completes, so any single-slot
    Modbus device sees this as a stray TCP touch and not as an
    actual modbus session that would steal its slot.
    """
    async with semaphore:
        try:
            connect_coro = asyncio.open_connection(ip, MODBUS_TCP_PORT)
            _reader, writer = await asyncio.wait_for(connect_coro, timeout=PROBE_TIMEOUT_SECONDS)
        except (TimeoutError, OSError):
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True


async def _arp_lookup(ip: str) -> str | None:
    """Look up the MAC for ``ip`` in the kernel ARP cache, best-effort.

    Tries iproute2's ``ip neigh show <ip>`` first because it works on
    every recent Linux. Returns ``None`` if iproute2 is missing,
    times out, or the host has no entry. Container installs without
    host networking will simply get ``None`` for every address - the
    caller treats that as "no vendor info available", not as an
    error.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip",
            "neigh",
            "show",
            ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=ARP_LOOKUP_TIMEOUT_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None
    line = stdout.decode("utf-8", errors="ignore").strip()
    # Format: "192.168.1.42 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
    match = re.search(r"lladdr\s+([0-9a-fA-F:]{17})", line)
    if match:
        return match.group(1).lower()
    return None


def _is_vendor_match(mac: str | None) -> bool:
    """Return True if mac's OUI is in our SunSpec vendor list."""
    if mac is None:
        return False
    return mac[:8].lower() in SUNSPEC_VENDOR_OUIS


async def async_discover_sunspec_candidates(
    hass: HomeAssistant, subnet: str
) -> list[SunSpecCandidate]:
    """Scan ``subnet`` for hosts with Modbus TCP port 502 open.

    Returns the candidates sorted vendor-matches-first. Raises
    :class:`ValueError` if subnet is malformed or larger than the
    soft cap (``MAX_HOSTS_PER_SCAN``).
    """
    try:
        network = ipaddress.IPv4Network(subnet, strict=False)
    except ValueError as exc:
        raise ValueError(f"Invalid subnet {subnet!r}: {exc}") from exc

    if network.num_addresses > MAX_HOSTS_PER_SCAN + 2:
        raise ValueError(
            f"Subnet {subnet} has {network.num_addresses} addresses; "
            f"refusing to scan more than {MAX_HOSTS_PER_SCAN}. "
            "Use a smaller CIDR (e.g. /24)."
        )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)
    hosts = [str(h) for h in network.hosts()]
    _LOGGER.info("Scanning %d hosts in %s for SunSpec inverters", len(hosts), subnet)

    open_results = await asyncio.gather(*[_probe_port_502(ip, semaphore) for ip in hosts])
    open_hosts = [ip for ip, ok in zip(hosts, open_results, strict=True) if ok]
    _LOGGER.info("%d host(s) responded on port 502", len(open_hosts))

    candidates: list[SunSpecCandidate] = []
    for ip in open_hosts:
        mac = await _arp_lookup(ip)
        candidates.append(SunSpecCandidate(ip=ip, mac=mac, vendor_match=_is_vendor_match(mac)))

    # Sort: vendor matches first (more likely to be the inverter the
    # user is looking for), then by IP for a stable order.
    candidates.sort(key=lambda c: (not c.vendor_match, c.ip))
    return candidates
