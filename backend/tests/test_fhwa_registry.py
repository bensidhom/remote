import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from app.standards import fhwa_registry
from app.standards import (
    ArtifactLegalStatus,
    ArtifactState,
    DisabledRuleBundleError,
    RegistryError,
    RuleBundleNotReadyError,
    load_rule_bundle_registry,
)


EXPECTED_BUNDLES = {
    "regulatory_ibr_2022",
    "submission_2026_v04_v1_1",
    "submission_2027_v04_v1_1",
    "errata2_2026_preview",
    "research_historical_import",
    "native_snbi",
    "submission_2028",
}

VERIFIED_ARTIFACTS = {
    "snbi_march_2022": (
        "artifacts/SNBI.March.2022.FHWA-HIF-22-017.pdf",
        "2b55c855c0ed306dfd02b06a0533d48103c3aee7e1685aee5c5e35a671b96142",
    ),
    "nbi_snbi_crosswalk_dec_2023": (
        "artifacts/NBI-SNBI.Data.Crosswalk.Dec.2023.xlsx",
        "9b81e0b7c9d808cbee65e8a5436f759e5d68b56665fb416ec51e69c40717d4fc",
    ),
    "nbi_snbi_code_mapping": (
        "artifacts/NBI-SNBI.Detailed.Code.Mapping.xlsx",
        "63f120e71363138a33d8cf1344199d00e363633c22ccc959d9ab49f27b76a475",
    ),
    "snbi_schema_v04_july_2024": (
        "artifacts/SNBI.Schema.v04.json",
        "2555813afaab4701c11e3d41b49cb1f894dfcae772afbd1638d9d25348e383a0",
    ),
    "validation_logic_v1_1_dec_2025": (
        "artifacts/SNBI.Data.Validation.Rules.v1.1.xlsx",
        "765776e38facb1e42a528230342317b261ba47e64676ecde418f57ee7d0e2232",
    ),
    "validation_logic_v1_2_sep_2026": (
        "artifacts/SNBI.Data.Validation.Rules.v1.2.xlsx",
        "765776e38facb1e42a528230342317b261ba47e64676ecde418f57ee7d0e2232",
    ),
    "legacy_element_xsd_v1_2013": (
        "artifacts/FHWAElement.Schema.v1.0.2013-10-03.xsd",
        "193cda71b64933e2d5316e7d193d6f780c9479a875ea3ebcb21dcaee36f0a95f",
    ),
}


def _minimal_registry(artifact):
    return {
        "format_version": 1,
        "registry_id": "test_registry",
        "observed_on": "2026-08-13",
        "artifacts": [artifact],
        "bundles": [
            {
                "id": "test_bundle",
                "title": "Test bundle",
                "profile_kind": "regulatory_baseline",
                "legal_status": "regulatory_ibr",
                "compliance_use": "regulatory_baseline_comparison",
                "implementation_status": "registry_only",
                "enabled": True,
                "effective_submission_years": [],
                "artifact_ids": [artifact["id"]],
                "inherits": [],
                "disabled_reason": None,
                "notes": [],
            }
        ],
    }


def _artifact(**overrides):
    value = {
        "id": "test_artifact",
        "title": "Test artifact",
        "role": "test_reference",
        "authority": "FHWA",
        "legal_status": "regulatory_ibr",
        "state": "remote_unverified",
        "source_url": "https://www.fhwa.dot.gov/example",
        "published_on": None,
        "published_label": None,
        "updated_on": None,
        "media_type": "application/octet-stream",
        "local_path": None,
        "sha256": None,
        "notes": [],
    }
    value.update(overrides)
    return value


class FhwaRegistryTests(unittest.TestCase):
    def test_seeded_profiles_are_complete_and_legally_distinct(self):
        registry = load_rule_bundle_registry()

        self.assertEqual(set(registry.bundles), EXPECTED_BUNDLES)
        self.assertEqual(
            registry.bundle("regulatory_ibr_2022").legal_status.value,
            "regulatory_ibr",
        )
        self.assertEqual(
            registry.bundle("submission_2026_v04_v1_1").legal_status.value,
            "operational_submission",
        )
        self.assertEqual(
            registry.bundle("errata2_2026_preview").legal_status.value,
            "informational_pending_rulemaking",
        )
        errata2 = registry.artifact("snbi_errata2_june_2026")
        self.assertIs(
            errata2.legal_status,
            ArtifactLegalStatus.INFORMATIONAL_PENDING_RULEMAKING,
        )

    def test_registry_claims_verified_hashes_only_for_exact_present_artifacts(self):
        registry = load_rule_bundle_registry()

        for artifact in registry.artifacts.values():
            if artifact.id in VERIFIED_ARTIFACTS:
                expected_path, expected_sha256 = VERIFIED_ARTIFACTS[artifact.id]
                self.assertIs(artifact.state, ArtifactState.VENDORED_VERIFIED)
                self.assertEqual(artifact.local_path, expected_path)
                self.assertEqual(artifact.sha256, expected_sha256)
                expected_date = (
                    "2026-09-20"
                    if artifact.id == "validation_logic_v1_2_sep_2026"
                    else "2026-08-13"
                )
                self.assertEqual(str(artifact.retrieved_on), expected_date)
                self.assertEqual(str(artifact.reviewed_on), expected_date)
                self.assertEqual(artifact.reviewed_by, "BridgeGuard maintainers")
                self.assertTrue(artifact.parser_version)
                self.assertIsNone(artifact.compiler_version)
                expected_supersedes = (
                    ("validation_logic_v1_1_dec_2025",)
                    if artifact.id == "validation_logic_v1_2_sep_2026"
                    else ()
                )
                expected_superseded_by = (
                    ("validation_logic_v1_2_sep_2026",)
                    if artifact.id == "validation_logic_v1_1_dec_2025"
                    else ()
                )
                self.assertEqual(artifact.supersedes, expected_supersedes)
                self.assertEqual(artifact.superseded_by, expected_superseded_by)
            else:
                self.assertIn(
                    artifact.state,
                    {ArtifactState.REMOTE_UNVERIFIED, ArtifactState.UNAVAILABLE},
                )
                self.assertIsNone(artifact.local_path)
                self.assertIsNone(artifact.sha256)

    def test_promoted_official_artifact_bytes_media_and_sizes_are_exact(self):
        registry = load_rule_bundle_registry()
        expected = {
            "snbi_march_2022": (6728697, "application/pdf", b"%PDF-"),
            "nbi_snbi_crosswalk_dec_2023": (
                53093,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                b"PK",
            ),
            "nbi_snbi_code_mapping": (
                163207,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                b"PK",
            ),
            "validation_logic_v1_1_dec_2025": (
                91515,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                b"PK",
            ),
            "validation_logic_v1_2_sep_2026": (
                91515,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                b"PK",
            ),
            "legacy_element_xsd_v1_2013": (
                5436,
                "application/xml",
                b"\xef\xbb\xbf<xs:schema",
            ),
        }
        root = fhwa_registry.DEFAULT_REGISTRY_PATH.parent

        for artifact_id, (size, media_type, signature) in expected.items():
            artifact = registry.artifact(artifact_id)
            path = root / (artifact.local_path or "")
            payload = path.read_bytes()
            self.assertEqual(len(payload), size)
            self.assertEqual(artifact.media_type, media_type)
            self.assertTrue(payload.startswith(signature))
            self.assertEqual(hashlib.sha256(payload).hexdigest(), artifact.sha256)

    def test_registry_schema_declares_review_and_supersession_metadata(self):
        schema_path = fhwa_registry.DEFAULT_REGISTRY_PATH.with_name(
            "registry.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        artifact_properties = schema["$defs"]["artifact"]["properties"]
        self.assertTrue(
            {
                "retrieved_on",
                "reviewed_on",
                "reviewed_by",
                "parser_version",
                "compiler_version",
                "supersedes",
                "superseded_by",
            }
            <= set(artifact_properties)
        )

    def test_selection_fails_closed_but_metadata_remains_available(self):
        registry = load_rule_bundle_registry()

        metadata = registry.select_submission_year(
            2026, require_verified_artifacts=False
        )
        self.assertEqual(metadata.id, "submission_2026_v04_v1_1")
        with self.assertRaises(RuleBundleNotReadyError):
            registry.select_submission_year(2026)
        with self.assertRaisesRegex(DisabledRuleBundleError, "not been locally pinned"):
            registry.select_submission_year(2028)

        disabled_metadata = registry.bundle("submission_2028")
        self.assertFalse(disabled_metadata.enabled)

    def test_registry_and_models_are_immutable(self):
        registry = load_rule_bundle_registry()

        with self.assertRaises(TypeError):
            registry.bundles["new_bundle"] = registry.bundle(
                "regulatory_ibr_2022"
            )
        with self.assertRaises(FrozenInstanceError):
            registry.bundle("native_snbi").enabled = False

    def test_local_verified_artifact_requires_matching_bytes(self):
        artifact_bytes = b"locally pinned FHWA fixture"
        digest = hashlib.sha256(artifact_bytes).hexdigest()
        payload = _minimal_registry(
            _artifact(
                state="vendored_verified",
                local_path="artifact.bin",
                sha256=digest,
            )
        )
        encoded = json.dumps(payload).encode("utf-8")
        fake_artifact_path = Path("artifact.bin")

        with patch.object(Path, "read_bytes", return_value=encoded), patch.object(
            fhwa_registry,
            "_safe_local_artifact_path",
            return_value=fake_artifact_path,
        ), patch.object(Path, "is_file", return_value=True), patch.object(
            fhwa_registry, "_sha256_file", return_value=digest
        ):
            registry = load_rule_bundle_registry(Path("registry.json"))
            self.assertEqual(registry.select("test_bundle").id, "test_bundle")

        with patch.object(Path, "read_bytes", return_value=encoded), patch.object(
            fhwa_registry,
            "_safe_local_artifact_path",
            return_value=fake_artifact_path,
        ), patch.object(Path, "is_file", return_value=True), patch.object(
            fhwa_registry, "_sha256_file", return_value="f" * 64
        ):
            with self.assertRaisesRegex(RegistryError, "SHA-256 mismatch"):
                load_rule_bundle_registry(Path("registry.json"))

    def test_unverified_artifact_cannot_claim_a_hash(self):
        payload = _minimal_registry(
            _artifact(sha256="0" * 64, state="remote_unverified")
        )

        with patch.object(
            Path, "read_bytes", return_value=json.dumps(payload).encode("utf-8")
        ):
            with self.assertRaisesRegex(RegistryError, "may not declare"):
                load_rule_bundle_registry(Path("registry.json"))


if __name__ == "__main__":
    unittest.main()
