"""Vendor profiles: what a manufacturer does beyond the SunSpec text.

The generic entities stay as they are for every device. A profile adds
entities that only make sense with the vendor's reading of a model, and
hides generic ones that would write the same register in another unit.
The profile is picked from ``Mn`` in common model 1 once per connection.

Only Fronius so far. See ``profile.py`` for the shape, and the vendor
modules for the knowledge and its sources.
"""

from __future__ import annotations

from .fronius import FRONIUS
from .profile import ModuleRole
from .profile import VendorProfile
from .profile import WriteStep
from .profile import plan_write

PROFILES: tuple[VendorProfile, ...] = (FRONIUS,)


def profile_for(manufacturer: str | None) -> VendorProfile | None:
    """The profile for a manufacturer string, or None for a device we only know generically."""
    for profile in PROFILES:
        if profile.matches(manufacturer):
            return profile
    return None


__all__ = ["PROFILES", "ModuleRole", "VendorProfile", "WriteStep", "plan_write", "profile_for"]
