"""Pick the pytest-homeassistant-custom-component release to test against.

Run by the pytest and mypy jobs. Prints two lines for
``$GITHUB_OUTPUT``::

    ha=2026.8.3
    phcc=0.13.357

The indirection exists because pytest-homeassistant-custom-component
pins one exact Home Assistant version in its metadata, and its newest
release always tracks the next HA beta. So the choice of test target is
made by choosing a pytest-haX version, not by constraining Home
Assistant, and a static constraint on either goes stale the moment HA
ships a new month. Asking PyPI at run time is what keeps the gate
pointed at what users actually run.

``--channel stable`` picks the newest release pinning a final HA,
``--channel beta`` the newest release overall, which is a prerelease
except in the days right after a stable HA lands.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

INDEX = "https://pypi.org/pypi/pytest-homeassistant-custom-component/json"
RELEASE = "https://pypi.org/pypi/pytest-homeassistant-custom-component/{version}/json"

# Nothing below this resolves on Python 3.14: older releases pull an
# aiodns 3.5.0 whose pycares type annotation fails on import. Same floor
# the workflows used to carry as a literal.
MINIMUM_PHCC = (0, 13, 316)

# How many releases back to inspect. Each one costs a request, and the
# newest stable pin has never been more than a handful of betas old.
LOOKBACK = 25


def _get(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return json.load(response)


def _version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


def _is_prerelease(version: str) -> bool:
    """True for a Home Assistant beta, e.g. 2026.9.0b4.

    HA's only prerelease form is a ``b`` suffix on the patch number, so
    a digits-and-dots check is enough and pulls in no dependency.
    """
    return not all(part.isdigit() for part in version.split("."))


def _pinned_ha(phcc_version: str) -> str | None:
    metadata = _get(RELEASE.format(version=phcc_version))
    info = metadata["info"]
    requirements = info.get("requires_dist") or [] if isinstance(info, dict) else []
    for requirement in requirements:
        if isinstance(requirement, str) and requirement.lower().startswith("homeassistant=="):
            return requirement.split("==", 1)[1].strip()
    return None


def resolve(channel: str) -> tuple[str, str]:
    """Return ``(ha_version, phcc_version)`` for the channel.

    Raises RuntimeError if PyPI answers but nothing matches, which means
    the assumption behind this script no longer holds and the workflow
    should stop rather than silently test something else.
    """
    index = _get(INDEX)
    releases = index["releases"]
    if not isinstance(releases, dict):
        raise RuntimeError("PyPI returned no release list")

    candidates = sorted(
        (v for v in releases if releases[v] and _version_tuple(v) >= MINIMUM_PHCC),
        key=_version_tuple,
        reverse=True,
    )[:LOOKBACK]

    for phcc_version in candidates:
        ha_version = _pinned_ha(phcc_version)
        if ha_version is None:
            continue
        if channel == "beta" or not _is_prerelease(ha_version):
            return ha_version, phcc_version

    raise RuntimeError(f"no pytest-haX release in the last {LOOKBACK} pins a {channel} HA")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    args = parser.parse_args()

    ha_version, phcc_version = resolve(args.channel)

    lines = [f"ha={ha_version}", f"phcc={phcc_version}"]
    print("\n".join(lines))

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (urllib.error.URLError, RuntimeError) as exc:
        # Loud on purpose. A resolution that quietly falls back to a
        # default is how the gate ends up on a release nobody runs.
        print(f"could not resolve versions: {exc}", file=sys.stderr)
        sys.exit(1)
