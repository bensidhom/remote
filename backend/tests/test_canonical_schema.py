import math
import unittest

from app.services.conversion.canonical_schema import (
    CanonicalResourceLimitError,
    DiagnosticSeverity,
    NormalizationLimits,
    SchemaStatus,
    normalize_canonical_document,
)


PROFILE = "research_historical_import"


def normalize(document, **kwargs):
    return normalize_canonical_document(
        document,
        profile_id=kwargs.pop("profile_id", PROFILE),
        **kwargs,
    )


class CanonicalSchemaTests(unittest.TestCase):
    def test_known_document_is_copied_and_not_mutated(self):
        source = {
            "BL01": 34,
            "BID01": "123",
            "Features": [
                {
                    "BF01": "H01",
                    "BF02": "C",
                    "Routes": [{"BRT01": "H01-R01", "BRT02": "95"}],
                }
            ],
        }
        result = normalize(source)

        self.assertEqual(result.original_document, source)
        self.assertEqual(result.normalized_document, source)
        self.assertIsNot(result.original_document, source)
        self.assertIsNot(result.normalized_document, source)
        self.assertIsNot(
            result.original_document["Features"], result.normalized_document["Features"]
        )
        source["Features"][0]["BF01"] = "CHANGED"
        result.original_document["Features"][0]["BF02"] = "MUTATED-COPY"
        self.assertEqual(result.normalized_document["Features"][0]["BF01"], "H01")
        self.assertEqual(result.normalized_document["Features"][0]["BF02"], "C")
        self.assertFalse(result.has_errors)

    def test_profile_and_schema_status_never_claim_submission_readiness(self):
        result = normalize({"BL01": 34, "BID01": "1"})
        self.assertEqual(result.profile_id, PROFILE)
        self.assertEqual(result.schema_status, SchemaStatus.UNAVAILABLE_UNVERIFIED)
        self.assertEqual(result.schema_status.value, "unavailable_unverified")
        self.assertFalse(result.submission_ready)

    def test_string_coercions_are_explicit_and_source_is_preserved(self):
        source = {"BID01": " 0001 ", "BID02": "   ", "BL01": 34}
        result = normalize(source)

        self.assertEqual(result.original_document["BID01"], " 0001 ")
        self.assertEqual(result.original_document["BID02"], "   ")
        self.assertEqual(result.normalized_document["BID01"], "0001")
        self.assertIsNone(result.normalized_document["BID02"])
        by_path = {}
        for coercion in result.coercions:
            by_path.setdefault(coercion.path, []).append(coercion.kind)
        self.assertEqual(by_path["/BID01"], ["trim_string"])
        self.assertEqual(
            by_path["/BID02"], ["trim_string", "blank_string_to_null"]
        )

    def test_unknown_tags_are_reported_with_rfc6901_paths_and_values(self):
        source = {
            "BID01": "1",
            "BZZ99": {"evidence": 7},
            "vendor/field~name": "kept in diagnostic",
            "Features": [{"BF01": "H01", "BUNKNOWN": "x", "loose/value": 3}],
        }
        result = normalize(source)

        by_tag = {item.tag: item for item in result.unknown_tags}
        self.assertTrue(by_tag["BZZ99"].official_looking)
        self.assertEqual(by_tag["BZZ99"].severity, DiagnosticSeverity.ERROR)
        self.assertEqual(by_tag["BZZ99"].value, {"evidence": 7})
        self.assertEqual(
            by_tag["vendor/field~name"].path, "/vendor~1field~0name"
        )
        self.assertFalse(by_tag["vendor/field~name"].official_looking)
        self.assertEqual(by_tag["BUNKNOWN"].path, "/Features/0/BUNKNOWN")
        self.assertEqual(by_tag["loose/value"].path, "/Features/0/loose~1value")
        self.assertNotIn("BZZ99", result.normalized_document)
        self.assertEqual(result.original_document["BZZ99"], {"evidence": 7})
        self.assertTrue(result.has_errors)

    def test_namespaced_extensions_are_the_only_extension_location(self):
        source = {
            "BID01": "1",
            "extensions": {
                "org.rowan.bridgeguard": {"BZZ99": "application-private", "text": " x "},
                "bridgeguard:inspection": [1, {"raw": True}],
                "not_namespaced": {"rejected": True},
            },
            "Features": [
                {
                    "BF01": "H01",
                    "extensions": {"com.example.vendor": {"score": 0.8}},
                }
            ],
        }
        result = normalize(source)

        extensions = result.normalized_document["extensions"]
        self.assertEqual(
            extensions["org.rowan.bridgeguard"]["BZZ99"], "application-private"
        )
        self.assertEqual(extensions["org.rowan.bridgeguard"]["text"], " x ")
        self.assertIn("bridgeguard:inspection", extensions)
        self.assertNotIn("not_namespaced", extensions)
        self.assertEqual(
            result.normalized_document["Features"][0]["extensions"]
            ["com.example.vendor"]["score"],
            0.8,
        )
        self.assertFalse(any(item.tag == "BZZ99" for item in result.unknown_tags))
        rejected = {item.code: item for item in result.rejected_values}
        self.assertEqual(
            rejected["invalid_extension_namespace"].path,
            "/extensions/not_namespaced",
        )

    def test_extensions_container_must_be_an_object(self):
        result = normalize({"BID01": "1", "extensions": ["not", "an", "object"]})
        self.assertNotIn("extensions", result.normalized_document)
        self.assertEqual(result.wrong_container_types[0].path, "/extensions")
        self.assertEqual(result.wrong_container_types[0].expected, "object")
        self.assertEqual(result.wrong_container_types[0].actual, "array")
        self.assertEqual(
            result.rejected_values[0].code, "extensions_type_mismatch"
        )

    def test_array_containers_are_exact_and_dict_is_never_wrapped(self):
        source = {"BID01": "1", "Features": {"BF01": "H01"}}
        result = normalize(source)
        self.assertEqual(result.original_document["Features"], {"BF01": "H01"})
        self.assertNotIn("Features", result.normalized_document)
        self.assertEqual(result.wrong_container_types[0].path, "/Features")
        self.assertEqual(result.wrong_container_types[0].expected, "array")
        self.assertEqual(result.rejected_values[0].code, "array_type_mismatch")

    def test_routes_are_arrays_only_inside_features(self):
        result = normalize(
            {
                "BID01": "1",
                "Features": [{"BF01": "H01", "Routes": {"BRT01": "R01"}}],
            }
        )
        feature = result.normalized_document["Features"][0]
        self.assertNotIn("Routes", feature)
        self.assertEqual(
            result.wrong_container_types[0].path, "/Features/0/Routes"
        )
        self.assertEqual(result.wrong_container_types[0].actual, "object")

        top_level = normalize({"BID01": "1", "Routes": []})
        self.assertEqual(top_level.unknown_tags[0].path, "/Routes")
        self.assertFalse(top_level.unknown_tags[0].official_looking)

    def test_non_object_array_members_are_rejected_but_not_silently_lost(self):
        source = {"Features": [{"BF01": "H01"}, "bad", 12, None]}
        result = normalize(source)
        self.assertEqual(result.normalized_document["Features"], [{"BF01": "H01"}])
        self.assertEqual(
            [item.path for item in result.wrong_container_types],
            ["/Features/1", "/Features/2", "/Features/3"],
        )
        self.assertEqual(
            [item.value for item in result.rejected_values], ["bad", 12, None]
        )
        self.assertEqual(result.original_document["Features"], source["Features"])

    def test_scalar_tags_reject_containers_and_nonfinite_numbers(self):
        result = normalize(
            {"BID01": {"wrong": 1}, "BL01": [34], "BG01": math.inf, "BG02": math.nan}
        )
        self.assertEqual(result.normalized_document, {})
        self.assertEqual(
            [item.path for item in result.wrong_container_types], ["/BID01", "/BL01"]
        )
        codes = [item.code for item in result.rejected_values]
        self.assertEqual(codes.count("scalar_type_mismatch"), 2)
        self.assertEqual(codes.count("non_finite_number"), 2)

    def test_duplicate_logical_child_keys_are_reported_and_both_rows_retained(self):
        source = {
            "Elements": [
                {"BE01": " 12 ", "BE02": 0, "BE03": 10},
                {"BE01": "12", "BE02": 0, "BE03": 11},
            ],
            "Features": [
                {
                    "BF01": "H01",
                    "Routes": [
                        {"BRT01": "R01", "BRT02": "1"},
                        {"BRT01": "R01", "BRT02": "2"},
                    ],
                },
                {"BF01": "H01", "BF02": "B"},
            ],
        }
        result = normalize(source)

        self.assertEqual(len(result.normalized_document["Elements"]), 2)
        self.assertEqual(len(result.normalized_document["Features"]), 2)
        by_container = {
            item.container_path: item for item in result.duplicate_logical_child_keys
        }
        self.assertEqual(by_container["/Elements"].logical_key_fields, ("BE01", "BE02"))
        self.assertEqual(by_container["/Elements"].logical_key_values, ("12", 0))
        self.assertEqual(by_container["/Elements"].first_path, "/Elements/0")
        self.assertEqual(by_container["/Elements"].duplicate_path, "/Elements/1")
        self.assertEqual(
            by_container["/Features/0/Routes"].duplicate_path,
            "/Features/0/Routes/1",
        )
        self.assertEqual(by_container["/Features"].duplicate_path, "/Features/1")

    def test_missing_logical_key_does_not_create_a_false_duplicate(self):
        result = normalize({"Elements": [{"BE03": 1}, {"BE03": 2}]})
        self.assertFalse(result.duplicate_logical_child_keys)
        self.assertEqual(len(result.normalized_document["Elements"]), 2)

    def test_wrong_root_type_is_diagnostic_and_original_is_preserved(self):
        source = [{"BID01": "1"}]
        result = normalize(source)
        self.assertEqual(result.original_document, source)
        self.assertEqual(result.normalized_document, {})
        self.assertEqual(result.wrong_container_types[0].path, "")
        self.assertEqual(result.rejected_values[0].code, "root_not_object")

    def test_omitted_arrays_are_not_fabricated(self):
        result = normalize({"BID01": "1"})
        self.assertEqual(result.normalized_document, {"BID01": "1"})
        for name in (
            "Elements",
            "Features",
            "Inspections",
            "PostingEvaluations",
            "PostingStatuses",
            "SpanSets",
            "SubstructureSets",
            "Works",
        ):
            self.assertNotIn(name, result.normalized_document)

    def test_non_json_extension_value_is_rejected_at_exact_path(self):
        source = {"extensions": {"org.example.ext": {"bad/value": (1, 2)}}}
        result = normalize(source)
        self.assertEqual(result.normalized_document["extensions"], {})
        self.assertEqual(result.rejected_values[0].code, "non_json_extension_value")
        self.assertEqual(
            result.rejected_values[0].path,
            "/extensions/org.example.ext/bad~1value",
        )
        self.assertEqual(result.rejected_values[0].value, (1, 2))

    def test_profile_id_is_mandatory(self):
        for invalid in (None, "", "   "):
            with self.subTest(profile_id=invalid):
                with self.assertRaises(ValueError):
                    normalize_canonical_document({}, profile_id=invalid)


class CanonicalResourceLimitsTests(unittest.TestCase):
    def test_depth_limit_has_json_pointer(self):
        source = {"extensions": {"org.example.ext": {"a": {"b": {"c": 1}}}}}
        with self.assertRaises(CanonicalResourceLimitError) as rejected:
            normalize(
                source,
                limits=NormalizationLimits(max_depth=3),
            )
        self.assertEqual(rejected.exception.code, "max_depth_exceeded")
        self.assertEqual(
            rejected.exception.path, "/extensions/org.example.ext/a/b"
        )

    def test_node_limit_is_hard(self):
        with self.assertRaises(CanonicalResourceLimitError) as rejected:
            normalize(
                {"BID01": "1", "BL01": 34},
                limits=NormalizationLimits(max_nodes=2),
            )
        self.assertEqual(rejected.exception.code, "max_nodes_exceeded")

    def test_string_array_and_object_limits(self):
        cases = [
            (
                {"BID01": "12345"},
                NormalizationLimits(max_string_length=4),
                "max_string_length_exceeded",
            ),
            (
                {"Features": [{}, {}]},
                NormalizationLimits(max_array_items=1),
                "max_array_items_exceeded",
            ),
            (
                {"BID01": "1", "BL01": 34},
                NormalizationLimits(max_object_members=1),
                "max_object_members_exceeded",
            ),
        ]
        for source, limits, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(CanonicalResourceLimitError) as rejected:
                    normalize(source, limits=limits)
                self.assertEqual(rejected.exception.code, code)

    def test_cycle_is_rejected_before_deep_copy(self):
        source = {}
        source["self"] = source
        with self.assertRaises(CanonicalResourceLimitError) as rejected:
            normalize(source)
        self.assertEqual(rejected.exception.code, "cyclic_document")
        self.assertEqual(rejected.exception.path, "/self")

    def test_limits_must_be_positive_integers(self):
        for invalid in (0, -1, True):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    normalize({}, limits=NormalizationLimits(max_nodes=invalid))


if __name__ == "__main__":
    unittest.main()
