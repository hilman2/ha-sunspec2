"""What holds for every blueprint: where it says it comes from and where the docs point.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

import json
import re
from typing import Any
from urllib.parse import quote

import pytest
from homeassistant.components.blueprint import models as blueprint_models
from homeassistant.util import yaml as yaml_util

from .blueprint_helpers import BLUEPRINT_FOLDER
from .blueprint_helpers import BLUEPRINT_NAMES
from .blueprint_helpers import REPOSITORY

GITHUB = "https://github.com/hilman2/ha-sunspec2/blob/main"
GUIDE = (REPOSITORY / "docs" / "blueprints.md").read_text()
README = (REPOSITORY / "README.md").read_text()


def _heading_anchors(markdown: str) -> set[str]:
    """The anchors GitHub gives the headings of a Markdown page."""
    anchors = set()
    for heading in re.findall(r"^#+ (.+)$", markdown, flags=re.MULTILINE):
        kept = re.sub(r"[^a-z0-9 -]", "", heading.lower())
        anchors.add(kept.replace(" ", "-"))
    return anchors


def _blueprint(name: str) -> dict[str, Any]:
    """The ``blueprint`` block of a file: its name, description, source and inputs."""
    block: dict[str, Any] = yaml_util.load_yaml_dict(
        BLUEPRINT_FOLDER / "sunspec2" / f"{name}.yaml"
    )["blueprint"]
    return block


def test_the_blueprints_are_the_five_the_readme_offers():
    assert BLUEPRINT_NAMES == [
        "block_battery_discharge",
        "charge_battery_at_low_price",
        "follow_house_consumption",
        "inverter_alert",
        "limit_at_negative_price",
    ]


@pytest.mark.parametrize("name", BLUEPRINT_NAMES)
def test_a_blueprint_names_the_file_it_is_imported_from(name):
    """Home Assistant re-imports a blueprint from its source_url."""
    blueprint = _blueprint(name)

    assert blueprint["source_url"] == f"{GITHUB}/blueprints/automation/sunspec2/{name}.yaml"
    assert blueprint["domain"] == "automation"
    assert blueprint["name"].startswith("SunSpec: ")


@pytest.mark.parametrize("name", BLUEPRINT_NAMES)
def test_a_blueprint_needs_the_home_assistant_the_integration_needs(name):
    hacs = json.loads((REPOSITORY / "hacs.json").read_text())

    assert _blueprint(name)["homeassistant"]["min_version"] == hacs["homeassistant"]


@pytest.mark.parametrize("name", BLUEPRINT_NAMES)
def test_a_blueprint_links_to_its_section_of_the_guide(name):
    """The link in the description is what a user clicks inside Home Assistant."""
    description = _blueprint(name)["description"]

    (anchor,) = re.findall(
        re.escape(f"{GITHUB}/docs/blueprints.md#") + r"([a-z0-9-]+)", description
    )
    assert anchor in _heading_anchors(GUIDE)
    assert f"(docs/blueprints.md#{anchor})" in README


@pytest.mark.parametrize("name", BLUEPRINT_NAMES)
def test_the_readme_has_an_import_link_for_a_blueprint(name):
    address = quote(f"{GITHUB}/blueprints/automation/sunspec2/{name}.yaml", safe="")

    assert (
        f"https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url={address}" in README
    )


def test_the_guide_links_to_headings_that_exist():
    """Links from the guide into the other pages of docs/, and into itself."""
    for page, anchor in re.findall(r"\]\(([a-z-]*\.md)?#([a-z0-9-]+)\)", GUIDE):
        target = (REPOSITORY / "docs" / page).read_text() if page else GUIDE
        # A page may also set an anchor by hand, with <a id="...">.
        assert anchor in _heading_anchors(target) or f'<a id="{anchor}">' in target, (page, anchor)


@pytest.mark.parametrize("name", BLUEPRINT_NAMES)
def test_a_blueprint_is_the_same_after_home_assistant_has_stored_it(name):
    """An import does not keep the file: Home Assistant parses it and writes its own.

    The blueprints use what plain YAML offers, an anchor for a template
    that is needed twice among it, and all of that has to come out of
    Home Assistant's writer as the same automation.
    """
    path = BLUEPRINT_FOLDER / "sunspec2" / f"{name}.yaml"
    data = yaml_util.load_yaml_dict(path)
    blueprint = blueprint_models.Blueprint(
        data, expected_domain="automation", path=path.name, schema=lambda config: config
    )

    assert yaml_util.parse_yaml(blueprint.yaml()) == data
