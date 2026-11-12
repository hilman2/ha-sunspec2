"""Vendor profiles: what a manufacturer does beyond the SunSpec text.

The generic entities stay as they are for every device. A profile adds
entities that only make sense with the vendor's reading of a model, and
hides generic ones that would write the same register in another unit.
The coordinator picks the profile from common model 1: ``Mn`` names the
manufacturer, and ``Md``, ``Opt`` and ``Vr`` tell a manufacturer's
generations apart.

The profiles are Fronius in two generations, SolarEdge, SMA, Kostal,
APsystems, SAJ and FranklinWH. ``profile.py`` holds the shape, the vendor
modules hold the knowledge and its sources.
"""

from __future__ import annotations

from .apsystems import APSYSTEMS
from .franklinwh import FRANKLINWH
from .fronius import FRONIUS
from .fronius_datamanager import FRONIUS_DATAMANAGER
from .kostal import KOSTAL
from .profile import ModuleRole
from .profile import VendorProfile
from .profile import WriteStep
from .profile import plan_write
from .saj import SAJ
from .sma import SMA
from .solaredge import SOLAREDGE

# Where two profiles share a manufacturer, the one that identifies its
# devices goes before the one that takes the rest.
PROFILES: tuple[VendorProfile, ...] = (
    FRONIUS_DATAMANAGER,
    FRONIUS,
    SOLAREDGE,
    SMA,
    KOSTAL,
    APSYSTEMS,
    SAJ,
    FRANKLINWH,
)


def profile_for(
    manufacturer: str | None, model: str = "", option: str = "", version: str = ""
) -> VendorProfile | None:
    """The profile for a device, or None for a device known only generically.

    Args:
        manufacturer: ``Mn`` of common model 1.
        model: ``Md``, for a manufacturer with more than one profile.
        option: ``Opt``, likewise.
        version: ``Vr``, likewise.

    Returns:
        The first profile in ``PROFILES`` that matches the manufacturer
        and whose ``identifies``, if it has one, accepts the other three.
    """
    for profile in PROFILES:
        if not profile.matches(manufacturer):
            continue
        if profile.identifies is None or profile.identifies(model, option, version):
            return profile
    return None


__all__ = ["PROFILES", "ModuleRole", "VendorProfile", "WriteStep", "plan_write", "profile_for"]
