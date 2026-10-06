"""Print the pip requirements of a Home Assistant core integration, one per line.

``python ha_requirements.py modbus | xargs -d '\n' pip install``

Home Assistant installs an integration's requirements itself when it
loads the integration, and the test image never does. This integration
depends on the core ``modbus`` integration (it takes its connections
from there), so importing ``homeassistant.components.modbus`` needs what
that integration declares, pymodbus among it. The pins come from the
installed Home Assistant's own manifest rather than from a version
written down here, for the same reason ``resolve_ha_versions.py`` asks
PyPI: whatever Home Assistant pins is what its users run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <integration>", file=sys.stderr)
        return 2
    spec = importlib.util.find_spec("homeassistant")
    if spec is None or not spec.submodule_search_locations:
        print("homeassistant is not installed", file=sys.stderr)
        return 1
    manifest = Path(spec.submodule_search_locations[0]) / "components" / argv[1] / "manifest.json"
    for requirement in json.loads(manifest.read_text(encoding="utf-8")).get("requirements", []):
        print(requirement)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
