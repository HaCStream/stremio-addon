import copy
from pathlib import Path

import yaml

from scripts.validate_addon_config import validate


def test_addon_config_schema_is_supported():
    config = yaml.safe_load(Path("addon/config.yaml").read_text(encoding="utf-8"))
    assert validate(config) == []


def test_uppercase_port_validator_is_rejected():
    config = yaml.safe_load(Path("addon/config.yaml").read_text(encoding="utf-8"))
    broken = copy.deepcopy(config)
    broken["schema"]["PORT"] = "PORT"
    assert "schema.PORT: unsupported Home Assistant type 'PORT'" in validate(broken)
