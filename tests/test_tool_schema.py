import pytest

from eurus.tools.schema import validate_arguments

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "count": {"type": "integer", "minimum": 1, "maximum": 5},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
    },
    "required": ["path"],
    "additionalProperties": False,
}


def test_valid_arguments():
    value = {"path": "x", "count": 2, "mode": "a", "tags": ["t"], "ratio": 1, "flag": True}
    assert validate_arguments(SCHEMA, value) == []


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ({}, "arguments.path is required"),
        ({"path": 1}, "arguments.path must be string"),
        ({"path": "x", "count": 0}, "at least 1"),
        ({"path": "x", "count": 9}, "at most 5"),
        ({"path": "x", "count": True}, "arguments.count must be integer"),
        ({"path": "x", "count": 1.5}, "arguments.count must be integer"),
        ({"path": "x", "mode": "z"}, "must be one of"),
        ({"path": "x", "tags": ["a", 2]}, "arguments.tags[1] must be string"),
        ({"path": "x", "extra": 1}, "arguments.extra is not allowed"),
        ({"path": "x", "flag": "yes"}, "arguments.flag must be boolean"),
        ("text", "arguments must be object"),
    ],
)
def test_invalid_arguments(value, fragment):
    errors = validate_arguments(SCHEMA, value)
    assert any(fragment in error for error in errors), errors


def test_union_types_and_unknown_types():
    assert validate_arguments({"type": ["string", "null"]}, None) == []
    assert validate_arguments({"type": ["string", "null"]}, 3) != []
    assert validate_arguments({"type": "mystery"}, 3) == [
        "arguments schema is invalid: 'mystery' is not valid under any of the given schemas"
    ]
    assert validate_arguments({"type": "str"}, 3) != []


def test_string_patterns_and_lengths():
    schema = {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "pattern": "^[a-z]+$"},
            "code": {"type": "string", "minLength": 2, "maxLength": 4},
        },
        "additionalProperties": False,
    }
    assert validate_arguments(schema, {"slug": "abc", "code": "xy"}) == []
    expected = "arguments.slug must match pattern '^[a-z]+$'"
    assert validate_arguments(schema, {"slug": "ABC"}) == [expected]
    assert validate_arguments(schema, {"code": "x"}) == [
        "arguments.code must be at least 2 characters"
    ]
    assert validate_arguments(schema, {"code": "xyzabc"}) == [
        "arguments.code must be at most 4 characters"
    ]


def test_array_constraints():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
            "ids": {"type": "array", "uniqueItems": True},
        },
    }
    assert validate_arguments(schema, {"tags": ["a"], "ids": [1, 2]}) == []
    assert validate_arguments(schema, {"tags": []}) == ["arguments.tags must have at least 1 items"]
    assert validate_arguments(schema, {"tags": ["a", "b", "c", "d"]}) == [
        "arguments.tags must have at most 3 items"
    ]
    assert validate_arguments(schema, {"ids": [1, 1]}) == [
        "arguments.ids must contain unique items"
    ]


def test_object_property_counts_and_additional_schemas():
    schema = {
        "type": "object",
        "minProperties": 1,
        "maxProperties": 2,
        "properties": {"name": {"type": "string"}},
        "additionalProperties": {"type": "integer"},
    }
    assert validate_arguments(schema, {"name": "x", "rank": 3}) == []
    assert validate_arguments(schema, {}) == ["arguments must have at least 1 properties"]
    assert validate_arguments(schema, {"name": "x", "rank": 3, "extra": 4}) == [
        "arguments must have at most 2 properties"
    ]
    assert validate_arguments(schema, {"name": "x", "rank": "high"}) == [
        "arguments.rank must be integer"
    ]


def test_numeric_exclusives_and_multiple_of():
    schema = {
        "type": "object",
        "properties": {
            "lat": {"type": "number", "exclusiveMinimum": -90, "exclusiveMaximum": 90},
            "step": {"type": "integer", "multipleOf": 5},
        },
    }
    assert validate_arguments(schema, {"lat": 0, "step": 10}) == []
    assert validate_arguments(schema, {"lat": -90}) == ["arguments.lat must be greater than -90"]
    assert validate_arguments(schema, {"lat": 90}) == ["arguments.lat must be less than 90"]
    assert validate_arguments(schema, {"step": 7}) == ["arguments.step must be a multiple of 5"]


def test_nested_schemas_and_internal_refs():
    schema = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "object",
                "required": ["field"],
                "properties": {"field": {"$ref": "#/$defs/fieldName"}},
            },
            "values": {"type": "array", "items": {"$ref": "#/$defs/fieldName"}},
        },
        "$defs": {"fieldName": {"type": "string", "enum": ["alpha", "beta"]}},
    }
    assert validate_arguments(schema, {"filter": {"field": "alpha"}, "values": ["beta"]}) == []
    assert validate_arguments(schema, {"filter": {}}) == ["arguments.filter.field is required"]
    assert validate_arguments(schema, {"filter": {"field": "gamma"}}) == [
        "arguments.filter.field must be one of ['alpha', 'beta']"
    ]


def test_unsupported_and_malformed_schemas_are_rejected():
    assert validate_arguments({"type": "integer", "exclusiveMinimum": True}, 3) != []
    assert validate_arguments({"type": "array", "items": [{"type": "string"}]}, ["x"]) != []
    assert validate_arguments({"type": "object", "required": "path"}, {}) != []
    assert validate_arguments({"type": "string", "pattern": "("}, "x") != []
    assert validate_arguments(["not", "a", "schema"], "x") == ["arguments schema must be an object"]
    assert validate_arguments({}, {"anything": 1}) == []


def test_unresolvable_external_refs_fail_closed():
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "https://untrusted.example/schema.json"}},
    }
    errors = validate_arguments(schema, {"a": 1})
    assert errors
    assert errors[0].startswith("arguments schema could not be evaluated:")
