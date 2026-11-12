"""Active network scan for SunSpec inverters.

The DHCP discovery in manifest.json only fires when Home Assistant sees
a DHCP packet of the device. A device with a static address never sends
one, and a device with a lease renews it only every few hours. The scan
in this module does not depend on a lease.

The scan runs only when the user chooses the network scan in the config
flow, never on its own. It opens a TCP connection to port 502 on every
host of the chosen subnet, sends no Modbus bytes, and looks up the MAC
address of every host that answers. Hosts whose MAC prefix is in
``SUNSPEC_VENDOR_OUIS`` come first in the result, as likely SunSpec
inverters; the other hosts follow as Modbus devices that may be SunSpec.
A per-host timeout and a concurrency cap bound the scan.
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

# Vendor MAC OUIs that point to a SunSpec-capable inverter, in the
# lowercase colon-separated form that `ip neigh show` prints. The set
# mirrors the dhcp list in manifest.json: an OUI added here has to be
# added there too, and the other way round.
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
        "80:97:1b",  # Altenergy Power System (APsystems ECU)
        # Microchip Technology: a generic embedded Ethernet and Wi-Fi OUI,
        # used by the KACO Powador Ethernet loggers, a few other inverter
        # brands and many unrelated IoT devices. A match is therefore not
        # specific to inverters, but it is the only OUI that catches a
        # KACO. The user picks from the candidate list anyway, so a stray
        # Microchip device in the list is harmless.
        "00:1e:c0",
    }
)

MODBUS_TCP_PORT = 502
PROBE_TIMEOUT_SECONDS = 1.5
MAX_CONCURRENT_PROBES = 50
ARP_LOOKUP_TIMEOUT_SECONDS = 2.0
# The most hosts one scan probes: a /22. A wider network is slow even at
# 50 parallel probes and is rarely what a user means by "scan my LAN".
MAX_HOSTS_PER_SCAN = 1022


@dataclass(frozen=True)
class SunSpecCandidate:
    """One host that responded on Modbus TCP port 502."""

    ip: str
    mac: str | None  # ``None`` if the ARP lookup failed (e.g. container)
    vendor_match: bool  # True if MAC's OUI is in SUNSPEC_VENDOR_OUIS


async def async_get_default_subnet(hass: HomeAssistant) -> str | None:
    """Return the subnet of the default network adapter in CIDR notation, or None.

    The subnet comes from the first IPv4 address of the enabled adapter
    with the default route whose network is a /22 or narrower. A wider
    network is not offered.
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
            # A network wider than /22 has more hosts than
            # MAX_HOSTS_PER_SCAN, and the scan would refuse it.
            if net.prefixlen < 22:
                continue
            return str(net)
    return None


async def _probe_port_502(ip: str, semaphore: asyncio.Semaphore) -> bool:
    """Return True if a TCP connect to ``ip`` on port 502 succeeds within the timeout."""
    # No Modbus bytes are sent, and the connection is closed as soon as
    # the TCP handshake completes. A device that grants one Modbus
    # session sees a stray TCP touch, not a session that takes its slot.
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
    """Return the MAC address for ``ip`` from the kernel's neighbour table, or None.

    The lookup runs iproute2's ``ip neigh show <ip>``. It returns None
    when iproute2 is missing, when the call times out or when the host
    has no entry. A container install without host networking gets None
    for every address; the caller treats None as "no vendor information",
    not as an error.
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
    """Return True if the OUI of ``mac`` is in ``SUNSPEC_VENDOR_OUIS``."""
    if mac is None:
        return False
    return mac[:8].lower() in SUNSPEC_VENDOR_OUIS


async def async_discover_sunspec_candidates(
    hass: HomeAssistant, subnet: str
) -> list[SunSpecCandidate]:
    """Scan ``subnet`` for hosts with Modbus TCP port 502 open.

    Returns the candidates, vendor matches first, then ordered by the
    address as text.

    Raises:
        ValueError: ``subnet`` is malformed or has more hosts than
            ``MAX_HOSTS_PER_SCAN``.
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

    # A vendor match is more likely the inverter the user is looking for.
    candidates.sort(key=lambda c: (not c.vendor_match, c.ip))
    return candidates
