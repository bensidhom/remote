import hashlib
import io
import unittest

from app.services.conversion.nbi_parser import (
    CsvDialect,
    IssuePolicy,
    LegacyAdapterCollisionError,
    NBIFormat,
    NBIParseError,
    ParserLimits,
    RecordType,
    iter_legacy_bridge_records,
    parse_nbi,
    to_legacy_bridge_dict,
)


CSV = CsvDialect(delimiter=",", quotechar='"')
MINIMAL_COLUMNS = (
    "STATE_CODE_001",
    "STRUCTURE_NUMBER_008",
    "RECORD_TYPE_005A",
)


def fixed_record(
    *,
    state="342",
    structure="BRIDGE-001",
    record_type="1",
    year_built="1999",
):
    record = bytearray(b" " * 445)
    state_bytes = state.encode("ascii")
    if len(state_bytes) != 3:
        raise ValueError("test state must occupy exactly three bytes")
    structure_bytes = structure.encode("ascii")
    if len(structure_bytes) > 15:
        raise ValueError("test structure number exceeds Item 8")
    record[0:3] = state_bytes
    record[3:18] = structure_bytes.ljust(15, b" ")
    record[18:19] = record_type.encode("ascii")
    record[156:160] = year_built.encode("ascii")
    return bytes(record)


def parse_fixed(data, **overrides):
    options = {
        "source_agency_context": "NJDOT",
        "encoding": "ascii",
        "input_format": NBIFormat.FHWA_FIXED_445,
    }
    options.update(overrides)
    return parse_nbi(data, **options)


class StrictFixedWidthParserTests(unittest.TestCase):
    def test_composite_adapter_keeps_same_item8_records_separate(self):
        result = parse_fixed(
            fixed_record(state="342", structure="SAME", record_type="1")
            + b"\n"
            + fixed_record(state="242", structure="SAME", record_type="1")
        )
        projected = iter_legacy_bridge_records(result)
        self.assertEqual(len(projected), 2)
        self.assertEqual(
            {bridge["over"]["STATE_CODE_001"] for _key, bridge in projected},
            {"24", "34"},
        )

    def test_golden_fixed_records_preserve_bytes_and_under_order(self):
        over = fixed_record(record_type="1")
        type_a = fixed_record(record_type="A")
        type_two = fixed_record(record_type="2")
        result = parse_fixed(over + b"\r\n" + type_a + b"\n" + type_two)

        self.assertEqual(result.input_format, NBIFormat.FHWA_FIXED_445)
        self.assertEqual(result.bytes_read, 1338)
        self.assertEqual(len(result.bridges), 1)
        key = next(iter(result.bridges))
        self.assertEqual(key.source_agency_context, "NJDOT")
        self.assertEqual(key.normalized_state, "34")
        self.assertEqual(key.raw_structure_number, "BRIDGE-001     ")

        group = result.bridges[key]
        self.assertEqual(group.over.raw_item_001, "342")
        self.assertEqual(group.over.fields["STATE_CODE_001"], "342")
        self.assertEqual(group.over.raw_item_008, "BRIDGE-001     ")
        self.assertEqual(group.over.fields["STRUCTURE_NUMBER_008"], "BRIDGE-001")
        self.assertEqual(group.over.raw_sha256, hashlib.sha256(over).hexdigest())
        self.assertEqual(len(group.over.source_record_id), 64)
        self.assertEqual(
            group.over.source_record_id,
            parse_fixed(over).records[0].source_record_id,
        )
        self.assertEqual(group.over.byte_offset, 0)
        self.assertEqual([record.byte_offset for record in group.unders], [447, 893])
        self.assertEqual(
            [record.record_type for record in group.unders],
            [RecordType.UNDER_ALTERNATE, RecordType.UNDER],
        )
        self.assertEqual(group.primary_under.record_type, RecordType.UNDER)
        self.assertEqual(group.secondary_unders, (group.unders[0],))
        self.assertIn(
            "type2_preferred_over_earlier_type_a",
            {issue.code for issue in result.issues},
        )
        with self.assertRaises(TypeError):
            group.over.fields["YEAR_BUILT_027"] = "2000"

    def test_same_raw_bridge_number_in_different_states_is_not_merged(self):
        data = fixed_record(state="342", structure="SAME", record_type="1")
        data += b"\n" + fixed_record(state="242", structure="SAME", record_type="1")

        result = parse_fixed(data)

        self.assertEqual(len(result.bridges), 2)
        self.assertEqual(
            {key.normalized_state for key in result.bridges}, {"24", "34"}
        )
        with self.assertRaises(LegacyAdapterCollisionError):
            to_legacy_bridge_dict(result)

    def test_item1_uses_state_fips_and_drops_fhwa_region_digit(self):
        nj = parse_fixed(fixed_record(state="342", structure="NJ"))
        alabama = parse_fixed(fixed_record(state="014", structure="AL"))
        self.assertEqual(next(iter(nj.bridges)).normalized_state, "34")
        self.assertEqual(next(iter(alabama.bridges)).normalized_state, "01")

        same_state = parse_fixed(
            fixed_record(state="341", structure="REGION", record_type="1")
            + b"\n"
            + fixed_record(state="342", structure="REGION", record_type="2")
        )
        self.assertEqual(len(same_state.bridges), 1)
        group = next(iter(same_state.bridges.values()))
        self.assertEqual(group.key.normalized_state, "34")
        self.assertEqual(group.over.raw_item_001, "341")
        self.assertEqual(group.primary_under.raw_item_001, "342")

    def test_legacy_adapter_projects_two_digit_state_without_mutating_source(self):
        result = parse_fixed(
            fixed_record(state="342", record_type="1")
            + b"\n"
            + fixed_record(state="342", record_type="2")
        )

        source_group = next(iter(result.bridges.values()))
        legacy = to_legacy_bridge_dict(result)["BRIDGE-001"]
        self.assertEqual(source_group.over.raw_item_001, "342")
        self.assertEqual(source_group.over.fields["STATE_CODE_001"], "342")
        self.assertEqual(source_group.unders[0].fields["STATE_CODE_001"], "342")
        self.assertEqual(legacy["over"]["STATE_CODE_001"], "34")
        self.assertEqual(legacy["unders"][0]["STATE_CODE_001"], "34")

    def test_item1_requires_exactly_three_numeric_digits(self):
        for invalid_state in (b"34 ", b"A42", b"004"):
            record = bytearray(fixed_record())
            record[0:3] = invalid_state
            with self.subTest(state=invalid_state):
                with self.assertRaises(NBIParseError) as rejected:
                    parse_fixed(bytes(record))
                self.assertEqual(rejected.exception.issue.code, "invalid_state_code")

    def test_duplicate_over_never_last_wins(self):
        first = fixed_record(record_type="1", year_built="1980")
        second = fixed_record(record_type="1", year_built="2020")
        result = parse_fixed(
            first + b"\n" + second,
            issue_policy=IssuePolicy.ACCEPT_WITH_ISSUE,
        )

        group = next(iter(result.bridges.values()))
        self.assertEqual(group.over.fields["YEAR_BUILT_027"], "1980")
        self.assertEqual(len(group.duplicate_overs), 1)
        self.assertEqual(group.duplicate_overs[0].fields["YEAR_BUILT_027"], "2020")
        self.assertIn("duplicate_over_record", {issue.code for issue in result.issues})

        with self.assertRaises(NBIParseError) as rejected:
            parse_fixed(first + b"\n" + second)
        self.assertEqual(rejected.exception.issue.code, "duplicate_over_record")

        quarantined = parse_fixed(
            first + b"\n" + second,
            issue_policy=IssuePolicy.QUARANTINE,
        )
        self.assertEqual(len(quarantined.records), 1)
        self.assertEqual(len(quarantined.quarantined_records), 1)

    def test_multiple_type2_records_select_earliest_and_require_review(self):
        first = fixed_record(record_type="2", year_built="1980")
        second = fixed_record(record_type="2", year_built="2020")
        result = parse_fixed(first + b"\n" + second)

        group = next(iter(result.bridges.values()))
        self.assertEqual(group.primary_under.fields["YEAR_BUILT_027"], "1980")
        self.assertEqual(group.secondary_unders[0].fields["YEAR_BUILT_027"], "2020")
        self.assertIn(
            "multiple_type2_under_records", {issue.code for issue in result.issues}
        )

    def test_short_and_long_records_are_rejected_property_style(self):
        valid = fixed_record()
        samples = [valid[:18], valid[:128], valid[:444], valid + b"X", valid + b"X" * 67]
        for sample in samples:
            with self.subTest(record_length=len(sample)):
                with self.assertRaises(NBIParseError) as rejected:
                    parse_fixed(sample)
                self.assertEqual(rejected.exception.issue.code, "fixed_record_length")

    def test_accept_with_issue_marks_long_record_nonconformant(self):
        result = parse_fixed(
            fixed_record() + b"EXTRA",
            issue_policy=IssuePolicy.ACCEPT_WITH_ISSUE,
        )

        self.assertEqual(len(result.records), 1)
        self.assertFalse(result.records[0].conformant)
        self.assertEqual(result.records[0].issue_codes, ("fixed_record_length",))

    def test_single_byte_fallback_encoding_preserves_fixed_byte_offsets(self):
        record = bytearray(fixed_record())
        record[100] = 0xE9
        result = parse_fixed(bytes(record), encoding="cp1252")
        self.assertEqual(result.records[0].fields["LOCATION_009"], "é")

        with self.assertRaises(NBIParseError) as rejected:
            parse_fixed(bytes(record), encoding="ascii")
        self.assertEqual(rejected.exception.issue.code, "decode_error")

    def test_unsupported_multibyte_encoding_is_explicit(self):
        with self.assertRaises(NBIParseError) as rejected:
            parse_fixed(fixed_record(), encoding="utf-16")
        self.assertEqual(
            rejected.exception.issue.code, "unsupported_multibyte_encoding"
        )

    def test_utf8_bom_requires_sig_encoding_and_preserves_offset(self):
        data = b"\xef\xbb\xbf" + fixed_record()
        with self.assertRaises(NBIParseError) as rejected:
            parse_fixed(data, encoding="utf-8")
        self.assertEqual(rejected.exception.issue.code, "unexpected_bom")

        accepted = parse_fixed(data, encoding="utf-8-sig")
        self.assertEqual(accepted.records[0].byte_offset, 3)
        self.assertEqual(accepted.records[0].raw_item_001, "342")

    def test_unknown_record_type_is_rejected_or_quarantined(self):
        with self.assertRaises(NBIParseError) as rejected:
            parse_fixed(fixed_record(record_type="B"))
        self.assertEqual(rejected.exception.issue.code, "unknown_record_type")

        result = parse_fixed(
            fixed_record(record_type="B"),
            issue_policy=IssuePolicy.QUARANTINE,
        )
        self.assertFalse(result.records)
        self.assertEqual(
            result.quarantined_records[0].issue_codes, ("unknown_record_type",)
        )

    def test_ambiguous_data_never_falls_back_to_positional_csv(self):
        with self.assertRaises(NBIParseError) as rejected:
            parse_nbi(
                b"not an NBI record\n",
                source_agency_context="NJDOT",
                encoding="utf-8",
                csv_dialect=CSV,
                positional_columns=MINIMAL_COLUMNS,
            )
        self.assertEqual(rejected.exception.issue.code, "ambiguous_format")


class StrictCsvParserTests(unittest.TestCase):
    def test_named_csv_requires_unique_complete_header(self):
        duplicate = (
            b"STATE_CODE_001,STRUCTURE_NUMBER_008,RECORD_TYPE_005A,"
            b"RECORD_TYPE_005A\n342,B1,1,1\n"
        )
        with self.assertRaises(NBIParseError) as rejected:
            parse_nbi(
                duplicate,
                source_agency_context="NJDOT",
                encoding="utf-8",
                input_format=NBIFormat.NAMED_CSV,
                csv_dialect=CSV,
            )
        self.assertEqual(rejected.exception.issue.code, "duplicate_csv_headers")

        incomplete = b"STATE_CODE_001,STRUCTURE_NUMBER_008\n342,B1\n"
        with self.assertRaises(NBIParseError) as rejected:
            parse_nbi(
                incomplete,
                source_agency_context="NJDOT",
                encoding="utf-8",
                input_format=NBIFormat.NAMED_CSV,
                csv_dialect=CSV,
            )
        self.assertEqual(rejected.exception.issue.code, "missing_required_headers")

    def test_named_csv_extra_row_values_are_explicit(self):
        data = (
            b"STATE_CODE_001,STRUCTURE_NUMBER_008,RECORD_TYPE_005A\n"
            b"342,B1,1,EXTRA\n"
        )
        with self.assertRaises(NBIParseError) as rejected:
            parse_nbi(
                data,
                source_agency_context="NJDOT",
                encoding="utf-8",
                input_format=NBIFormat.NAMED_CSV,
                csv_dialect=CSV,
            )
        self.assertEqual(rejected.exception.issue.code, "csv_row_too_long")

        result = parse_nbi(
            data,
            source_agency_context="NJDOT",
            encoding="utf-8",
            input_format=NBIFormat.NAMED_CSV,
            issue_policy=IssuePolicy.ACCEPT_WITH_ISSUE,
            csv_dialect=CSV,
        )
        self.assertEqual(result.records[0].csv_extra_values, ("EXTRA",))
        self.assertFalse(result.records[0].conformant)

    def test_named_csv_short_row_can_be_quarantined(self):
        data = (
            b"STATE_CODE_001,STRUCTURE_NUMBER_008,RECORD_TYPE_005A\n"
            b"342,B1\n"
        )
        result = parse_nbi(
            data,
            source_agency_context="NJDOT",
            encoding="utf-8",
            input_format=NBIFormat.NAMED_CSV,
            issue_policy=IssuePolicy.QUARANTINE,
            csv_dialect=CSV,
        )
        self.assertFalse(result.records)
        self.assertEqual(
            result.quarantined_records[0].issue_codes, ("csv_row_too_short",)
        )

    def test_unknown_named_column_requires_explicit_opt_in(self):
        data = (
            b"STATE_CODE_001,STRUCTURE_NUMBER_008,RECORD_TYPE_005A,VENDOR_NOTE\n"
            b"342,B1,1,reviewed\n"
        )
        with self.assertRaises(NBIParseError) as rejected:
            parse_nbi(
                data,
                source_agency_context="NJDOT",
                encoding="utf-8",
                input_format=NBIFormat.NAMED_CSV,
                csv_dialect=CSV,
            )
        self.assertEqual(rejected.exception.issue.code, "unknown_header_columns")

        result = parse_nbi(
            data,
            source_agency_context="NJDOT",
            encoding="utf-8",
            input_format=NBIFormat.NAMED_CSV,
            csv_dialect=CSV,
            allow_unknown_named_columns=True,
        )
        self.assertEqual(result.records[0].fields["VENDOR_NOTE"], "reviewed")
        self.assertIn("unknown_header_columns", {issue.code for issue in result.issues})

    def test_positional_csv_has_explicit_columns_and_auto_detection(self):
        result = parse_nbi(
            b"342, RAW-BRIDGE ,1\n",
            source_agency_context="NJDOT",
            encoding="utf-8",
            csv_dialect=CSV,
            positional_columns=MINIMAL_COLUMNS,
        )
        self.assertEqual(result.input_format, NBIFormat.LEGACY_POSITIONAL_CSV)
        record = result.records[0]
        self.assertEqual(record.bridge_key.normalized_state, "34")
        self.assertEqual(record.raw_item_008, " RAW-BRIDGE ")
        self.assertEqual(record.fields["STRUCTURE_NUMBER_008"], "RAW-BRIDGE")

    def test_utf8_multibyte_csv_is_decoded_strictly_with_byte_provenance(self):
        header = b"STATE_CODE_001,STRUCTURE_NUMBER_008,RECORD_TYPE_005A\r\n"
        row = "342,PONT-É,1\n".encode("utf-8")
        result = parse_nbi(
            header + row,
            source_agency_context="NJDOT",
            encoding="utf-8",
            input_format=NBIFormat.NAMED_CSV,
            csv_dialect=CSV,
        )
        self.assertEqual(result.records[0].raw_item_008, "PONT-É")
        self.assertEqual(result.records[0].byte_offset, len(header))
        self.assertEqual(result.records[0].raw_sha256, hashlib.sha256(row[:-1]).hexdigest())

    def test_csv_legacy_item1_still_requires_exact_three_digit_ssf(self):
        data = (
            b"STATE_CODE_001,STRUCTURE_NUMBER_008,RECORD_TYPE_005A\n"
            b"34,B1,1\n"
        )
        with self.assertRaises(NBIParseError) as rejected:
            parse_nbi(
                data,
                source_agency_context="NJDOT",
                encoding="utf-8",
                input_format=NBIFormat.NAMED_CSV,
                csv_dialect=CSV,
            )
        self.assertEqual(rejected.exception.issue.code, "invalid_state_code")


class ParserLimitTests(unittest.TestCase):
    def test_binary_stream_and_byte_limit(self):
        data = fixed_record()
        result = parse_nbi(
            io.BytesIO(data),
            source_agency_context="NJDOT",
            encoding="ascii",
            input_format=NBIFormat.FHWA_FIXED_445,
            limits=ParserLimits(max_bytes=len(data), max_records=1),
        )
        self.assertEqual(len(result.records), 1)

        with self.assertRaises(NBIParseError) as rejected:
            parse_nbi(
                io.BytesIO(data),
                source_agency_context="NJDOT",
                encoding="ascii",
                input_format=NBIFormat.FHWA_FIXED_445,
                limits=ParserLimits(max_bytes=len(data) - 1, max_records=1),
            )
        self.assertEqual(rejected.exception.issue.code, "input_too_large")

    def test_record_limit_is_hard(self):
        with self.assertRaises(NBIParseError) as rejected:
            parse_fixed(
                fixed_record() + b"\n" + fixed_record(structure="SECOND"),
                limits=ParserLimits(max_bytes=1024, max_records=1),
            )
        self.assertEqual(rejected.exception.issue.code, "record_limit_exceeded")

    def test_short_read_binary_stream_is_consumed_to_eof(self):
        class ChunkedStream:
            def __init__(self, data):
                self.data = data
                self.offset = 0

            def read(self, _requested):
                if self.offset >= len(self.data):
                    return b""
                end = min(self.offset + 7, len(self.data))
                chunk = self.data[self.offset:end]
                self.offset = end
                return chunk

        data = fixed_record()
        result = parse_nbi(
            ChunkedStream(data),
            source_agency_context="NJDOT",
            encoding="ascii",
            input_format=NBIFormat.FHWA_FIXED_445,
            limits=ParserLimits(max_bytes=len(data), max_records=1),
        )
        self.assertEqual(result.bytes_read, len(data))
        self.assertEqual(len(result.records), 1)


if __name__ == "__main__":
    unittest.main()
