"""Reading an eAIP by profile, and refusing to read one by guesswork.

The design position under all of this: there are about 180 States and no
universal eAIP reader. A hard-coded parser fits the State it was written
against and quietly mis-reads the next one, which is worse than failing. So
layout is configuration, and the probe writes a draft of it from a real page —
which means an AIS officer can onboard their own State without anybody writing
code, and without sending a copy of the page to a stranger.

What is tested is mostly refusal. A profile that does not match must fail
loudly and name what it looked for; a value that could not be read as its
declared kind must not become a value; and a draft nobody has checked must
never produce anything at full confidence.

The documents below are written in the tests. The no-mock-data rule governs
source data entering the product; these are structural fixtures for a document
reader, and none of the values in them is presented as a State's publication.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from aeropub.eaip.parse import parse_page
from aeropub.eaip.probe import draft_profile, probe, read_document
from aeropub.eaip.profile import (
    EaipProfile,
    FieldRule,
    Locator,
    ProfileError,
    SectionRule,
    load_layout,
)
from aeropub.entities import scope_of, under
from aeropub.facts import Precedence
from aeropub.provenance import Confidence

VALID_FROM = date(2026, 9, 3)
READ_AT = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)

PAGE = """<html><body>
<div id="OTHH-AD-2.6"><h2>AD 2.6 Rescue and fire fighting services</h2>
<table><tr><td>Aerodrome category for fire fighting</td><td>CAT 9</td></tr></table></div>
<div id="OTHH-AD-2.12"><h2>AD 2.12 Runway physical characteristics</h2>
<table><tr><td>Width of RWY</td><td>60 m</td></tr>
<tr><td>Strength (PCN)</td><td>80/F/A/W/T</td></tr></table></div>
</body></html>"""


def profile(*, verified: bool = False, sections=None) -> EaipProfile:
    return EaipProfile(
        state="OT",
        name="Qatar",
        sections=sections
        if sections is not None
        else (
            SectionRule(
                code="AD 2.6",
                locate=Locator(attribute="id", pattern=r"OTHH-AD-2\.6"),
                fields=(
                    FieldRule(attribute="rffs_category",
                              label=r"category for fire fighting", kind="integer"),
                ),
            ),
            SectionRule(
                code="AD 2.12",
                locate=Locator(attribute="id", pattern=r"OTHH-AD-2\.12"),
                fields=(
                    FieldRule(attribute="runway_width_m", label=r"Width of RWY",
                              kind="number", unit="m", scope="RWY34L"),
                    FieldRule(attribute="pcn", label=r"Strength \(PCN\)",
                              kind="code", scope="RWY34L"),
                ),
            ),
        ),
        verified_at=READ_AT if verified else None,
        verified_by="an AIS officer" if verified else "",
    )


def parse(page: str = PAGE, **overrides):
    fields = dict(
        aerodrome="OTHH", document="AIP Qatar AD 2 OTHH",
        valid_from=VALID_FROM, retrieved_at=READ_AT,
    )
    fields.update(overrides)
    return parse_page(page, fields.pop("profile", profile()), **fields)


# --------------------------------------------------------------------------
# The probe reports; it does not conclude
# --------------------------------------------------------------------------


class TestProbe:
    def test_it_reports_what_the_document_contains(self):
        report = probe(PAGE, source="sample")
        assert report.tables == 2
        assert "OTHH-AD-2.12" in report.identifiers

    def test_it_counts_identifier_shapes_without_naming_them_sections(self):
        # "25 elements carry an id matching AD-2.<number>" is an observation.
        # "these are the AD 2 sections" is a conclusion, and a tool that made
        # it would be confidently wrong about some State.
        report = probe(PAGE)
        shapes = {o.what: o.count for o in report.observations}
        assert shapes["AD 2 section"] == 2

    def test_an_observation_carries_no_confidence_score(self):
        # A number there would be read as an assessment, and the probe has no
        # basis for one.
        report = probe(PAGE)
        assert not hasattr(report.observations[0], "confidence")

    def test_headings_are_reported_for_a_reader_to_check_against(self):
        report = probe(PAGE)
        assert any("Runway physical characteristics" in h for h in report.headings)

    def test_a_document_with_nothing_recognisable_says_so(self):
        report = probe("<html><body><p>Download the PDF.</p></body></html>")
        assert report.observations == ()
        assert "may be a PDF wrapper" in report.describe()

    def test_malformed_markup_is_read_rather_than_refused(self):
        # Published pages are not well formed and refusing them would make the
        # tool useless exactly where it is needed.
        report = probe('<div id="AD-2.12"><p>Width 60 m<div id="AD-2.13">TORA')
        assert "AD-2.12" in report.identifiers
        assert "AD-2.13" in report.identifiers

    def test_text_belongs_to_every_open_element(self):
        # A heading nested inside a section div must still be findable by the
        # div, or every locator would have to name the innermost element.
        elements = {e.identifier: e for e in read_document(PAGE) if e.identifier}
        assert "Width of RWY" in elements["OTHH-AD-2.12"].text


class TestDraftProfile:
    def test_every_rule_points_at_an_identifier_really_in_the_document(self):
        draft = draft_profile(PAGE, state="OT")
        present = set(probe(PAGE).identifiers)
        for rule in draft.sections:
            assert any(
                rule.locate.matches_attribute(identifier) for identifier in present
            )

    def test_it_names_the_sections_it_found(self):
        draft = draft_profile(PAGE, state="OT")
        assert {r.code for r in draft.sections} == {"AD 2.6", "AD 2.12"}

    def test_a_draft_is_never_born_verified(self):
        # Somebody has to look at it beside the page. Nothing else can.
        draft = draft_profile(PAGE, state="OT")
        assert not draft.is_verified
        assert "UNVERIFIED" in draft.describe()

    def test_a_draft_defines_no_fields(self):
        # The probe knows the element exists. It does not know what is in it,
        # and inventing field rules is exactly the guesswork this avoids.
        assert all(r.fields == () for r in draft_profile(PAGE, state="OT").sections)

    def test_a_document_with_no_section_identifiers_drafts_nothing(self):
        draft = draft_profile("<html><body><p>nothing</p></body></html>", state="OT")
        assert draft.sections == ()

    def test_a_draft_round_trips_through_json(self, tmp_path: Path):
        draft = draft_profile(PAGE, state="OT", name="Qatar")
        path = tmp_path / "ot.json"
        path.write_text(draft.dumps(), encoding="utf-8")
        loaded = load_layout(path)
        assert {r.code for r in loaded.sections} == {r.code for r in draft.sections}
        assert not loaded.is_verified


# --------------------------------------------------------------------------
# Parsing, and refusing to
# --------------------------------------------------------------------------


class TestParsing:
    def test_it_reads_what_the_profile_locates(self):
        result = parse()
        assert result.is_complete
        held = {f.attribute: f.value for f in result.facts}
        assert held == {
            "rffs_category": 9,
            "runway_width_m": 60.0,
            "pcn": "80/F/A/W/T",
        }

    def test_a_count_is_an_integer_and_a_measurement_is_not(self):
        # A fire category read as a float becomes 9.0, and the RFFS check reads
        # it with int() — which raises, and reports the aerodrome as publishing
        # something uninterpretable. Found by running the parser, not reading it.
        held = {f.attribute: f.value for f in parse().facts}
        assert held["rffs_category"] == 9
        assert isinstance(held["rffs_category"], int)
        assert isinstance(held["runway_width_m"], float)

    def test_a_fractional_count_is_not_truncated_into_one(self):
        page = PAGE.replace("CAT 9", "CAT 9.5")
        result = parse(page)
        assert not any(f.attribute == "rffs_category" for f in result.facts)

    def test_scoped_values_are_filed_against_the_object(self):
        result = parse()
        by_attribute = {f.attribute: f.entity for f in result.facts}
        assert by_attribute["runway_width_m"] == under("OTHH", "RWY34L")
        assert scope_of(by_attribute["runway_width_m"]) == "RWY34L"
        assert by_attribute["rffs_category"] == "OTHH"

    def test_a_section_the_profile_cannot_find_is_a_miss_not_an_error(self):
        result = parse("<html><body><p>a different layout entirely</p></body></html>")
        assert len(result.missed) == 2
        assert result.facts == ()
        assert not result.is_complete
        assert "nothing matched id=~/" in result.missed[0].detail

    def test_a_miss_names_what_it_looked_for(self):
        # The answer to "why is AD 2.12 empty this cycle" belongs in the output,
        # not in somebody's debugger.
        printed = parse("<html><body></body></html>").render()
        assert "NOT FOUND" in printed
        assert "OTHH-AD-2" in printed
        assert "coverage gaps, not values" in printed

    def test_a_located_section_with_an_unreadable_field_is_partial(self):
        page = PAGE.replace("<td>60 m</td>", "<td>see remarks</td>")
        result = parse(page)
        assert result.partial
        assert not any(f.attribute == "runway_width_m" for f in result.facts)
        assert any(f.attribute == "pcn" for f in result.facts)

    def test_nothing_plausible_is_scraped_when_a_label_is_absent(self):
        page = PAGE.replace("Width of RWY", "Largeur de piste")
        result = parse(page)
        assert not any(f.attribute == "runway_width_m" for f in result.facts)

    def test_a_profile_with_no_sections_is_refused_rather_than_reporting_success(self):
        with pytest.raises(ValueError) as caught:
            parse(profile=EaipProfile(state="OT"))
        assert "parse nothing and report success" in str(caught.value)


class TestProvenance:
    def test_every_value_cites_the_section_it_was_read_from(self):
        for held in parse().facts:
            assert held.source.locator.startswith(("AD 2.6", "AD 2.12"))
            assert held.source.document == "AIP Qatar AD 2 OTHH"

    def test_the_page_is_hashed_as_parsed(self):
        result = parse()
        assert len(result.content_hash) == 64
        assert all(f.source.content_hash == result.content_hash for f in result.facts)

    def test_a_changed_page_changes_the_hash(self):
        assert parse().content_hash != parse(PAGE.replace("60 m", "45 m")).content_hash

    def test_the_parser_id_names_the_profile(self):
        # A bad rule has to trace to every value it produced, exactly as a code
        # parser's defect does.
        assert all(f.source.parser_id == "eaip-profile:OT" for f in parse().facts)

    def test_an_unverified_profile_produces_low_confidence_values(self):
        result = parse()
        assert not result.verified_profile
        assert all(f.source.confidence is Confidence.LOW for f in result.facts)
        assert "not verified" in result.render()

    def test_a_verified_profile_produces_high_confidence_values(self):
        result = parse(profile=profile(verified=True))
        assert all(f.source.confidence is Confidence.HIGH for f in result.facts)
        assert "not verified" not in result.render()

    def test_parsing_with_a_draft_is_allowed_because_verifying_requires_it(self):
        # Refusing an unverified profile would make verification impossible:
        # somebody has to parse with the draft to check it.
        assert parse().facts

    def test_values_carry_the_layer_they_were_published_at(self):
        assert all(f.precedence is Precedence.AIP for f in parse().facts)


# --------------------------------------------------------------------------
# The profile itself
# --------------------------------------------------------------------------


class TestProfileValidation:
    def test_a_locator_that_matches_everything_is_refused(self):
        with pytest.raises(ProfileError) as caught:
            Locator()
        assert "matches everything finds nothing" in str(caught.value)

    def test_a_pattern_without_an_attribute_is_refused(self):
        with pytest.raises(ProfileError):
            Locator(pattern="AD-2")

    def test_a_bad_regex_is_refused_at_load_rather_than_at_parse(self):
        with pytest.raises(ProfileError):
            Locator(attribute="id", pattern="AD-2.(")

    def test_patterns_are_anchored_so_ad_2_1_does_not_match_ad_2_12(self):
        # The bug this prevents: a rule for AD 2.1 silently swallowing AD 2.12.
        narrow = Locator(attribute="id", pattern=r"AD-2\.1")
        assert narrow.matches_attribute("AD-2.1")
        assert not narrow.matches_attribute("AD-2.12")

    def test_an_unknown_field_kind_is_refused(self):
        with pytest.raises(ProfileError) as caught:
            FieldRule(attribute="x", label="X", kind="whatever")
        assert "no declared kind" in str(caught.value)

    def test_two_rules_for_one_section_are_refused(self):
        # Whichever runs last would win, silently.
        with pytest.raises(ProfileError) as caught:
            EaipProfile(state="OT", sections=(
                SectionRule(code="AD 2.12", locate=Locator(attribute="id", pattern="a")),
                SectionRule(code="AD 2.12", locate=Locator(attribute="id", pattern="b")),
            ))
        assert "more than once" in str(caught.value)

    def test_a_profile_needs_a_state(self):
        with pytest.raises(ProfileError):
            EaipProfile(state="  ")

    def test_a_field_needs_both_a_name_and_a_label(self):
        with pytest.raises(ProfileError):
            FieldRule(attribute="", label="Width")
        with pytest.raises(ProfileError):
            FieldRule(attribute="runway_width_m", label="")

    def test_a_malformed_profile_file_names_itself(self, tmp_path: Path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ProfileError) as caught:
            load_layout(path)
        assert "broken.json" in str(caught.value)

    def test_the_section_lookup_is_case_and_space_insensitive(self):
        assert profile().section("ad  2.12") is not None
        assert profile().section("AD 2.99") is None


class TestTheFieldReaderDoesNotWalkIntoTheNextRow:
    """The bug this guards was found by running the parser, not reading it.

    The field reader matched a label against the section's flattened text and
    took the next 120 characters. With the width cell reading "see remarks" it
    returned 80 — out of the PCN in the row below. A runway width of 80 metres,
    confidently, from the pavement rating.
    """

    def unreadable_width(self) -> str:
        return PAGE.replace("<td>60 m</td>", "<td>see remarks</td>")

    def test_an_unreadable_cell_does_not_borrow_the_next_row(self):
        result = parse(self.unreadable_width())
        assert not any(f.attribute == "runway_width_m" for f in result.facts)

    def test_and_the_section_is_reported_partial_rather_than_complete(self):
        result = parse(self.unreadable_width())
        assert result.partial
        assert not result.is_complete

    def test_the_neighbouring_field_still_reads_correctly(self):
        held = {f.attribute: f.value for f in parse(self.unreadable_width()).facts}
        assert held["pcn"] == "80/F/A/W/T"

    def test_a_label_and_value_in_one_cell_are_read(self):
        page = PAGE.replace(
            "<tr><td>Width of RWY</td><td>60 m</td></tr>",
            "<tr><td>Width of RWY 45 m</td></tr>",
        )
        held = {f.attribute: f.value for f in parse(page).facts}
        assert held["runway_width_m"] == 45.0

    def test_prose_sections_still_read(self):
        # Not every State lays every section out as a table.
        page = """<html><body>
        <div id="OTHH-AD-2.12"><h2>AD 2.12</h2>
        <p>Width of RWY 45 m. Strength (PCN) 62/F/B/X/T.</p></div></body></html>"""
        held = {f.attribute: f.value for f in parse(page).facts}
        assert held["runway_width_m"] == 45.0

    def test_the_prose_fallback_stops_at_the_next_known_label(self):
        # Same defect, same fix: without the boundary the width would take the
        # 62 out of the PCN that follows it.
        page = """<html><body>
        <div id="OTHH-AD-2.12"><h2>AD 2.12</h2>
        <p>Width of RWY see remarks. Strength (PCN) 62/F/B/X/T.</p></div></body></html>"""
        held = {f.attribute: f.value for f in parse(page).facts}
        assert "runway_width_m" not in held
        assert held["pcn"] == "62/F/B/X/T"


class TestTheLoopCloses:
    """A parsed page reaching an operational answer, with citations intact.

    This is the proof the whole package exists for: a page a State published,
    read by a profile a person wrote, becoming facts that drive a suitability
    assessment — with every value still resolving to the section it came from.
    Without this test the package is a document reader with no destination.
    """

    def test_a_parsed_page_drives_a_suitability_assessment(self):
        from aeropub.aip import AipCoverage
        from aeropub.aircraft import AircraftType, Characteristic, Origin
        from aeropub.dossier import build
        from aeropub.facts import FactStore
        from aeropub.notam_register import NotamRegister
        from aeropub.suitability import Assessment, assess_suitability

        result = parse(profile=profile(verified=True))
        store = FactStore(result.facts)

        source = result.facts[0].source
        aircraft = AircraftType(designator="TEST").with_characteristics([
            Characteristic(attribute=a, value=v, source=source, origin=Origin.ACAP, **k)
            for a, v, k in (
                ("wingspan_m", 60.0, {}),
                ("omgws_m", 12.0, {}),
                ("reference_field_length_m", 3100.0, {}),
                ("overall_length_m", 70.0, {}),
                ("fuselage_width_m", 6.2, {}),
                ("acn", 62.0, {"variant": "F/A at MTOW"}),
            )
        ])
        assessment = assess_suitability(
            build("OTHH", facts=store, coverage=AipCoverage(),
                  register=NotamRegister(),
                  as_at=READ_AT, on=VALID_FROM),
            aircraft,
        )
        # The fire category the page published is the one the check read.
        rffs = next(
            c for c in assessment.checks if c.name == "Rescue and fire fighting"
        )
        assert rffs.assessment is Assessment.SUITABLE
        assert "Category 9" in rffs.detail
        # And it still resolves to the section of the document it came from.
        assert any("AD 2.6" in citation for citation in rffs.citations())

    def test_an_integer_category_survives_the_whole_pipeline(self):
        # The float bug this guards would have surfaced here, three modules
        # from where it was introduced: the RFFS check reads the published
        # category with int(), and int("9.0") raises.
        from aeropub.aip import AipCoverage
        from aeropub.aircraft import AircraftType, Characteristic, Origin
        from aeropub.dossier import build
        from aeropub.facts import FactStore
        from aeropub.notam_register import NotamRegister
        from aeropub.suitability import assess_suitability

        result = parse()
        source = result.facts[0].source
        assessment = assess_suitability(
            build("OTHH", facts=FactStore(result.facts), coverage=AipCoverage(),
                  register=NotamRegister(), as_at=READ_AT, on=VALID_FROM),
            AircraftType(designator="TEST").with_characteristics([
                Characteristic(attribute="overall_length_m", value=70.0,
                               source=source, origin=Origin.ACAP),
                Characteristic(attribute="fuselage_width_m", value=6.2,
                               source=source, origin=Origin.ACAP),
            ]),
        )
        rffs = next(
            c for c in assessment.checks if c.name == "Rescue and fire fighting"
        )
        assert "not interpreted" not in rffs.detail

    def test_a_parsed_pcn_parses_as_a_pavement_rating(self):
        from aeropub.aircraft import PavementRating

        held = {f.attribute: f.value for f in parse().facts}
        assert PavementRating.pcn(held["pcn"]).number == 80.0
