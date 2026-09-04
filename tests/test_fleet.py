"""The fleet library — the base that makes the first session a lookup.

Most of what is tested here is separation and refusal, because those are the
two ways this module goes wrong quietly.

**Separation.** A register, an operator's fleet list and an observation are
three different claims about one tail, and every one of them is true about
something different. The failure this guards against is a lessor appearing in
an operator's fleet because a register entry was read as a statement of
operation.

**Refusal.** A fleet that silently shrinks to the types with figures produces
an assessment that looks complete and covers half the aeroplanes. Every gap in
here is asserted to survive into the document rather than being dropped on the
way.

The designators, marks and operators below are fixtures. They cite a file
created beside them, which is the manifest format working as intended — a
citation is only writable when the document is there to be hashed — and none
of them is a claim about a real aeroplane.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from aeropub.aip import AipCoverage
from aeropub.aircraft import AircraftType, Characteristic, Origin
from aeropub.api import to_json
from aeropub.dossier import build
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.fleet import (
    Basis,
    FleetLibrary,
    Holding,
    OperatorRecord,
    Registration,
    Segment,
    TypeCoverage,
    TypeReference,
    fleet_of,
    library_template,
    load_library,
    merge_libraries,
    route_profile,
    screen,
)
from aeropub.manifest import ManifestError
from aeropub.notam_register import NotamRegister
from aeropub.operator import Role, assess_operator
from aeropub.provenance import SourceRef
from aeropub.suitability import Assessment

READ_AT = "2026-09-01T12:00:00Z"
NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
ON = date(2026, 10, 5)
AD = "XXXX"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST", document="test fixture — not a real publication",
        locator="register", retrieved_at=NOW, content_hash="e" * 64,
        parser_id="test", parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def characteristic(attribute: str, value, **overrides) -> Characteristic:
    fields = dict(attribute=attribute, value=value, source=ref(), origin=Origin.ACAP)
    fields.update(overrides)
    return Characteristic(**fields)


def aircraft(designator: str, *items) -> AircraftType:
    return AircraftType(designator=designator).with_characteristics(items)


#: Two fixtures differing only in size. WIDE needs fire Category 9 and a code E
#: runway; NARR needs Category 6 and fits code C.
WIDE = aircraft(
    "WIDE",
    characteristic("wingspan_m", 60.0),
    characteristic("omgws_m", 12.0),
    characteristic("overall_length_m", 70.0),
    characteristic("fuselage_width_m", 6.0),
)
NARR = aircraft(
    "NARR",
    characteristic("wingspan_m", 34.0),
    characteristic("omgws_m", 8.0),
    characteristic("overall_length_m", 38.0),
    characteristic("fuselage_width_m", 4.0),
)


def registration(mark: str, designator: str, **overrides) -> Registration:
    fields = dict(
        mark=mark, designator=designator, source=ref(), basis=Basis.REGISTER
    )
    fields.update(overrides)
    return Registration(**fields)


def holding(mark: str, basis: Basis = Basis.ATTESTED) -> Holding:
    return Holding(mark=mark, basis=basis, source=ref(locator="fleet list"))


def operator(icao: str, *marks, **overrides) -> OperatorRecord:
    fields = dict(
        icao=icao, name=f"{icao} test operator",
        holdings=tuple(holding(m) for m in marks),
    )
    fields.update(overrides)
    return OperatorRecord(**fields)


@pytest.fixture
def library() -> FleetLibrary:
    """One operator, two types, one of them without figures."""
    return FleetLibrary(
        operators=(operator("AAA", "T-AAAA", "T-AAAB", "T-AAAC"),),
        registrations=(
            registration("T-AAAA", "WIDE"),
            registration("T-AAAB", "WIDE"),
            registration("T-AAAC", "NARR"),
        ),
        references=(
            TypeReference(
                designator="NARR",
                publisher="Example",
                document="Airplane Characteristics for Airport Planning",
                revision="Rev A",
                locator="Table 2.1.1",
            ),
        ),
        types=(WIDE,),
    )


# --------------------------------------------------------------------------
# The three authorities, kept apart
# --------------------------------------------------------------------------


class TestBasis:
    def test_only_attestation_states_operation(self):
        assert Basis.ATTESTED.states_operation
        assert not Basis.REGISTER.states_operation
        assert not Basis.OBSERVED.states_operation

    def test_a_holding_records_which_authority_said_so(self):
        held = holding("T-AAAA", Basis.OBSERVED)
        assert held.basis is Basis.OBSERVED
        # And it is not silently promoted by being held.
        assert not held.basis.states_operation

    def test_a_registration_cannot_be_built_without_a_citation(self):
        with pytest.raises(TypeError):
            Registration(
                mark="T-AAAA", designator="WIDE", source=None, basis=Basis.REGISTER
            )

    def test_a_tail_may_be_claimed_by_more_than_one_operator(self, library):
        """A lessor's register entry and a lessee's attestation both stand.

        Returning one of them would make the platform pick a winner between two
        true statements, and the reader is the one who needs to see both.
        """
        both = FleetLibrary(
            operators=(operator("AAA", "T-AAAA"), operator("BBB", "T-AAAA")),
            registrations=(registration("T-AAAA", "WIDE"),),
            types=(WIDE,),
        )
        assert [o.icao for o in both.operators_of("T-AAAA")] == ["AAA", "BBB"]

    def test_a_stronger_claim_wins_when_libraries_merge(self):
        """Attestation beats observation for the same tail.

        Both documents are kept in the library; what merges is the operator's
        holding, and the claim that speaks to operation is the one that stands.
        """
        observed = FleetLibrary(
            operators=(
                OperatorRecord(
                    icao="AAA", name="AAA test operator",
                    holdings=(holding("T-AAAA", Basis.OBSERVED),),
                ),
            ),
        )
        attested = FleetLibrary(operators=(operator("AAA", "T-AAAA"),))
        merged = merge_libraries(observed, attested)
        record = merged.operator("AAA")
        assert len(record.holdings) == 1
        assert record.holdings[0].basis is Basis.ATTESTED

    def test_merging_does_not_overwrite_an_earlier_citation(self):
        """The newest file does not win.

        Silently preferring a later document discards the citation somebody is
        already relying on, and the discard leaves no trace.
        """
        first = FleetLibrary(
            registrations=(registration("T-AAAA", "WIDE", source=ref(locator="first")),)
        )
        second = FleetLibrary(
            registrations=(registration("T-AAAA", "NARR", source=ref(locator="second")),)
        )
        merged = merge_libraries(first, second)
        assert merged.registration("T-AAAA").source.locator == "first"


# --------------------------------------------------------------------------
# Coverage — the difference between research and an afternoon's ingest
# --------------------------------------------------------------------------


class TestCoverage:
    def test_figures_held_is_verified(self, library):
        assert library.coverage("WIDE") is TypeCoverage.VERIFIED

    def test_a_bibliography_entry_alone_is_registered(self, library):
        assert library.coverage("NARR") is TypeCoverage.REGISTERED

    def test_nothing_at_all_is_absent(self, library):
        assert library.coverage("ZZZZ") is TypeCoverage.ABSENT

    def test_only_verified_is_usable(self):
        assert TypeCoverage.VERIFIED.is_usable
        assert not TypeCoverage.REGISTERED.is_usable
        assert not TypeCoverage.ABSENT.is_usable

    def test_the_report_opens_on_the_research(self, library):
        rows = library.coverage_report()
        assert [c for _, c in rows] == sorted(
            [c for _, c in rows],
            key=lambda c: {"absent": 0, "registered": 1, "verified": 2}[c.value],
        )

    def test_a_reference_is_not_a_source_ref(self):
        """The bibliography must never be presentable as provenance.

        A citation that resolves to nobody's reading is worse than no citation:
        it is the one a reviewer stops checking.
        """
        reference = TypeReference(
            designator="NARR", publisher="Example", document="ACAP"
        )
        assert not isinstance(reference, SourceRef)
        assert not hasattr(reference, "content_hash")

    def test_a_reference_needs_a_publisher(self):
        with pytest.raises(ValueError, match="publisher"):
            TypeReference(designator="NARR", publisher="  ", document="ACAP")


# --------------------------------------------------------------------------
# Resolving an operator into a fleet
# --------------------------------------------------------------------------


class TestFleetOf:
    def test_it_resolves_tails_through_the_register_to_types(self, library):
        resolved = fleet_of(library, "AAA")
        assert [t.designator for t in resolved.fleet] == ["WIDE"]

    def test_the_undescribed_type_survives_as_a_gap(self, library):
        resolved = fleet_of(library, "AAA")
        assert [g.designator for g in resolved.gaps] == ["NARR"]
        assert resolved.designators == ("NARR", "WIDE")
        assert not resolved.is_complete

    def test_a_gap_with_a_bibliography_entry_is_actionable(self, library):
        resolved = fleet_of(library, "AAA")
        gap = resolved.gaps[0]
        assert gap.is_actionable
        assert gap.marks == ("T-AAAC",)
        assert resolved.actionable_gaps == (gap,)

    def test_a_gap_with_nothing_behind_it_is_research(self, library):
        bare = FleetLibrary(
            operators=(operator("AAA", "T-AAAZ"),),
            registrations=(registration("T-AAAZ", "ZZZZ"),),
        )
        gap = fleet_of(bare, "AAA").gaps[0]
        assert gap.coverage is TypeCoverage.ABSENT
        assert not gap.is_actionable

    def test_a_tail_with_no_register_entry_is_a_different_failure(self):
        """Unidentified is not the same as undescribed.

        One is an aeroplane we can name and cannot check; the other is an
        aeroplane we cannot name at all.
        """
        partial = FleetLibrary(
            operators=(operator("AAA", "T-AAAA", "T-NONE"),),
            registrations=(registration("T-AAAA", "WIDE"),),
            types=(WIDE,),
        )
        resolved = fleet_of(partial, "AAA")
        assert resolved.unidentified == ("T-NONE",)
        assert resolved.gaps == ()
        assert not resolved.is_complete

    def test_an_unknown_operator_raises_rather_than_returning_nothing(self, library):
        """An empty fleet and an operator nobody has loaded print the same.

        They are opposite answers, so the one that cannot be checked raises.
        """
        with pytest.raises(KeyError, match="coverage gap"):
            fleet_of(library, "ZZZ")

    def test_an_operator_resolves_by_iata_and_by_name(self):
        record = OperatorRecord(
            icao="AAA", iata="AA", name="Example Air", holdings=()
        )
        found = FleetLibrary(operators=(record,))
        assert found.operator("AAA") is record
        assert found.operator("AA") is record
        assert found.operator("example air") is record
        assert found.operator("QQQ") is None

    def test_the_fleet_hands_straight_to_layer_three(self, library):
        profile = fleet_of(library, "AAA").as_profile()
        assert profile.name == "AAA test operator"
        assert [t.designator for t in profile.fleet] == ["WIDE"]
        # No network is invented. Layer three renders that as NOT_IN_NETWORK,
        # which is a real answer, rather than as no exposure.
        assert len(profile.network) == 0

    def test_a_duplicate_tail_is_refused(self):
        with pytest.raises(ValueError, match="more than once"):
            OperatorRecord(
                icao="AAA", name="AAA",
                holdings=(holding("T-AAAA"), holding("T-AAAA", Basis.OBSERVED)),
            )

    def test_fleet_size_counts_what_is_held_not_what_is_claimed(self, library):
        assert library.operator("AAA").fleet_size == 3


class TestRanking:
    def test_the_top_operators_are_counted_not_listed(self):
        """"The top 50" is answered from held records, never from source.

        A ranking written into code would claim a completeness nobody
        verified, and would go stale without anything failing.
        """
        many = FleetLibrary(
            operators=(
                operator("AAA", "T-AAAA"),
                operator("BBB", "T-BBBA", "T-BBBB", "T-BBBC"),
                operator("CCC", "T-CCCA", "T-CCCB"),
            ),
        )
        assert [o.icao for o in many.ranked_by_fleet_size()] == ["BBB", "CCC", "AAA"]
        assert [o.icao for o in many.ranked_by_fleet_size(2)] == ["BBB", "CCC"]

    def test_segments_are_selectable(self):
        mixed = FleetLibrary(
            operators=(
                operator("AAA", segment=Segment.COMMERCIAL),
                operator("BBB", segment=Segment.BUSINESS),
                operator("CCC", segment=Segment.PRIVATE),
            ),
        )
        assert [o.icao for o in mixed.segment(Segment.BUSINESS)] == ["BBB"]

    def test_only_scheduled_operations_have_a_discoverable_network(self):
        """The reason business aviation needs a stated city pair.

        A management company flies a different pairing every week; reading past
        sectors as a network produces a profile that is wrong the first time
        the customer files a plan.
        """
        assert Segment.COMMERCIAL.has_discoverable_network
        assert Segment.CARGO.has_discoverable_network
        assert not Segment.BUSINESS.has_discoverable_network
        assert not Segment.PRIVATE.has_discoverable_network


# --------------------------------------------------------------------------
# Screening a fleet against an aerodrome
# --------------------------------------------------------------------------


def fact(entity: str, attribute: str, value) -> Fact:
    return Fact(
        entity=entity, attribute=attribute, value=value,
        valid_from=date(2026, 1, 1), source=ref(locator="AD 2"),
        precedence=Precedence.AIP,
    )


def dossier(*facts):
    return build(
        AD, facts=FactStore(facts), coverage=AipCoverage(),
        register=NotamRegister(), as_at=NOW, on=ON,
    )


#: A code C aerodrome with fire Category 6 — enough for NARR, not for WIDE.
SMALL_FIELD = (
    fact(AD, "aerodrome_reference_code", "3C"),
    fact(AD, "rffs_category", 6),
)


class TestScreen:
    @pytest.fixture
    def both_types(self) -> FleetLibrary:
        return FleetLibrary(
            operators=(operator("AAA", "T-AAAA", "T-AAAC"),),
            registrations=(
                registration("T-AAAA", "WIDE"),
                registration("T-AAAC", "NARR"),
            ),
            types=(WIDE, NARR),
        )

    def test_the_whole_fleet_is_screened_at_once(self, both_types):
        result = screen(both_types, "AAA", dossier(*SMALL_FIELD))
        assert {s.designator for s in result.screened} == {"WIDE", "NARR"}

    def test_the_answer_differs_by_type_at_one_aerodrome(self, both_types):
        """The reason the library is worth building.

        One aerodrome, one operator, two answers — and both fall out of held
        figures rather than a table of assertions.
        """
        result = screen(both_types, "AAA", dossier(*SMALL_FIELD))
        assert "WIDE" in result.not_suitable
        assert "WIDE" not in result.suitable
        assert "NARR" not in result.not_suitable

    def test_each_screened_type_carries_the_tails_it_is_about(self, both_types):
        result = screen(both_types, "AAA", dossier(*SMALL_FIELD))
        wide = next(s for s in result.screened if s.designator == "WIDE")
        assert wide.marks == ("T-AAAA",)

    def test_a_type_with_no_figures_is_reported_not_dropped(self, library):
        result = screen(library, "AAA", dossier(*SMALL_FIELD))
        assert [g.designator for g in result.gaps] == ["NARR"]
        assert "NARR" in result.unchecked
        assert not result.is_complete

    def test_unchecked_merges_the_two_ways_of_not_knowing(self, library):
        """A missing figure and an unread manual are one answer: not yet.

        Splitting them in the summary invites a reader to count only the first
        and conclude the fleet is covered. Against an aerodrome that publishes
        nothing, WIDE is unchecked because the aerodrome side is missing and
        NARR because the aircraft side is; both belong in the same line.
        """
        result = screen(library, "AAA", dossier())
        assert "WIDE" in result.unchecked  # nothing published to check against
        assert "NARR" in result.unchecked  # no figures held to check with
        assert result.suitable == ()

    def test_a_definite_failure_still_leaves_the_type_inconclusive(self, library):
        """"Not suitable on one check with two unmade" is not "not suitable".

        A definite failure dominates the roll-up, which is right — but the
        unmade checks beneath it may be hiding a second failure, and the
        verdict does not cover them.
        """
        result = screen(library, "AAA", dossier(fact(AD, "rffs_category", 6)))
        wide = next(s for s in result.screened if s.designator == "WIDE")
        assert wide.assessment is Assessment.NOT_SUITABLE
        assert not wide.is_conclusive

    def test_narrowing_screens_a_sub_fleet(self, both_types):
        result = screen(both_types, "AAA", dossier(*SMALL_FIELD), designators=["NARR"])
        assert [s.designator for s in result.screened] == ["NARR"]

    def test_a_fleet_with_no_figures_screens_nothing_and_says_so(self):
        """Zero types screened must not read as zero problems."""
        bare = FleetLibrary(
            operators=(operator("AAA", "T-AAAZ"),),
            registrations=(registration("T-AAAZ", "ZZZZ"),),
        )
        result = screen(bare, "AAA", dossier(*SMALL_FIELD))
        assert result.screened == ()
        assert result.suitable == ()
        assert result.unchecked == ("ZZZZ",)
        assert not result.is_complete
        assert "NOT SCREENED" in result.render()

    def test_an_operator_with_nothing_held_is_a_coverage_gap(self):
        result = screen(FleetLibrary(operators=(operator("AAA"),)), "AAA",
                        dossier(*SMALL_FIELD))
        assert result.screened == ()
        assert not result.is_complete
        assert "Nothing to screen" in result.render()


# --------------------------------------------------------------------------
# Departure to destination
# --------------------------------------------------------------------------


class TestRouteProfile:
    def test_it_builds_a_profile_for_one_city_pair(self, library):
        profile = route_profile(
            library, "AAA", departure="AAAA", destination="BBBB",
            alternates=["CCCC", "DDDD"],
        )
        assert profile.network.role_of("AAAA") is Role.DESTINATION
        assert profile.network.role_of("BBBB") is Role.DESTINATION
        assert profile.network.role_of("CCCC") is Role.ALTERNATE

    def test_the_departure_is_not_given_the_enroute_role(self, library):
        """Pavement and fire category matter at the field you are sitting on.

        The en-route role deliberately excludes both, so giving the departure
        aerodrome that role would quietly drop the checks that keep an
        aeroplane out of trouble on the return.
        """
        profile = route_profile(library, "AAA", departure="AAAA", destination="BBBB")
        assert profile.network.role_of("AAAA") is not Role.ENROUTE

    def test_a_single_alternate_is_sole_suitable(self, library):
        """With one named alternate there is by definition nothing to swap to."""
        profile = route_profile(
            library, "AAA", departure="AAAA", destination="BBBB",
            alternates=["CCCC"],
        )
        assert profile.network.is_sole_suitable("CCCC")

    def test_several_alternates_leave_the_judgement_with_the_operator(self, library):
        profile = route_profile(
            library, "AAA", departure="AAAA", destination="BBBB",
            alternates=["CCCC", "DDDD"],
        )
        assert not profile.network.is_sole_suitable("CCCC")

    def test_a_takeoff_alternate_is_always_relied_upon(self, library):
        profile = route_profile(
            library, "AAA", departure="AAAA", destination="BBBB",
            takeoff_alternate="EEEE",
        )
        assert profile.network.role_of("EEEE") is Role.TAKEOFF_ALTERNATE
        assert profile.network.is_sole_suitable("EEEE")

    def test_enroute_alternates_take_the_most_demanding_role(self, library):
        profile = route_profile(
            library, "AAA", departure="AAAA", destination="BBBB",
            enroute_alternates=["FFFF"],
        )
        assert profile.network.role_of("FFFF") is Role.EDTO_ALTERNATE

    def test_the_profile_assesses_end_to_end(self, library):
        """The whole point: a city pair in, a layer-three assessment out."""
        profile = route_profile(
            library, "AAA", departure=AD, destination="BBBB", alternates=["CCCC"]
        )
        assessment = assess_operator(dossier(*SMALL_FIELD), profile)
        assert assessment.findings
        assert any(f.designator == "WIDE" for f in assessment.findings)

    def test_it_can_be_narrowed_to_one_type(self, library):
        profile = route_profile(
            library, "AAA", departure="AAAA", destination="BBBB",
            designators=["WIDE"],
        )
        assert [t.designator for t in profile.fleet] == ["WIDE"]


# --------------------------------------------------------------------------
# Reading a library document
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "register.txt"
    path.write_text("a register extract, standing in for one somebody read\n",
                    encoding="utf-8")
    return path


def write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def library_document(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "Civil aircraft register extract",
            "document_path": "register.txt",
            "retrieved_at": READ_AT,
        },
        "basis": "register",
        "registrations": [
            {"mark": "T-AAAA", "designator": "WIDE", "owner": "Example Leasing",
             "recorded_on": "2026-03-01", "locator": "row 1"},
        ],
        "operators": [
            {"icao": "AAA", "iata": "AA", "name": "Example Air",
             "segment": "commercial", "bases": ["XXXX"],
             "holdings": [{"mark": "T-AAAA", "locator": "row 1"}]},
        ],
        "references": [
            {"designator": "NARR", "publisher": "Example",
             "document": "Airplane Characteristics for Airport Planning",
             "revision": "Rev A", "locator": "Table 2.1.1"},
        ],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_library_loads_with_every_statement_cited(self, tmp_path, document):
        path = write(tmp_path, "library.json", library_document())
        loaded = load_library(path)
        registration = loaded.registration("T-AAAA")
        assert registration.source.document == "Civil aircraft register extract"
        assert registration.source.locator == "row 1"
        assert registration.recorded_on == date(2026, 3, 1)

    def test_the_document_is_hashed_as_it_is_read(self, tmp_path, document):
        path = write(tmp_path, "library.json", library_document())
        loaded = load_library(path)
        assert len(loaded.registration("T-AAAA").source.content_hash) == 64

    def test_the_documents_basis_applies_to_every_record(self, tmp_path, document):
        path = write(tmp_path, "library.json", library_document())
        loaded = load_library(path)
        assert loaded.registration("T-AAAA").basis is Basis.REGISTER
        assert loaded.operator("AAA").holdings[0].basis is Basis.REGISTER

    def test_a_record_may_not_declare_its_own_basis(self, tmp_path, document):
        """One document makes one kind of claim.

        A record on a different basis would come out cited to this document,
        and the citation would resolve to a page that does not contain it.
        """
        payload = library_document()
        payload["registrations"][0]["basis"] = "attested"
        path = write(tmp_path, "library.json", payload)
        with pytest.raises(ManifestError, match="own basis"):
            load_library(path)

    def test_a_library_making_claims_needs_a_basis(self, tmp_path, document):
        payload = library_document()
        del payload["basis"]
        path = write(tmp_path, "library.json", payload)
        with pytest.raises(ManifestError, match="basis is required"):
            load_library(path)

    def test_a_bibliography_only_document_needs_no_basis(self, tmp_path, document):
        """The bibliography ships before anybody has read anything."""
        payload = library_document()
        del payload["basis"]
        del payload["registrations"]
        del payload["operators"]
        path = write(tmp_path, "library.json", payload)
        loaded = load_library(path)
        assert loaded.coverage("NARR") is TypeCoverage.REGISTERED

    def test_a_registration_needs_a_locator(self, tmp_path, document):
        payload = library_document()
        del payload["registrations"][0]["locator"]
        path = write(tmp_path, "library.json", payload)
        with pytest.raises(ManifestError, match="locator"):
            load_library(path)

    def test_a_tail_with_no_type_is_refused(self, tmp_path, document):
        payload = library_document()
        del payload["registrations"][0]["designator"]
        path = write(tmp_path, "library.json", payload)
        with pytest.raises(ManifestError, match="designator"):
            load_library(path)

    def test_an_operator_needs_an_icao_designator(self, tmp_path, document):
        payload = library_document()
        del payload["operators"][0]["icao"]
        path = write(tmp_path, "library.json", payload)
        with pytest.raises(ManifestError, match="icao is required"):
            load_library(path)

    def test_an_unreadable_document_refuses_the_whole_file(self, tmp_path):
        """No partial success. A library that is nearly all cited stops being
        checked."""
        path = write(tmp_path, "library.json", library_document())
        with pytest.raises(ManifestError, match="document_path"):
            load_library(path)

    def test_holdings_may_be_bare_marks(self, tmp_path, document):
        payload = library_document()
        payload["operators"][0]["holdings"] = ["T-AAAA"]
        path = write(tmp_path, "library.json", payload)
        loaded = load_library(path)
        assert loaded.operator("AAA").marks() == ("T-AAAA",)

    def test_aircraft_manifests_arrive_through_the_acap_path(self, tmp_path, document):
        """The figure layer has one way in, not a second weaker one here."""
        manifest = {
            "designator": "NARR",
            "source": {
                "source_id": "EXAMPLE",
                "document": "Airplane Characteristics for Airport Planning",
                "document_path": "register.txt",
                "retrieved_at": READ_AT,
            },
            "characteristics": [
                {"attribute": "wingspan_m", "value": 34.0, "unit": "m",
                 "locator": "Table 2.1.1"},
            ],
        }
        write(tmp_path, "narr.json", manifest)
        payload = library_document(aircraft=["narr.json"])
        path = write(tmp_path, "library.json", payload)
        loaded = load_library(path)
        assert loaded.coverage("NARR") is TypeCoverage.VERIFIED
        assert loaded.type("NARR").value("wingspan_m") == 34.0

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(library_template())
        assert blank["basis"] == "register"
        assert "references" in blank


# --------------------------------------------------------------------------
# The API payload
# --------------------------------------------------------------------------


class TestApiPayload:
    def test_a_fleet_payload_states_whether_it_is_complete(self, library):
        payload = to_json(fleet_of(library, "AAA"))
        assert payload["complete"] is False
        assert [g["designator"] for g in payload["gaps"]] == ["NARR"]

    def test_a_gap_carries_the_document_to_go_and_read(self, library):
        payload = to_json(fleet_of(library, "AAA"))
        gap = payload["gaps"][0]
        assert gap["actionable"] is True
        assert gap["references"][0]["publisher"] == "Example"
        # And it is not dressed as provenance.
        assert "content_hash" not in gap["references"][0]

    def test_every_figure_in_the_payload_keeps_its_citation(self, library):
        payload = to_json(fleet_of(library, "AAA"))
        held = payload["types"][0]["characteristics"]
        assert held
        assert all("source_ref" in c for c in held)

    def test_a_screen_payload_separates_unchecked_from_suitable(self, library):
        result = screen(library, "AAA", dossier(*SMALL_FIELD))
        payload = to_json(result)
        assert "NARR" in payload["unchecked"]
        assert "NARR" not in payload["suitable"]
        assert payload["complete"] is False


# --------------------------------------------------------------------------
# What the documents say for themselves
# --------------------------------------------------------------------------


class TestRendering:
    def test_the_fleet_says_it_is_incomplete(self, library):
        text = fleet_of(library, "AAA").render()
        assert "incomplete" in text
        assert "NARR" in text
        assert "REGISTERED" in text

    def test_an_operator_with_no_tails_does_not_read_as_a_clear_answer(self):
        empty = FleetLibrary(operators=(operator("AAA"),))
        text = fleet_of(empty, "AAA").render()
        assert "not a statement that they fly nothing" in text

    def test_the_screen_says_unchecked_is_not_a_pass(self, library):
        text = screen(library, "AAA", dossier(*SMALL_FIELD)).render()
        assert "Unchecked is not a pass" in text
