import copy
from pathlib import Path

import yaml

from scripts.validate_addon_config import validate
from addon.start import configure


def test_addon_config_schema_is_supported():
    config = yaml.safe_load(Path("addon/config.yaml").read_text(encoding="utf-8"))
    assert validate(config) == []


def test_invalid_schema_type_is_rejected():
    config = yaml.safe_load(Path("addon/config.yaml").read_text(encoding="utf-8"))
    broken = copy.deepcopy(config)
    broken["schema"]["CACHE_MB"] = "PORT"
    assert "schema.CACHE_MB: unsupported Home Assistant type 'PORT'" in validate(broken)


def test_nested_options_and_translations_cover_schema():
    config = yaml.safe_load(Path("addon/config.yaml").read_text(encoding="utf-8"))
    translation = yaml.safe_load(Path("addon/translations/en.yaml").read_text(encoding="utf-8"))
    assert set(translation["configuration"]) == set(config["schema"])
    for group in ("debug", "ai"):
        assert set(translation["configuration"][group]["fields"]) == set(config["schema"][group])
    assert set(translation["network"]) == set(config["ports"])


def test_nested_options_reach_application_environment():
    env = {}
    configure({"debug": {"DEBUG_ENABLED": False},
               "ai": {"AI_SEARCH_ENABLED": True, "GEMINI_API_KEY": "secret"}}, env)
    assert env["DEBUG_ENABLED"] == "False"
    assert env["AI_SEARCH_ENABLED"] == "True"
    assert env["GEMINI_API_KEY"] == "secret"
