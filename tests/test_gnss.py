"""ENR 4.3 — the approval that decides whether the line on the plate is real.

Three things carry this module, and they are what is asserted below.

**Silence and absence are different answers.** A region nobody has read comes
back ``UNREAD``; a region that was read and approves no SBAS comes back
``NOT_PUBLISHED``. Only the second is an answer. Collapsing them would turn a
coverage gap into a finding, which is the failure mode this whole platform is
built against.

**No prediction is ever computed.** The module reports the published
requirement and the published provider. A RAIM number derived from what this
platform holds — which is no almanac, no satellite health and no receiver
model — is a number somebody would fly on.

**Substitution is listed, not ruled on.** The same discipline as
``navaids.alternatives_to``, and for the same reason.

Every region, system and identifier below is a fixture. None is a claim about
a real State's approvals.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aeropub.entities import named
from aeropub.gnss import (
    GNSS,
    NO_PREDICTION_COMPUTED,
    ApproachCapability,
    Augmentation,
    Availability,
    Constellation,
    GnssRegister,
    GnssService,
    RaimRequirement,
    ServiceStatus,
    gnss_template,
    load_gnss,
    substitutions_for,
    view_gnss,
)
from aeropub.manifest import ManifestError
from aeropub.notam_register import (
    NotamRegister,
    RegisteredNotam,
    Subject,
    SubjectKind,
)
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
READ_AT = "2026-09-01T12:00:00Z"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 4.3",
        retrieved_at=NOW,
        content_hash="f" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def service(region: str, augmentation: Augmentation, **overrides) -> GnssService:
    fields = dict(region=region, augmentation=augmentation, source=ref())
    fields.update(overrides)
    return GnssService(**fields)


SBAS_APPROVED = service(
    "AAAA",
    Augmentation.SBAS,
    system="EXSAT",
    constellations=(Constellation.GPS,),
    status=ServiceStatus.APPROVED,
    service_area="the whole of AAAA FIR, as published",
    approved_operations=("RNP APCH", "LPV"),
)
ABAS_AAAA = service(
    "AAAA",
    Augmentation.ABAS,
    constellations=(Constellation.GPS,),
    status=ServiceStatus.APPROVED,
    approved_operations=("RNAV 5", "RNP APCH LNAV"),
    raim_prediction=RaimRequirement.REQUIRED,
    prediction_service="the AAAA AIS prediction page",
    substitutes_for=("ALP", "BRV"),
)
ABAS_BBBB = service(
    "BBBB",
    Augmentation.ABAS,
    constellations=(Constellation.GPS,),
    status=ServiceStatus.APPROVED,
    approved_operations=("RNAV 5",),
    raim_prediction=RaimRequirement.RECOMMENDED,
)


def register(*services: GnssService, covers: tuple[str, ...] = ()) -> GnssRegister:
    held = services or (SBAS_APPROVED, ABAS_AAAA, ABAS_BBBB)
    return GnssRegister(services=held, covers=frozenset(covers))


def notam(identifier: str, entity: str) -> RegisteredNotam:
    return RegisteredNotam(
        identifier=identifier,
        subjects=(Subject(kind=SubjectKind.AIRSPACE, entity=entity),),
        effective_start=NOW - timedelta(days=1),
        effective_end=NOW + timedelta(days=1),
        source=ref(locator=identifier),
        text="test fixture",
    )


# --------------------------------------------------------------------------
# What an approach line is flown on
# --------------------------------------------------------------------------


class TestCapabilities:
    def test_an_lpv_line_can_only_come_from_sbas(self):
        """The whole reason this module exists: the line is on the plate and
        the authority for it is in ENR 4.3."""
        assert ApproachCapability.LPV.satisfied_by == frozenset(
            {Augmentation.SBAS}
        )

    def test_an_lp_line_is_the_same(self):
        assert Augmentation.ABAS not in ApproachCapability.LP.satisfied_by

    def test_a_gls_line_can_only_come_from_gbas(self):
        assert ApproachCapability.GLS.satisfied_by == frozenset(
            {Augmentation.GBAS}
        )

    def test_an_lnav_line_can_be_flown_on_raim_alone(self):
        assert Augmentation.ABAS in ApproachCapability.LNAV.satisfied_by

    def test_lnav_vnav_is_not_an_sbas_only_line(self):
        """Its vertical guidance may come from baro-VNAV, which the aeroplane
        provides and the State does not have to approve as a satellite
        service."""
        assert Augmentation.ABAS in ApproachCapability.LNAV_VNAV.satisfied_by

    def test_only_lnav_has_no_vertical_guidance(self):
        assert not ApproachCapability.LNAV.has_vertical_guidance
        assert ApproachCapability.LPV.has_vertical_guidance

    def test_the_label_is_the_one_printed_on_the_plate(self):
        assert ApproachCapability.LNAV_VNAV.label == "LNAV/VNAV"


class TestAugmentation:
    def test_only_the_airborne_one_needs_a_prediction(self):
        assert Augmentation.ABAS.needs_prediction
        assert not Augmentation.SBAS.needs_prediction
        assert not Augmentation.GBAS.needs_prediction

    def test_an_unknown_status_is_not_an_approval(self):
        assert ServiceStatus.UNKNOWN.is_approved is None
        assert ServiceStatus.APPROVED.is_approved is True
        assert ServiceStatus.TRIAL.is_approved is False

    def test_a_requirement_nobody_stated_does_not_bind(self):
        assert not RaimRequirement.NOT_STATED.is_binding
        assert RaimRequirement.REQUIRED.is_binding


# --------------------------------------------------------------------------
# The four states
# --------------------------------------------------------------------------


class TestAvailability:
    def test_only_the_unread_one_is_unknown(self):
        assert Availability.UNREAD.is_available is None
        assert Availability.PUBLISHED.is_available is True
        assert Availability.NOT_PUBLISHED.is_available is False
        assert Availability.WITHDRAWN.is_available is False

    def test_an_unread_region_answers_nothing(self):
        found = view_gnss(
            register(), regions=["ZZZZ"], capabilities=[ApproachCapability.LPV]
        )
        assert found.capabilities[0].availability is Availability.UNREAD
        assert found.capabilities[0].is_available is None
        assert "ZZZZ" in found.unread_regions

    def test_a_read_region_with_no_sbas_refuses_the_lpv_line(self):
        """Read, and approving nothing that could provide it. That is an
        answer, and it is only an answer because UNREAD exists."""
        found = view_gnss(
            register(), regions=["BBBB"], capabilities=[ApproachCapability.LPV]
        )
        finding = found.capabilities[0]
        assert finding.availability is Availability.NOT_PUBLISHED
        assert "SBAS" in finding.basis

    def test_an_approved_sbas_publishes_the_lpv_line(self):
        found = view_gnss(
            register(), regions=["AAAA"], capabilities=[ApproachCapability.LPV]
        )
        finding = found.capabilities[0]
        assert finding.availability is Availability.PUBLISHED
        assert finding.service is SBAS_APPROVED

    def test_a_service_on_trial_is_not_silence(self):
        """Somebody decided this, and a dispatcher reads it differently from
        a State that never mentioned it."""
        held = register(
            service(
                "CCCC",
                Augmentation.SBAS,
                system="TRIALSAT",
                status=ServiceStatus.TRIAL,
            )
        )
        found = view_gnss(
            held, regions=["CCCC"], capabilities=[ApproachCapability.LPV]
        )
        finding = found.capabilities[0]
        assert finding.availability is Availability.WITHDRAWN
        assert "trial" in finding.basis

    def test_each_region_is_answered_on_its_own(self):
        """A sector crossing one State that approves LPV and one that does not
        gets two findings, never an average."""
        found = view_gnss(
            register(),
            regions=["AAAA", "BBBB"],
            capabilities=[ApproachCapability.LPV],
        )
        answers = {f.region: f.availability for f in found.capabilities}
        assert answers["AAAA"] is Availability.PUBLISHED
        assert answers["BBBB"] is Availability.NOT_PUBLISHED

    def test_an_explicit_list_of_lines_beats_the_physics(self):
        """A State that enumerates its approved lines has said something more
        specific than what the augmentation can technically provide."""
        held = register(
            service(
                "DDDD",
                Augmentation.SBAS,
                system="EXSAT",
                status=ServiceStatus.APPROVED,
                capabilities=(ApproachCapability.LNAV,),
            )
        )
        found = view_gnss(
            held, regions=["DDDD"], capabilities=[ApproachCapability.LPV]
        )
        assert found.capabilities[0].availability is Availability.NOT_PUBLISHED


# --------------------------------------------------------------------------
# What was read
# --------------------------------------------------------------------------


class TestCoverage:
    def test_publishing_a_row_for_a_region_is_a_claim_to_have_read_it(self):
        assert register().is_read("AAAA")

    def test_a_region_declared_read_counts_even_with_no_rows(self):
        """Reading a State's ENR 4.3 and finding it approves nothing is a
        result. Without somewhere to record it, it is indistinguishable from
        never having looked."""
        held = register(ABAS_BBBB, covers=("EEEE",))
        assert held.is_read("EEEE")
        found = view_gnss(
            held, regions=["EEEE"], capabilities=[ApproachCapability.LNAV]
        )
        assert found.capabilities[0].availability is Availability.NOT_PUBLISHED
        assert not found.unread_regions

    def test_a_region_nobody_named_is_unread(self):
        assert not register().is_read("ZZZZ")

    def test_the_view_is_not_conclusive_while_anything_is_unread(self):
        assert view_gnss(register(), regions=["AAAA"]).is_conclusive
        assert not view_gnss(register(), regions=["AAAA", "ZZZZ"]).is_conclusive

    def test_an_unread_region_is_named_in_the_render(self):
        page = view_gnss(register(), regions=["ZZZZ"]).render()
        assert "ZZZZ" in page
        assert "nothing is refused" in page

    def test_the_unanswered_are_kept_apart_from_the_refused(self):
        found = view_gnss(
            register(),
            regions=["BBBB", "ZZZZ"],
            capabilities=[ApproachCapability.LPV],
        )
        assert [f.region for f in found.unavailable] == ["BBBB"]
        assert [f.region for f in found.unanswered] == ["ZZZZ"]


# --------------------------------------------------------------------------
# Before departure
# --------------------------------------------------------------------------


class TestPrediction:
    def test_a_required_prediction_is_reported_with_its_provider(self):
        found = view_gnss(register(), regions=["AAAA"])
        note = found.predictions[0]
        assert note.is_binding
        assert "prediction page" in note.describe()

    def test_the_strongest_requirement_in_a_region_wins(self):
        """A State requiring one for RNP 4 and recommending one for RNAV 5 has
        required one. Reporting whichever was printed first would lose it."""
        held = register(
            service("FFFF", Augmentation.ABAS, raim_prediction=RaimRequirement.RECOMMENDED),
            service("FFFF", Augmentation.SBAS, raim_prediction=RaimRequirement.REQUIRED),
        )
        assert view_gnss(held, regions=["FFFF"]).predictions[0].is_binding

    def test_a_region_that_says_nothing_produces_no_note(self):
        held = register(service("GGGG", Augmentation.ABAS))
        assert view_gnss(held, regions=["GGGG"]).predictions == ()

    def test_an_unread_region_produces_no_prediction_note_either(self):
        """Its absence belongs in the unread list, not as a claim that no
        prediction is required."""
        found = view_gnss(register(), regions=["ZZZZ"])
        assert found.predictions == ()
        assert found.unread_regions == ("ZZZZ",)

    def test_the_render_says_no_prediction_was_computed(self):
        page = view_gnss(register(), regions=["AAAA"]).render()
        assert NO_PREDICTION_COMPUTED in page

    def test_nothing_in_the_module_computes_one(self):
        """A guard against the obvious future mistake."""
        source = Path("src/aeropub/gnss.py").read_text(encoding="utf-8")
        assert "almanac" in source
        for banned in ("def predict", "def compute_raim", "def raim("):
            assert banned not in source


# --------------------------------------------------------------------------
# NOTAM
# --------------------------------------------------------------------------


class TestOutages:
    def test_a_notam_against_the_region_is_found(self):
        """GNSS interference is filed against airspace, not against a box on
        a hill."""
        notams = NotamRegister(notams=(notam("A0001/26", named(GNSS, "AAAA")),))
        found = view_gnss(register(), regions=["AAAA"], notams=notams, at=NOW)
        assert len(found.outages) == 1

    def test_a_notam_against_the_constellation_is_found_too(self):
        notams = NotamRegister(notams=(notam("A0002/26", named(GNSS, "gps")),))
        found = view_gnss(register(), regions=["AAAA"], notams=notams, at=NOW)
        assert len(found.outages) == 1

    def test_a_notam_against_the_named_system_is_found(self):
        notams = NotamRegister(notams=(notam("A0003/26", named(GNSS, "EXSAT")),))
        found = view_gnss(register(), regions=["AAAA"], notams=notams, at=NOW)
        assert len(found.outages) == 1

    def test_no_notam_register_is_not_an_empty_one(self):
        assert view_gnss(register(), regions=["AAAA"]).outages == ()

    def test_a_notam_reopens_a_capability_the_state_had_published(self):
        """The same rule an aid gets: a NOTAM in force overrides the published
        value rather than being reported beside it."""
        notams = NotamRegister(notams=(notam("A0005/26", named(GNSS, "AAAA")),))
        found = view_gnss(
            register(),
            regions=["AAAA"],
            capabilities=[ApproachCapability.LPV],
            notams=notams,
            at=NOW,
        )
        finding = found.capabilities[0]
        assert finding.availability is Availability.PUBLISHED
        assert finding.is_available is None
        assert "A0005/26" in finding.describe()

    def test_a_notam_against_the_constellation_reaches_the_service_using_it(self):
        notams = NotamRegister(notams=(notam("A0006/26", named(GNSS, "gps")),))
        found = view_gnss(
            register(),
            regions=["AAAA"],
            capabilities=[ApproachCapability.LPV],
            notams=notams,
            at=NOW,
        )
        assert found.capabilities[0].is_available is None

    def test_a_notam_does_not_turn_a_refusal_into_a_maybe(self):
        """Not published and NOTAMed is still not published — softening it to
        unknown would read as an opening."""
        notams = NotamRegister(notams=(notam("A0007/26", named(GNSS, "BBBB")),))
        found = view_gnss(
            register(),
            regions=["BBBB"],
            capabilities=[ApproachCapability.LPV],
            notams=notams,
            at=NOW,
        )
        assert found.capabilities[0].is_available is False

    def test_the_reopened_are_kept_apart_from_the_unread(self):
        """Different actions: one needs somebody to read an AIP, the other to
        read a NOTAM."""
        notams = NotamRegister(notams=(notam("A0008/26", named(GNSS, "AAAA")),))
        found = view_gnss(
            register(),
            regions=["AAAA", "ZZZZ"],
            capabilities=[ApproachCapability.LPV],
            notams=notams,
            at=NOW,
        )
        assert [f.region for f in found.reopened] == ["AAAA"]
        assert {f.region for f in found.unanswered} == {"AAAA", "ZZZZ"}

    def test_the_outage_carries_the_key_it_was_found_against(self):
        notams = NotamRegister(notams=(notam("A0004/26", named(GNSS, "AAAA")),))
        found = view_gnss(register(), regions=["AAAA"], notams=notams, at=NOW)
        key, message, _force = found.outages[0]
        assert key == named(GNSS, "AAAA")
        assert message.identifier == "A0004/26"


# --------------------------------------------------------------------------
# Substitution
# --------------------------------------------------------------------------


class TestSubstitution:
    def test_it_lists_what_the_state_publishes(self):
        found = substitutions_for(register(), region="AAAA", aid="ALP")
        assert found == (ABAS_AAAA,)

    def test_an_aid_nobody_named_comes_back_empty(self):
        assert substitutions_for(register(), region="AAAA", aid="ZZZ") == ()

    def test_a_blank_aid_does_not_match_everything(self):
        assert substitutions_for(register(), region="AAAA", aid="  ") == ()

    def test_it_does_not_reach_into_another_region(self):
        assert substitutions_for(register(), region="BBBB", aid="ALP") == ()

    def test_the_docstring_refuses_to_rule(self):
        assert "Not a ruling" in substitutions_for.__doc__


# --------------------------------------------------------------------------
# The published statement itself
# --------------------------------------------------------------------------


class TestService:
    def test_a_statement_with_no_airspace_is_refused(self):
        """An approval with no region attached would be read as global."""
        with pytest.raises(ValueError, match="region"):
            GnssService(region="", augmentation=Augmentation.SBAS, source=ref())

    def test_a_bare_abas_statement_is_named_for_its_augmentation(self):
        assert service("AAAA", Augmentation.ABAS).name == "ABAS"

    def test_a_named_system_is_named_for_itself(self):
        assert SBAS_APPROVED.name == "EXSAT"

    def test_the_description_carries_the_operations_as_published(self):
        assert "RNP APCH" in SBAS_APPROVED.describe()

    def test_the_systems_list_skips_the_unnamed(self):
        assert register().systems == ("EXSAT",)

    def test_an_empty_region_matches_nothing(self):
        assert register().in_region("") == ()


# --------------------------------------------------------------------------
# Reading a manifest
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "enr43.txt"
    path.write_text(
        "an ENR 4.3 paragraph, standing in for one somebody read\n",
        encoding="utf-8",
    )
    return path


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "enr43.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "AIP ENR 4.3",
            "document_path": "enr43.txt",
            "retrieved_at": READ_AT,
        },
        "region": "AAAA",
        "covers": ["BBBB"],
        "services": [
            {
                "augmentation": "sbas",
                "system": "EXSAT",
                "constellations": ["gps"],
                "status": "approved",
                "approved_operations": ["RNP APCH", "LPV"],
                "raim_prediction": "required",
                "prediction_service": "the AAAA AIS prediction page",
                "locator": "ENR 4.3 para 2",
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_register_loads_with_every_statement_cited(self, tmp_path, document):
        held = load_gnss(write(tmp_path, manifest()))
        found = held.in_region("AAAA")[0]
        assert found.source.locator == "ENR 4.3 para 2"
        assert found.system == "EXSAT"

    def test_the_declared_regions_are_read_even_with_no_rows(self, tmp_path, document):
        held = load_gnss(write(tmp_path, manifest()))
        assert held.is_read("BBBB")
        assert held.in_region("BBBB") == ()

    def test_a_row_without_a_locator_is_refused(self, tmp_path, document):
        payload = manifest()
        del payload["services"][0]["locator"]
        with pytest.raises(ManifestError, match="locator"):
            load_gnss(write(tmp_path, payload))

    def test_an_unknown_augmentation_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["services"][0]["augmentation"] = "magic"
        with pytest.raises(ManifestError, match="augmentation"):
            load_gnss(write(tmp_path, payload))

    def test_an_unknown_capability_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["services"][0]["capabilities"] = ["ils"]
        with pytest.raises(ManifestError, match="capabilities"):
            load_gnss(write(tmp_path, payload))

    def test_a_missing_status_is_unknown_rather_than_approved(self, tmp_path, document):
        payload = manifest()
        del payload["services"][0]["status"]
        held = load_gnss(write(tmp_path, payload))
        assert held.in_region("AAAA")[0].status is ServiceStatus.UNKNOWN

    def test_a_missing_requirement_is_not_stated(self, tmp_path, document):
        payload = manifest()
        del payload["services"][0]["raim_prediction"]
        held = load_gnss(write(tmp_path, payload))
        assert (
            held.in_region("AAAA")[0].raim_prediction is RaimRequirement.NOT_STATED
        )

    def test_covers_must_be_a_list(self, tmp_path, document):
        with pytest.raises(ManifestError, match="covers"):
            load_gnss(write(tmp_path, manifest(covers="BBBB")))

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(gnss_template())
        assert blank["covers"] == []
        assert blank["services"][0]["raim_prediction"] == "not_stated"
