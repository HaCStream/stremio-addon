"""Catch invalid Home Assistant add-on option schema types before publishing."""

import re
from pathlib import Path

import yaml


# Home Assistant's documented option types. Keep the grammar explicit so a typo
# such as `PORT: PORT` fails instead of reaching Supervisor.
TYPE = re.compile(
    r"(?:str|password|int|float)(?:\(\d*,\d*\))?"
    r"|bool|email|url|port|device(?:\([^()]*\))?"
    r"|match\(.+\)|list\([^()]+\)"
)


def validate(config):
    errors = []
    for field in ("name", "version", "slug", "description", "arch"):
        if not config.get(field):
            errors.append(f"missing required add-on field: {field}")

    options = config.get("options", {})
    schema = config.get("schema", {})
    if not isinstance(options, dict) or not isinstance(schema, dict):
        return errors + ["options and schema must be mappings"]

    def check(option_group, schema_group, prefix=""):
        for name, kind in schema_group.items():
            path = f"{prefix}{name}"
            if isinstance(kind, dict):
                if not isinstance(option_group.get(name), dict):
                    errors.append(f"options.{path}: expected a mapping")
                else:
                    check(option_group[name], kind, path + ".")
            elif not isinstance(kind, str) or not TYPE.fullmatch(kind.removesuffix("?")):
                errors.append(f"schema.{path}: unsupported Home Assistant type {kind!r}")
        for name in option_group.keys() - schema_group.keys():
            errors.append(f"options.{prefix}{name}: missing schema entry")

    check(options, schema)
    return errors


def main():
    path = Path("addon/config.yaml")
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise SystemExit(f"{path}: expected a YAML mapping")
    errors = validate(config)
    if errors:
        raise SystemExit("\n".join(f"{path}: {error}" for error in errors))
    print(f"{path}: valid add-on fields and option schema types")


if __name__ == "__main__":
    main()
