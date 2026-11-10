"""Validate renderer specs and produce actionable, scene-numbered errors."""
from __future__ import annotations

import difflib
import json
import sys
from copy import deepcopy
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.json")


class SpecError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def _suggest(value, options) -> str:
    if not isinstance(value, str):
        return ""
    match = difflib.get_close_matches(value, [str(option) for option in options],
                                      n=1, cutoff=0.6)
    return f" — did you mean '{match[0]}'?" if match else ""


def _path(path) -> str:
    result = ""
    for part in path:
        result += f"[{part}]" if isinstance(part, int) else (f".{part}" if result else str(part))
    return result


def _type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, (int, float)):
        return "a whole number" if isinstance(value, int) else "a number"
    return type(value).__name__


def _describe(error) -> str:
    location = _path(error.absolute_path)
    field = f"'{location}'" if location else "spec"
    rule = error.validator
    if rule == "required":
        missing = error.message.split("'")[1]
        prefix = f"{location}." if location else ""
        return f"missing required field '{prefix}{missing}'"
    if rule == "additionalProperties":
        allowed = error.schema.get("properties", {})
        extras = [key for key in error.instance if key not in allowed]
        base = f" in '{location}'" if location else ""
        return "; ".join(
            f"unknown field '{key}'{base}{_suggest(key, allowed)}" for key in extras
        )
    if rule in ("enum", "const"):
        choices = error.validator_value if rule == "enum" else [error.validator_value]
        values = ", ".join(repr(choice) for choice in choices)
        return (f"{field} must be one of {values} (got {error.instance!r})"
                f"{_suggest(error.instance, choices)}")
    if rule == "type":
        return f"{field} should be {error.validator_value}, got {_type_name(error.instance)}"
    if rule == "minItems" and location == "timeline":
        return "timeline is empty — add at least one scene"
    if rule in ("minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum"):
        return f"{field} {error.message}"
    return f"{field}: {error.message}"


def validate_spec(spec, schema: dict | None = None) -> list[str]:
    """Return all user-readable validation problems; [] means valid."""
    try:
        from jsonschema import Draft7Validator
    except ImportError as exc:
        raise SpecError(["jsonschema is not installed; run 'pip install -r requirements.txt'"]) from exc

    schema = schema or json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        return [f"spec must be an object, got {_type_name(spec)}"]

    # Validate the document envelope separately so each scene can be checked
    # against its own definition and report useful errors instead of `oneOf`.
    envelope = deepcopy(schema)
    envelope["properties"]["timeline"] = {
        key: value for key, value in schema["properties"]["timeline"].items()
        if key != "items"
    }
    problems = [
        _describe(error)
        for error in sorted(Draft7Validator(envelope).iter_errors(spec),
                            key=lambda item: tuple(map(str, item.absolute_path)))
    ]

    timeline = spec.get("timeline")
    if not isinstance(timeline, list):
        return problems
    definitions = schema["definitions"]
    scene_types = [item["$ref"].rsplit("/", 1)[-1]
                   for item in definitions["scene"]["oneOf"]]
    for index, scene in enumerate(timeline):
        label = f"scene {index}"
        if not isinstance(scene, dict):
            problems.append(f"{label}: should be an object, got {_type_name(scene)}")
            continue
        scene_type = scene.get("type")
        if scene_type is None:
            problems.append(f"{label}: missing required field 'type' (one of {', '.join(scene_types)})")
            continue
        if scene_type not in scene_types:
            problems.append(f"{label}: unknown type {scene_type!r}"
                            f"{_suggest(scene_type, scene_types)} (valid types: {', '.join(scene_types)})")
            continue
        validator_schema = {"$ref": f"#/definitions/{scene_type}",
                            "definitions": definitions}
        validator = Draft7Validator(validator_schema)
        for error in sorted(validator.iter_errors(scene),
                            key=lambda item: tuple(map(str, item.absolute_path))):
            problems.append(f"{label} ({scene_type}): {_describe(error)}")
        if scene_type == "bullets" and isinstance(scene.get("narration_per_bullet"), list):
            if len(scene["narration_per_bullet"]) != len(scene.get("items", [])):
                problems.append(f"{label} (bullets): narration_per_bullet must have one entry per item")
        if scene_type == "chart":
            labels, values = scene.get("labels"), scene.get("values")
            if isinstance(labels, list) and isinstance(values, list) and len(labels) != len(values):
                problems.append(f"{label} (chart): labels and values must have the same number of entries")
            if scene.get("chart_type") == "pie" and isinstance(values, list) and any(
                    isinstance(value, (int, float)) and value < 0 for value in values):
                problems.append(f"{label} (chart): pie values cannot be negative")
    return problems


def load_and_validate(path: str | Path) -> dict:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError([f"cannot read spec file {path}: {exc.strerror or exc}"]) from None
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError([f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"]) from None
    problems = validate_spec(spec)
    if problems:
        raise SpecError(problems)
    return spec


def report(path: str | Path, error: SpecError) -> str:
    count = len(error.errors)
    lines = [f"[spec] {path}: {count} problem{'s' if count != 1 else ''} found"]
    return "\n".join(lines + [f"  - {problem}" for problem in error.errors])


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python3 src/validate.py SPEC.json", file=sys.stderr)
        return 2
    try:
        spec = load_and_validate(argv[0])
    except SpecError as error:
        print(report(argv[0], error), file=sys.stderr)
        return 1
    print(f"[spec] {argv[0]}: OK ({len(spec['timeline'])} scenes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
