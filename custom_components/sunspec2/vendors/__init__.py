"""Vendor profiles: what a manufacturer does beyond the SunSpec text.

The generic entities stay as they are for every device. A profile adds
entities that only make sense with the vendor's reading of a model, and
hides generic ones that would write the same register in another unit.
The profile is picked from ``Mn`` in common model 1 once per connection.

Fronius in two generations, SolarEdge and SMA so far. See ``profile.py``
for the shape, and the vendor modules for the knowledge and its sources.
"""

from __future__ import annotations

from .fronius import FRONIUS
from .fronius_datamanager import FRONIUS_DATAMANAGER
from .profile import ModuleRole
from .profile import VendorProfile
from .profile import WriteStep
from .profile import plan_write
from .sma import SMA
from .solaredge import SOLAREDGE

# Where two profiles share a manufacturer, the one that identifies its
# devices goes before the one that takes the rest.
PROFILES: tuple[VendorProfile, ...] = (FRONIUS_DATAMANAGER, FRONIUS, SOLAREDGE, SMA)


def profile_for(
    manufacturer: str | None, model: str = "", option: str = "", version: str = ""
) -> VendorProfile | None:
    """The profile for a device, or None for one we only know generically.

    Args:
        manufacturer (str|None): ``Mn`` of common model 1.
        model (str): ``Md``, for a manufacturer with more than one profile.
        option (str): ``Opt``, likewise.
        version (str): ``Vr``, likewise.

    Returns:
        VendorProfile|None: The first profile in PROFILES that matches
        the manufacturer and, where it looks at them, the other three.
    """
    for profile in PROFILES:
        if not profile.matches(manufacturer):
            continue
        if profile.identifies is None or profile.identifies(model, option, version):
            return profile
    return None


__all__ = ["PROFILES", "ModuleRole", "VendorProfile", "WriteStep", "plan_write", "profile_for"]
