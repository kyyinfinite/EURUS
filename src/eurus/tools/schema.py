from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

REQUIRED_PROPERTY = re.compile(r"^'(.*)' is a required property$")
UNEXPECTED_PROPERTIES = re.compile(
    r"Additional properties are not allowed \((.*) (?:was|were) unexpected\)$"
)


def _render_location(root: str, segments: Iterable[Any]) -> str:
    location = root
    for segment in segments:
        if isinstance(segment, bool):
            location = f"{location}.{segment}"
        elif isinstance(segment, int):
            location = f"{location}[{segment}]"
        else:
            location = f"{location}.{segment}"
    return location


def _type_label(expected: Any) -> str:
    if isinstance(expected, list):
        return " or ".join(str(item) for item in expected)
    return str(expected)


def _unexpected_keys(error: ValidationError) -> list[str]:
    match = UNEXPECTED_PROPERTIES.match(error.message)
    if not match:
        return []
    raw_keys = match.group(1)
    return [key.strip("'\"") for key in raw_keys.split(", ")]


def _format_error(root: str, error: ValidationError) -> list[str]:
    location = _render_location(root, error.path)
    keyword = error.validator
    limit = error.validator_value
    if keyword == "required":
        match = REQUIRED_PROPERTY.match(error.message)
        if match:
            return [f"{location}.{match.group(1)} is required"]
        return [f"{location} is missing required properties"]
    if keyword == "type":
        return [f"{location} must be {_type_label(limit)}"]
    if keyword == "enum":
        return [f"{location} must be one of {limit}"]
    if keyword == "const":
        return [f"{location} must be {limit!r}"]
    if keyword == "additionalProperties":
        keys = _unexpected_keys(error)
        if keys:
            return [f"{location}.{key} is not allowed" for key in keys if key]
        return [f"{location} has properties that are not allowed"]
    if keyword == "minimum":
        return [f"{location} must be at least {limit}"]
    if keyword == "maximum":
        return [f"{location} must be at most {limit}"]
    if keyword == "exclusiveMinimum":
        return [f"{location} must be greater than {limit}"]
    if keyword == "exclusiveMaximum":
        return [f"{location} must be less than {limit}"]
    if keyword == "multipleOf":
        return [f"{location} must be a multiple of {limit}"]
    if keyword == "minLength":
        return [f"{location} must be at least {limit} characters"]
    if keyword == "maxLength":
        return [f"{location} must be at most {limit} characters"]
    if keyword == "pattern":
        return [f"{location} must match pattern {limit!r}"]
    if keyword == "minItems":
        return [f"{location} must have at least {limit} items"]
    if keyword == "maxItems":
        return [f"{location} must have at most {limit} items"]
    if keyword == "uniqueItems":
        return [f"{location} must contain unique items"]
    if keyword == "minProperties":
        return [f"{location} must have at least {limit} properties"]
    if keyword == "maxProperties":
        return [f"{location} must have at most {limit} properties"]
    if keyword in {"oneOf", "anyOf"}:
        return [f"{location} is not valid against any of the expected schemas"]
    if keyword == "allOf":
        return [f"{location} is not valid against all of the expected schemas"]
    if keyword == "not":
        return [f"{location} is not allowed by the schema"]
    if keyword == "prefixItems":
        return [f"{location} does not match the expected item schemas"]
    if keyword == "dependentRequired":
        return [f"{location} is missing properties required by its siblings"]
    if keyword == "dependentSchemas":
        return [f"{location} does not match the schemas required by its siblings"]
    if keyword == "propertyNames":
        return [f"{location} has a property name that is not allowed"]
    if keyword == "contains":
        return [f"{location} must contain at least one matching item"]
    return [f"{location}: {error.message}"]


def validate_arguments(schema: dict[str, Any], value: Any, path: str = "arguments") -> list[str]:
    if not isinstance(schema, dict):
        return [f"{path} schema must be an object"]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [f"{path} schema is invalid: {exc.message}"]
    validator = Draft202012Validator(schema)
    try:
        errors = list(validator.iter_errors(value))
    except Exception as exc:
        return [f"{path} schema could not be evaluated: {exc}"]
    problems: list[str] = []
    for error in errors:
        problems.extend(_format_error(path, error))
    return problems


def validate_tool_output(schema: dict[str, Any], text: str) -> list[str]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return ["output must be valid JSON when an output schema is declared"]
    return validate_arguments(schema, payload, path="output")
