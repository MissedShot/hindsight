from __future__ import annotations

import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).parents[1] / "prepare_openapi_for_generator.py"


class NormalizeOpenApiNullableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("normalize_openapi_nullable", SCRIPT)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {SCRIPT}")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_normalizes_only_simple_nullable_union(self) -> None:
        source = {
            "properties": {
                "simple": {
                    "anyOf": [{"type": "string", "maxLength": 12}, {"type": "null"}],
                    "title": "Simple",
                },
                "complex": {
                    "anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}],
                },
                "ordinary": {"type": "array", "items": {"type": "string"}},
            }
        }

        result, count = self.module.normalize_nullable_unions(source)

        self.assertEqual(count, 1)
        self.assertEqual(
            result["properties"]["simple"],
            {"type": "string", "maxLength": 12, "title": "Simple", "nullable": True},
        )
        self.assertEqual(result["properties"]["complex"], source["properties"]["complex"])
        self.assertEqual(result["properties"]["ordinary"], source["properties"]["ordinary"])
        self.assertEqual(source["properties"]["simple"]["anyOf"][1], {"type": "null"})

    def test_normalizes_nested_nullable_union(self) -> None:
        source = {"items": [{"schema": {"anyOf": [{"$ref": "#/components/schemas/PeerClaim"}, {"type": "null"}]}}]}

        result, count = self.module.normalize_nullable_unions(source)

        self.assertEqual(count, 1)
        self.assertEqual(
            result,
            {
                "items": [
                    {
                        "schema": {
                            "$ref": "#/components/schemas/PeerClaim",
                            "nullable": True,
                        }
                    }
                ]
            },
        )

    def test_component_selectors_leave_unselected_schemas_unchanged(self) -> None:
        nullable = {"anyOf": [{"type": "string"}, {"type": "null"}]}
        document = {
            "components": {
                "schemas": {
                    "PeerClaim": {"properties": {"value": nullable}},
                    "RetainPeerContext": {"properties": {"value": nullable}},
                    "Unrelated": {"properties": {"value": nullable}},
                }
            }
        }

        result, count, selected = self.module.normalize_component_schemas(
            document,
            schema_names={"RetainPeerContext"},
            schema_prefixes=("Peer",),
        )

        self.assertEqual(count, 2)
        self.assertEqual(selected, {"PeerClaim", "RetainPeerContext"})
        self.assertEqual(
            result["components"]["schemas"]["PeerClaim"]["properties"]["value"],
            {"type": "string", "nullable": True},
        )
        self.assertEqual(
            result["components"]["schemas"]["Unrelated"],
            document["components"]["schemas"]["Unrelated"],
        )

    def test_enum_varnames_are_prefixed_only_for_selected_string_enums(self) -> None:
        document = {
            "components": {
                "schemas": {
                    "PeerClaimOrigin": {"type": "string", "enum": ["derived", "manual"]},
                    "PeerSourceKind": {"type": "string", "enum": ["memory_unit", "manual"]},
                    "PeerObject": {"type": "object"},
                    "Unrelated": {"type": "string", "enum": ["manual"]},
                }
            }
        }

        result, count, annotated = self.module.prefix_enum_varnames(
            document,
            schema_names=set(),
            schema_prefixes=("Peer",),
        )

        self.assertEqual(count, 2)
        self.assertEqual(annotated, {"PeerClaimOrigin", "PeerSourceKind"})
        self.assertEqual(
            result["components"]["schemas"]["PeerClaimOrigin"]["x-enum-varnames"],
            ["PeerClaimOriginDerived", "PeerClaimOriginManual"],
        )
        self.assertEqual(
            result["components"]["schemas"]["PeerSourceKind"]["x-enum-varnames"],
            ["PeerSourceKindMemoryUnit", "PeerSourceKindManual"],
        )
        self.assertNotIn("x-enum-varnames", result["components"]["schemas"]["Unrelated"])
        self.assertNotIn("x-enum-varnames", document["components"]["schemas"]["PeerClaimOrigin"])


if __name__ == "__main__":
    unittest.main()
