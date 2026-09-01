"""Tests for the NOTAM parser.

The messages below are **format examples**, written to exercise the grammar
defined by ICAO Annex 15 and PANS-AIM. They are not captured traffic and are
not presented as real NOTAM: no aerodrome here is being described, only the
shape of a message. That distinction is why this file does not violate the
no-mock rule — the subject under test is a published format, not a State's data.

The parser must still be re-validated against captured NOTAM before anything
operational depends on it. The specification says what a message should look
like; States are inventive.
"""

from datetime import datetime, timezone

import pytest

from aeropub.notam import (
    CONDITIONS,
    SUBJECTS,
    NotamKind,
    decode_qcode,
    parse,
    parse_validity,
)

# A complete message exercising every item.
FULL = (
    "A2291/26 NOTAMN\n"
    "Q) OTDF/QMRLC/IV/NBO/A/000/999/2516N05133E005\n"
    "A) OTHH\n"
    "B) 2610120600\n"
    "C) 2610202359\n"
    "D) DAILY 0600-1800\n"
    "E) RWY 34L/16R CLSD DUE WIP\n"
    "F) SFC\n"
    "G) 1000FT AMSL"
)

MINIMAL = (
    "B0042/26 NOTAMN\n"
    "Q) OERR/QFAXX/IV/NBO/A/000/999/2438N04643E005\n"
    "A) OERK\n"
    "B) 2609010000\n"
    "C) PERM\n"
    "E) AD REFERENCE POINT REVISED"
)


class TestHeader:
    def test_identifier_is_parsed(self):
        notam = parse(FULL)
        assert (notam.series, notam.number, notam.year) == ("A", 2291, 26)
        assert notam.identifier == "A2291/26"

    def test_a_new_notam_references_nothing(self):
        assert parse(FULL).kind is NotamKind.NEW
        assert parse(FULL).supersedes is None

    def test_a_replacement_names_what_it_replaces(self):
        notam = parse("A2300/26 NOTAMR A2291/26\nE) EXTENDED")
        assert notam.kind is NotamKind.REPLACE
        assert notam.supersedes == "A2291/26"

    def test_a_cancellation_names_what_it_cancels(self):
        notam = parse("A2400/26 NOTAMC A2291/26\nE) CNL")
        assert notam.kind is NotamKind.CANCEL
        assert notam.supersedes == "A2291/26"

    def test_an_unidentifiable_message_is_rejected_outright(self):
        # Nothing to attach a finding to, so partial acceptance is worse than
        # refusal — it would enter the store as an anonymous fragment.
        with pytest.raises(ValueError, match="no NOTAM header"):
            parse("RWY 34L CLSD")


class TestQLine:
    def test_every_field_is_extracted(self):
        q = parse(FULL).q
        assert q.fir == "OTDF"
        assert q.code == "QMRLC"
        assert q.traffic == "IV"
        assert q.purpose == "NBO"
        assert q.scope == "A"
        assert (q.lower_fl, q.upper_fl) == (0, 999)
        assert (q.latitude, q.longitude) == ("2516N", "05133E")
        assert q.radius_nm == 5

    def test_the_code_splits_into_subject_and_condition(self):
        q = parse(FULL).q
        assert q.subject_code == "MR"
        assert q.condition_code == "LC"

    def test_a_known_code_decodes_to_plain_language(self):
        assert parse(FULL).q.decoded == "runway closed"

    def test_scope_is_readable(self):
        q = parse(FULL).q
        assert q.is_aerodrome_scope
        assert not q.is_enroute_scope

    def test_a_message_without_a_q_line_still_parses(self):
        assert parse("A2300/26 NOTAMR A2291/26\nE) EXTENDED").q is None


class TestPartialDecoding:
    """Structure is certain; meaning is admitted to be partial."""

    def test_an_unknown_subject_leaves_the_reading_undecided(self):
        # Never "runway <unknown>" — a half-decoded reading looks like a
        # complete one, which is the misreading most worth avoiding.
        notam = parse(FULL.replace("QMRLC", "QZZLC"))
        assert notam.q.subject_code == "ZZ"
        assert notam.q.subject is None
        assert notam.q.condition == "closed"
        assert notam.q.decoded is None

    def test_an_unknown_condition_leaves_the_reading_undecided(self):
        notam = parse(FULL.replace("QMRLC", "QMRZZ"))
        assert notam.q.subject == "runway"
        assert notam.q.condition is None
        assert notam.q.decoded is None

    def test_structure_survives_a_wholly_unknown_code(self):
        notam = parse(FULL.replace("QMRLC", "QZZZZ"))
        assert notam.q.code == "QZZZZ"
        assert notam.q.radius_nm == 5
        assert notam.q.decoded is None

    def test_decode_qcode_rejects_a_malformed_code(self):
        with pytest.raises(ValueError, match="Q plus four letters"):
            decode_qcode("MRLC")


class TestItems:
    def test_location_indicators(self):
        assert parse(FULL).locations == ("OTHH",)

    def test_multiple_locations_are_all_captured(self):
        notam = parse(FULL.replace("A) OTHH", "A) OTHH OTBD"))
        assert notam.locations == ("OTHH", "OTBD")

    def test_validity_window(self):
        notam = parse(FULL)
        assert notam.valid_from == datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)
        assert notam.valid_to == datetime(2026, 10, 20, 23, 59, tzinfo=timezone.utc)

    def test_free_text(self):
        assert parse(FULL).text == "RWY 34L/16R CLSD DUE WIP"

    def test_schedule(self):
        assert parse(FULL).schedule == "DAILY 0600-1800"

    def test_limits(self):
        notam = parse(FULL)
        assert notam.lower_limit == "SFC"
        assert notam.upper_limit == "1000FT AMSL"

    def test_absent_optional_items_are_none(self):
        notam = parse(MINIMAL)
        assert notam.schedule is None
        assert notam.lower_limit is None
        assert notam.upper_limit is None

    def test_free_text_running_to_the_end_is_not_truncated(self):
        notam = parse(MINIMAL)
        assert notam.text == "AD REFERENCE POINT REVISED"

    def test_multi_line_free_text_is_kept_whole(self):
        notam = parse(
            "A0001/26 NOTAMN\nA) OTHH\nB) 2610120600\nC) 2610202359\n"
            "E) RWY 34L CLSD\nCONTACT TWR FOR DETAILS"
        )
        assert "CONTACT TWR" in notam.text


class TestValidity:
    def test_a_timestamp_parses_to_utc(self):
        moment, permanent, estimated = parse_validity("2610202359")
        assert moment == datetime(2026, 10, 20, 23, 59, tzinfo=timezone.utc)
        assert not permanent and not estimated

    def test_perm_has_no_end(self):
        moment, permanent, estimated = parse_validity("PERM")
        assert moment is None and permanent

    def test_an_estimated_end_is_flagged(self):
        # Worth keeping: an estimated end is a NOTAM likely to be extended,
        # which is the signal behind tracking a crane that never comes down.
        moment, permanent, estimated = parse_validity("2610202359EST")
        assert moment == datetime(2026, 10, 20, 23, 59, tzinfo=timezone.utc)
        assert estimated and not permanent

    def test_a_permanent_notam_reads_as_permanent(self):
        notam = parse(MINIMAL)
        assert notam.permanent
        assert notam.valid_to is None

    def test_unparseable_validity_yields_no_moment_rather_than_a_guess(self):
        moment, permanent, estimated = parse_validity("WHENEVER")
        assert moment is None and not permanent


class TestInForce:
    def test_before_the_start_it_is_not_in_force(self):
        notam = parse(FULL)
        assert not notam.is_in_force(datetime(2026, 10, 11, tzinfo=timezone.utc))

    def test_inside_the_window_it_is(self):
        notam = parse(FULL)
        assert notam.is_in_force(datetime(2026, 10, 15, tzinfo=timezone.utc))

    def test_after_the_end_it_is_not(self):
        notam = parse(FULL)
        assert not notam.is_in_force(datetime(2026, 10, 21, tzinfo=timezone.utc))

    def test_a_permanent_notam_never_expires(self):
        notam = parse(MINIMAL)
        assert notam.is_in_force(datetime(2030, 1, 1, tzinfo=timezone.utc))


class TestTolerance:
    def test_a_message_on_one_line_parses(self):
        notam = parse(FULL.replace("\n", " "))
        assert notam.q.code == "QMRLC"
        assert notam.text == "RWY 34L/16R CLSD DUE WIP"

    def test_leading_whitespace_is_ignored(self):
        assert parse("   " + FULL).identifier == "A2291/26"

    def test_the_raw_message_is_retained(self):
        # The archive holds the artefact; the parsed object holds the text it
        # came from, so a finding can always be shown against the original.
        assert parse(FULL).raw == FULL


class TestCodeTables:
    @pytest.mark.parametrize("table", [SUBJECTS, CONDITIONS])
    def test_keys_are_two_uppercase_letters(self, table):
        for code in table:
            assert len(code) == 2 and code.isupper() and code.isalpha()

    @pytest.mark.parametrize("table", [SUBJECTS, CONDITIONS])
    def test_a_meaning_never_just_repeats_its_code(self, table):
        # "VOR" is a perfectly good meaning for NV — an operator says "VOR
        # unserviceable", not "VHF omnidirectional radio range unserviceable".
        # What must not happen is a code mapping to itself.
        for code, meaning in table.items():
            assert meaning, f"{code} has no meaning"
            assert meaning.upper() != code, f"{code} maps to itself"

    def test_the_module_declares_its_tables_partial(self):
        # A deliberate subset. If it ever claims completeness without being
        # checked against Doc 8126, this is the reminder.
        import aeropub.notam as module
        assert "partial" in module.__doc__
        assert "Doc 8126" in module.__doc__
        assert "re-validated" in module.__doc__
