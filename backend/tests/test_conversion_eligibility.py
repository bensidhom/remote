import hashlib
import unittest
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

from app.services.conversion.eligibility import (
    BID01Candidate,
    BID01Policy,
    BID01PolicyApplicationError,
    EligibilityProfile,
    EligibilityReason,
    InternalRuleStatus,
    ReviewAction,
    apply_bid01_policy,
    bid01_candidates_from_groups,
    evaluate_bridge_eligibility,
    evaluate_many_bridge_groups,
    preview_bid01_policy,
)
from app.services.conversion.nbi_parser import (
    LegacyBridgeKey,
    LegacyBridgeRecords,
    RecordType,
    SourceRecord,
)


def record(
    *,
    structure="000123",
    state="34",
    agency="NJDOT",
    record_type=RecordType.OVER,
    item_42a="1",
    item_112="Y",
    line=1,
    order=1,
):
    raw_item_008 = structure.ljust(15)
    raw = f"{agency}|{state}|{raw_item_008}|{record_type.value}|{line}".encode()
    fields = {
        "STATE_CODE_001": state,
        "STRUCTURE_NUMBER_008": structure,
        "RECORD_TYPE_005A": record_type.value,
        "SERVICE_ON_042A": item_42a,
        "BRIDGE_LEN_IND_112": item_112,
    }
    digest = hashlib.sha256(raw).hexdigest()
    return SourceRecord(
        source_order=order,
        line_number=line,
        byte_offset=(line - 1) * 446,
        raw_sha256=digest,
        source_record_id=digest,
        record_type=record_type,
        bridge_key=LegacyBridgeKey(agency, state, raw_item_008),
        raw_item_001=f"{state}2",
        raw_item_008=raw_item_008,
        fields=MappingProxyType(fields),
        conformant=True,
    )


def group(over=None, unders=(), duplicate_overs=()):
    records = tuple(
        item
        for item in ((over,) if over is not None else ())
        + tuple(duplicate_overs)
        + tuple(unders)
    )
    if not records:
        raise ValueError("test group needs a record")
    key = records[0].bridge_key
    type_twos = [item for item in unders if item.record_type is RecordType.UNDER]
    type_as = [item for item in unders if item.record_type is RecordType.UNDER_ALTERNATE]
    primary = type_twos[0] if type_twos else type_as[0] if type_as else None
    return LegacyBridgeRecords(
        key=key,
        over=over,
        duplicate_overs=tuple(duplicate_overs),
        unders=tuple(unders),
        primary_under=primary,
        secondary_unders=tuple(item for item in unders if item is not primary),
    )


class SubmissionEligibilityMatrixTests(unittest.TestCase):
    def test_complete_item42a_matrix(self):
        for value in [str(number) for number in range(10)]:
            with self.subTest(item_42a=value):
                result = evaluate_bridge_eligibility(
                    group(record(item_42a=value, item_112="Y")),
                    profile=EligibilityProfile.SUBMISSION_TRANSITION_REVIEW,
                )
                decision = result.decisions[0]
                expected = value in {"1", "4", "5", "6", "7", "8"}
                self.assertEqual(decision.eligible, expected)
                self.assertEqual(decision.retained_by_profile, expected)
                self.assertEqual(decision.normalized_item_42a, value)
                if expected:
                    self.assertIn(
                        EligibilityReason.ELIGIBLE_HIGHWAY_NBIS,
                        decision.reason_codes,
                    )
                else:
                    self.assertIn(
                        EligibilityReason.SERVICE_ON_EXCLUDED,
                        decision.reason_codes,
                    )

    def test_item112_matrix_and_normalization(self):
        cases = [
            ("Y", True, EligibilityReason.ELIGIBLE_HIGHWAY_NBIS),
            (" y ", True, EligibilityReason.ELIGIBLE_HIGHWAY_NBIS),
            ("N", False, EligibilityReason.NOT_NBIS_LENGTH),
            (" n ", False, EligibilityReason.NOT_NBIS_LENGTH),
            ("U", False, EligibilityReason.INVALID_NBIS_LENGTH),
            ("", False, EligibilityReason.MISSING_REQUIRED_SOURCE),
            (None, False, EligibilityReason.MISSING_REQUIRED_SOURCE),
        ]
        for raw, expected, reason in cases:
            with self.subTest(item_112=raw):
                decision = evaluate_bridge_eligibility(
                    group(record(item_112=raw)),
                    profile=EligibilityProfile.SUBMISSION_TRANSITION_REVIEW,
                ).decisions[0]
                self.assertEqual(decision.eligible, expected)
                self.assertIn(reason, decision.reason_codes)

    def test_missing_and_invalid_values_have_specific_review_actions(self):
        missing = evaluate_bridge_eligibility(
            group(record(item_42a=None, item_112=None)),
            profile=EligibilityProfile.SUBMISSION_TRANSITION_REVIEW,
        ).decisions[0]
        self.assertFalse(missing.eligible)
        self.assertEqual(missing.normalized_item_42a, None)
        self.assertEqual(missing.normalized_item_112, None)
        self.assertIn(ReviewAction.SUPPLY_ITEM_42A, missing.review_actions)
        self.assertIn(ReviewAction.SUPPLY_ITEM_112, missing.review_actions)

        invalid = evaluate_bridge_eligibility(
            group(record(item_42a="X", item_112="?")),
            profile=EligibilityProfile.SUBMISSION_TRANSITION_REVIEW,
        ).decisions[0]
        self.assertIn(EligibilityReason.INVALID_SERVICE_ON, invalid.reason_codes)
        self.assertIn(EligibilityReason.INVALID_NBIS_LENGTH, invalid.reason_codes)
        self.assertIn(ReviewAction.CORRECT_ITEM_42A, invalid.review_actions)
        self.assertIn(ReviewAction.CORRECT_ITEM_112, invalid.review_actions)

    def test_exact_raw_source_references_and_internal_status(self):
        source = record(item_42a=" 1 ", item_112=" y ")
        decision = evaluate_bridge_eligibility(
            group(source),
            profile=EligibilityProfile.SUBMISSION_TRANSITION_REVIEW,
        ).decisions[0]
        self.assertEqual(
            decision.rule_status, InternalRuleStatus.INTERNAL_UNVERIFIED
        )
        self.assertEqual(decision.normalized_item_42a, "1")
        self.assertEqual(decision.normalized_item_112, "Y")
        references = {reference.field_name: reference for reference in decision.source_references}
        self.assertEqual(references["SERVICE_ON_042A"].exact_source_value, " 1 ")
        self.assertEqual(references["BRIDGE_LEN_IND_112"].exact_source_value, " y ")
        self.assertEqual(references["SERVICE_ON_042A"].source_record_id, decision.source_record_id)
        self.assertEqual(references["SERVICE_ON_042A"].raw_sha256, source.raw_sha256)
        with self.assertRaises(FrozenInstanceError):
            decision.eligible = False

    def test_under_requires_eligible_over(self):
        eligible_over = record(item_42a="4", item_112="Y")
        under = record(
            record_type=RecordType.UNDER,
            item_42a=None,
            item_112=None,
            line=2,
            order=2,
        )
        decisions = evaluate_bridge_eligibility(
            group(eligible_over, unders=(under,)),
            profile=EligibilityProfile.SUBMISSION_TRANSITION_REVIEW,
        ).decisions
        self.assertTrue(decisions[1].eligible)
        self.assertIn(EligibilityReason.ELIGIBLE_UNDER_VIA_OVER, decisions[1].reason_codes)
        self.assertEqual(decisions[1].source_references[0].line_number, 1)

        excluded_over = replace(
            eligible_over,
            fields=MappingProxyType(
                {**eligible_over.fields, "BRIDGE_LEN_IND_112": "N"}
            ),
        )
        excluded = evaluate_bridge_eligibility(
            group(excluded_over, unders=(under,)),
            profile=EligibilityProfile.SUBMISSION_TRANSITION_REVIEW,
        ).decisions[1]
        self.assertFalse(excluded.eligible)
        self.assertIn(
            EligibilityReason.UNDER_WITHOUT_ELIGIBLE_OVER,
            excluded.reason_codes,
        )

    def test_under_only_is_retained_only_for_research(self):
        under = record(record_type=RecordType.UNDER, item_42a=None, item_112=None)
        submission = evaluate_bridge_eligibility(
            group(unders=(under,)),
            profile=EligibilityProfile.SUBMISSION_TRANSITION_REVIEW,
        ).decisions[0]
        self.assertFalse(submission.eligible)
        self.assertFalse(submission.retained_by_profile)
        self.assertIn(ReviewAction.PROVIDE_ELIGIBLE_OVER, submission.review_actions)

        research = evaluate_bridge_eligibility(
            group(unders=(under,)),
            profile=EligibilityProfile.RESEARCH_HISTORICAL_IMPORT,
        ).decisions[0]
        self.assertFalse(research.eligible)
        self.assertTrue(research.retained_by_profile)
        self.assertIn(EligibilityReason.RESEARCH_ONLY, research.reason_codes)

    def test_research_retention_does_not_change_federal_decision(self):
        excluded = group(record(item_42a="2", item_112="N"))
        decision = evaluate_bridge_eligibility(
            excluded,
            profile=EligibilityProfile.RESEARCH_HISTORICAL_IMPORT,
        ).decisions[0]
        self.assertFalse(decision.eligible)
        self.assertTrue(decision.retained_by_profile)
        self.assertIn(EligibilityReason.SERVICE_ON_EXCLUDED, decision.reason_codes)
        self.assertIn(EligibilityReason.NOT_NBIS_LENGTH, decision.reason_codes)
        self.assertIn(EligibilityReason.RESEARCH_ONLY, decision.reason_codes)
        self.assertFalse(hasattr(decision, "fhwa_omit"))

    def test_duplicate_over_is_never_independently_eligible(self):
        selected = record(line=1, order=1)
        duplicate = record(line=2, order=2)
        result = evaluate_bridge_eligibility(
            group(selected, duplicate_overs=(duplicate,)),
            profile=EligibilityProfile.SUBMISSION_TRANSITION_REVIEW,
        )
        self.assertTrue(result.decisions[0].eligible)
        self.assertFalse(result.decisions[1].eligible)
        self.assertIn(
            EligibilityReason.DUPLICATE_OVER_NOT_SELECTED,
            result.decisions[1].reason_codes,
        )
        self.assertIn(
            ReviewAction.RESOLVE_DUPLICATE_OVER,
            result.decisions[1].review_actions,
        )

    def test_group_evaluation_is_order_independent(self):
        first = group(record(structure="B", state="34"))
        second = group(record(structure="A", state="24", agency="MDDOT"))
        forward = evaluate_many_bridge_groups(
            (first, second),
            profile=EligibilityProfile.SUBMISSION_TRANSITION_REVIEW,
        )
        reverse = evaluate_many_bridge_groups(
            (second, first),
            profile=EligibilityProfile.SUBMISSION_TRANSITION_REVIEW,
        )
        self.assertEqual(tuple(forward), tuple(reverse))
        self.assertEqual(forward, reverse)


def candidate(identifier, *, state="34", agency="NJDOT", source="source"):
    raw = identifier.ljust(15)
    return BID01Candidate(
        bridge_key=LegacyBridgeKey(agency, state, raw),
        source_record_id=f"{source}:{agency}:{state}:{identifier}",
        raw_identifier=raw,
    )


class BID01PolicyTests(unittest.TestCase):
    def test_policy_is_mandatory_and_has_no_default(self):
        with self.assertRaises(TypeError):
            preview_bid01_policy([candidate("000123")])
        with self.assertRaises(ValueError):
            preview_bid01_policy([candidate("000123")], policy=None)

    def test_all_policies_preview_every_changed_id(self):
        candidates = [candidate("000123"), candidate("0"), candidate("ABC")]

        preserve = preview_bid01_policy(candidates, policy=BID01Policy.PRESERVE)
        self.assertEqual([entry.target_identifier for entry in preserve.entries], ["0", "000123", "ABC"])
        self.assertFalse(preserve.changed_entries)

        trim_all = preview_bid01_policy(
            candidates, policy=BID01Policy.TRIM_ALL_LEADING_ZEROS
        )
        targets = {entry.source_identifier: entry.target_identifier for entry in trim_all.entries}
        self.assertEqual(targets, {"000123": "123", "0": "", "ABC": "ABC"})
        self.assertEqual(
            {entry.source_identifier for entry in trim_all.changed_entries},
            {"000123", "0"},
        )
        self.assertEqual(trim_all.invalid_entries[0].source_identifier, "0")

        keep_one = preview_bid01_policy(
            candidates, policy=BID01Policy.TRIM_ALL_BUT_ONE
        )
        targets = {entry.source_identifier: entry.target_identifier for entry in keep_one.entries}
        self.assertEqual(targets, {"000123": "0123", "0": "0", "ABC": "ABC"})

    def test_target_longer_than_snbi_bid01_limit_is_invalid(self):
        preview = preview_bid01_policy(
            [candidate("1234567890123456")], policy=BID01Policy.PRESERVE
        )
        self.assertFalse(preview.can_apply)
        self.assertEqual(preview.invalid_entries[0].target_identifier, "1234567890123456")

    def test_raw_identifier_is_immutable_and_application_is_separate(self):
        item = candidate("000123")
        preview = preview_bid01_policy(
            [item], policy=BID01Policy.TRIM_ALL_LEADING_ZEROS
        )
        applied = apply_bid01_policy(
            [item], policy=BID01Policy.TRIM_ALL_LEADING_ZEROS
        )
        self.assertEqual(item.raw_identifier, "000123         ")
        self.assertEqual(preview.entries[0].raw_identifier, item.raw_identifier)
        self.assertEqual(applied[item.source_record_id], "123")
        with self.assertRaises(TypeError):
            applied["new"] = "value"

    def test_collision_is_scoped_to_agency_and_state_and_rejected(self):
        colliding = [candidate("0001", source="a"), candidate("001", source="b")]
        preview = preview_bid01_policy(
            colliding, policy=BID01Policy.TRIM_ALL_LEADING_ZEROS
        )
        self.assertFalse(preview.can_apply)
        self.assertEqual(len(preview.collisions), 1)
        self.assertEqual(preview.collisions[0].target_identifier, "1")
        with self.assertRaises(BID01PolicyApplicationError) as rejected:
            apply_bid01_policy(
                colliding, policy=BID01Policy.TRIM_ALL_LEADING_ZEROS
            )
        self.assertEqual(rejected.exception.preview, preview)

        separate_scopes = [
            candidate("0001", state="34", source="a"),
            candidate("001", state="24", source="b"),
            candidate("0001", state="34", agency="OTHER", source="c"),
        ]
        allowed = preview_bid01_policy(
            separate_scopes, policy=BID01Policy.TRIM_ALL_LEADING_ZEROS
        )
        self.assertTrue(allowed.can_apply)
        self.assertFalse(allowed.collisions)

        same_raw_duplicate = [
            candidate("0001", source="first"),
            candidate("0001", source="second"),
        ]
        duplicate_preview = preview_bid01_policy(
            same_raw_duplicate, policy=BID01Policy.PRESERVE
        )
        self.assertEqual(len(duplicate_preview.collisions), 1)

    def test_preview_is_order_independent(self):
        values = [
            candidate("0002", source="c"),
            candidate("0001", source="a"),
            candidate("001", source="b"),
        ]
        forward = preview_bid01_policy(
            values, policy=BID01Policy.TRIM_ALL_LEADING_ZEROS
        )
        reverse = preview_bid01_policy(
            reversed(values), policy=BID01Policy.TRIM_ALL_LEADING_ZEROS
        )
        self.assertEqual(forward, reverse)

    def test_candidates_derive_from_group_without_state_policy(self):
        nj = group(record(structure="000123", state="34", agency="NJDOT"))
        md = group(record(structure="000123", state="24", agency="MDDOT"))
        candidates = bid01_candidates_from_groups((nj, md))
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            {item.bridge_key.normalized_state for item in candidates}, {"24", "34"}
        )


if __name__ == "__main__":
    unittest.main()
