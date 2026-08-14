#!/usr/bin/env python3
"""Prepare scoped compatibility inputs for openapi-generator 7.10."""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any


def normalize_nullable_unions(value: Any) -> tuple[Any, int]:
    """Return a copy with simple ``T | null`` unions represented as nullable T."""
    if isinstance(value, list):
        normalized_items: list[Any] = []
        count = 0
        for item in value:
            normalized, item_count = normalize_nullable_unions(item)
            normalized_items.append(normalized)
            count += item_count
        return normalized_items, count

    if not isinstance(value, dict):
        return value, 0

    normalized: dict[str, Any] = {}
    count = 0
    for key, item in value.items():
        normalized_item, item_count = normalize_nullable_unions(item)
        normalized[key] = normalized_item
        count += item_count

    alternatives = normalized.get("anyOf")
    if not isinstance(alternatives, list) or len(alternatives) != 2:
        return normalized, count

    null_alternatives = [item for item in alternatives if isinstance(item, dict) and item == {"type": "null"}]
    non_null_alternatives = [item for item in alternatives if item not in null_alternatives]
    if len(null_alternatives) != 1 or len(non_null_alternatives) != 1:
        return normalized, count

    non_null = non_null_alternatives[0]
    if not isinstance(non_null, dict):
        return normalized, count

    outer_fields = {key: item for key, item in normalized.items() if key != "anyOf"}
    compatible = dict(non_null)
    compatible.update(outer_fields)
    compatible["nullable"] = True
    return compatible, count + 1


def selected_schema_names(
    schemas: dict[str, Any], *, schema_names: set[str], schema_prefixes: tuple[str, ...]
) -> set[str]:
    selected = {
        name for name in schemas if name in schema_names or any(name.startswith(prefix) for prefix in schema_prefixes)
    }
    if not selected:
        raise ValueError("schema selectors matched no component schemas")
    return selected


def normalize_component_schemas(
    document: dict[str, Any], *, schema_names: set[str], schema_prefixes: tuple[str, ...]
) -> tuple[dict[str, Any], int, set[str]]:
    """Normalize nullable unions only in explicitly selected component schemas."""
    normalized_document = copy.deepcopy(document)
    schemas = normalized_document.get("components", {}).get("schemas", {})
    if not isinstance(schemas, dict):
        raise ValueError("OpenAPI document has no component schemas")

    selected = selected_schema_names(schemas, schema_names=schema_names, schema_prefixes=schema_prefixes)
    count = 0
    for name in selected:
        schemas[name], schema_count = normalize_nullable_unions(schemas[name])
        count += schema_count
    return normalized_document, count, selected


def _pascal_case(value: str) -> str:
    return "".join(part[:1].upper() + part[1:].lower() for part in re.split(r"[^A-Za-z0-9]+", value) if part)


def prefix_enum_varnames(
    document: dict[str, Any], *, schema_names: set[str], schema_prefixes: tuple[str, ...]
) -> tuple[dict[str, Any], int, set[str]]:
    """Add unique Go-safe names to string enums in selected component schemas."""
    prepared_document = copy.deepcopy(document)
    schemas = prepared_document.get("components", {}).get("schemas", {})
    if not isinstance(schemas, dict):
        raise ValueError("OpenAPI document has no component schemas")

    selected = selected_schema_names(schemas, schema_names=schema_names, schema_prefixes=schema_prefixes)
    annotated: set[str] = set()
    for name in selected:
        schema = schemas[name]
        if not isinstance(schema, dict):
            continue
        values = schema.get("enum")
        if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
            continue
        varnames = [f"{name}{_pascal_case(value)}" for value in values]
        if len(set(varnames)) != len(varnames):
            raise ValueError(f"generated enum names collide in schema {name}")
        schema["x-enum-varnames"] = varnames
        annotated.add(name)
    if not annotated:
        raise ValueError("selected schemas contain no string enums")
    return prepared_document, len(annotated), annotated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--schema", action="append", default=[], dest="schema_names")
    parser.add_argument("--schema-prefix", action="append", default=[], dest="schema_prefixes")
    parser.add_argument("--normalize-nullable", action="store_true")
    parser.add_argument("--prefix-enum-varnames", action="store_true")
    args = parser.parse_args()

    if args.input.resolve() == args.output.resolve():
        parser.error("input and output paths must differ")
    if not args.normalize_nullable and not args.prefix_enum_varnames:
        parser.error("select at least one compatibility transform")
    if not args.schema_names and not args.schema_prefixes:
        parser.error("at least one schema selector is required")

    document = json.loads(args.input.read_text())
    if not isinstance(document, dict) or "openapi" not in document:
        parser.error("input must be an OpenAPI JSON object")

    messages: list[str] = []
    try:
        if args.normalize_nullable:
            document, count, selected = normalize_component_schemas(
                document,
                schema_names=set(args.schema_names),
                schema_prefixes=tuple(args.schema_prefixes),
            )
            messages.append(f"normalized {count} nullable unions in {len(selected)} schemas")
        if args.prefix_enum_varnames:
            document, count, _ = prefix_enum_varnames(
                document,
                schema_names=set(args.schema_names),
                schema_prefixes=tuple(args.schema_prefixes),
            )
            messages.append(f"annotated {count} string enums")
    except ValueError as error:
        parser.error(str(error))

    args.output.write_text(json.dumps(document, separators=(",", ":")))
    print("Prepared openapi-generator input: " + "; ".join(messages))


if __name__ == "__main__":
    main()
