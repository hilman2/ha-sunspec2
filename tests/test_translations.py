"""Every translation against en.json, the file the others follow.

Home Assistant fills a string missing from a language with the English
one, so a gap does not break anything and goes unnoticed. A placeholder
missing from a translation leaves the value out of the message, and a
misspelt one raises when Home Assistant formats the string.

Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from homeassistant.generated.languages import LANGUAGES as HA_LANGUAGES

from .const import LANGUAGES

_TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "sunspec2" / "translations"


def _strings(tree: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten a translation file to dotted key and string."""
    flat: dict[str, str] = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            flat.update(_strings(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def _load(language: str) -> dict[str, str]:
    return _strings(json.loads((_TRANSLATIONS / f"{language}.json").read_text("utf-8")))


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_file_name_is_a_language_home_assistant_offers(language):
    """Norwegian is ``nb``: a file named ``no.json`` would never be loaded."""
    assert language in HA_LANGUAGES


@pytest.mark.parametrize("language", [lang for lang in LANGUAGES if lang != "en"])
def test_a_translation_has_the_strings_of_en_json_and_their_placeholders(language):
    english = _load("en")
    translated = _load(language)

    assert translated.keys() == english.keys()
    for key, text in english.items():
        expected = set(re.findall(r"{(\w+)}", text))
        assert set(re.findall(r"{(\w+)}", translated[key])) == expected, key
