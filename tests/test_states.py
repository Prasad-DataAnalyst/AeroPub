"""Tests for per-State profiles.

The Qatar profile is real configuration — the addresses came from the operator
who works that State. What it deliberately does not contain is invented
structure, and these tests hold that line: nothing may claim to be verified
that has not been reached, and nothing may be declared absent that has not been
checked.
"""

import pytest

from datetime import date

from aeropub.airac import AiracCycle, cycle_for
from aeropub.registry import DetectionTier, Source, SourceFormat, SourceKind
from aeropub.states import StateProfile, get_profile, profiles
from aeropub.states import qatar


def a_source(source_id="x", kind=SourceKind.AIP, **kw) -> Source:
    return Source(
        source_id=source_id,
        authority="XX",
        name="Example",
        kind=kind,
        url="https://example.invalid/aip",
        fmt=SourceFormat.EAIP_HTML,
        tier=DetectionTier.ADAPTIVE_POLL,
        **kw,
    )


class TestProfileValidation:
    def test_code_and_name_are_required(self):
        with pytest.raises(ValueError, match="code"):
            StateProfile(code="  ", name="Nowhere", authority="A", aim_url="https://x.invalid")
        with pytest.raises(ValueError, match="name"):
            StateProfile(code="XX", name=" ", authority="A", aim_url="https://x.invalid")

    def test_duplicate_source_ids_are_rejected(self):
        with pytest.raises(ValueError, match="duplicate source ids"):
            StateProfile(
                code="XX", name="Nowhere", authority="A", aim_url="https://x.invalid",
                sources=(a_source("dup"), a_source("dup", kind=SourceKind.NOTAM)),
            )

    def test_a_kind_cannot_be_both_present_and_absent(self):
        # Contradictory coverage claims are worse than admitted ignorance.
        with pytest.raises(ValueError, match="both present and absent"):
            StateProfile(
                code="XX", name="Nowhere", authority="A", aim_url="https://x.invalid",
                sources=(a_source(kind=SourceKind.AIP),),
                absent=frozenset({SourceKind.AIP}),
            )


class TestCoverageDistinctions:
    """Absent, unknown and present must never look alike."""

    def test_unknown_kinds_exclude_both_registered_and_absent(self):
        profile = StateProfile(
            code="XX", name="Nowhere", authority="A", aim_url="https://x.invalid",
            sources=(a_source(kind=SourceKind.AIP),),
            absent=frozenset({SourceKind.AIC_INDEX}),
        )
        unknown = profile.unknown_kinds()
        assert SourceKind.AIP not in unknown        # registered
        assert SourceKind.AIC_INDEX not in unknown  # declared absent
        assert SourceKind.NOTAM in unknown          # genuinely not looked at

    def test_source_kinds_reports_what_is_registered(self):
        profile = StateProfile(
            code="XX", name="Nowhere", authority="A", aim_url="https://x.invalid",
            sources=(a_source("a", SourceKind.AIP), a_source("b", SourceKind.NOTAM)),
        )
        assert profile.source_kinds() == frozenset({SourceKind.AIP, SourceKind.NOTAM})


class TestVerification:
    def test_a_registered_source_starts_unverified(self):
        assert not a_source().is_verified

    def test_verifying_records_the_moment(self):
        from datetime import datetime, timezone
        when = datetime(2026, 9, 1, tzinfo=timezone.utc)
        assert a_source().verified(when).verified_at == when

    def test_profile_lists_what_still_needs_checking(self):
        from datetime import datetime, timezone
        when = datetime(2026, 9, 1, tzinfo=timezone.utc)
        profile = StateProfile(
            code="XX", name="Nowhere", authority="A", aim_url="https://x.invalid",
            sources=(a_source("checked").verified(when), a_source("unchecked")),
        )
        assert [s.source_id for s in profile.unverified_sources()] == ["unchecked"]


OBSERVED_URLS = {
    # Live Qatar eAIP addresses seen in a public search index on 01 SEP 2026.
    # Not fetched — the host is blocked from the build environment — but these
    # are real addresses on the authority's own domain, and the URL builders
    # must reproduce them exactly or the structure inference is wrong.
    "1801-pdf": "https://www.aim.gov.qa/eaip/2018-01-04-AIRAC/pdf/GEN-0.1.pdf",
    "2201-html": "https://www.aim.gov.qa/eaip/2022-01-27-AIRAC/html/eAIP/GEN-0.1-en-GB.html",
    "2210-html": "https://www.aim.gov.qa/eaip/2022-10-06-AIRAC/html/eAIP/GEN-3.1-en-GB.html",
    "2505-aic": "https://www.aim.gov.qa/eaip/2025-05-15-AIRAC/html/eAIC/eAIC-2025-03-A-en-GB.html",
    "2510-aic": "https://www.aim.gov.qa/eaip/2025-10-02-AIRAC/html/eAIC/eAIC-2025-07-A-en-GB.html",
}


class TestQatarUrlStructure:
    """The builders must reproduce real observed addresses, exactly."""

    def test_section_html_url(self):
        c = AiracCycle.from_identifier("2201")
        assert qatar.eaip_section_url(c, "GEN-0.1") == OBSERVED_URLS["2201-html"]

    def test_section_html_url_other_cycle(self):
        c = AiracCycle.from_identifier("2210")
        assert qatar.eaip_section_url(c, "GEN-3.1") == OBSERVED_URLS["2210-html"]

    def test_section_pdf_url(self):
        c = AiracCycle.from_identifier("1801")
        assert qatar.eaip_pdf_url(c, "GEN-0.1") == OBSERVED_URLS["1801-pdf"]

    def test_circular_url(self):
        c = AiracCycle.from_identifier("2505")
        assert qatar.eaic_url(c, 2025, 3) == OBSERVED_URLS["2505-aic"]

    def test_circular_number_is_zero_padded(self):
        c = AiracCycle.from_identifier("2510")
        assert qatar.eaic_url(c, 2025, 7) == OBSERVED_URLS["2510-aic"]

    def test_edition_is_addressed_by_airac_effective_date(self):
        # The path carries the effective date, not the cycle identifier, so the
        # AIRAC calendar is what addresses the publication.
        c = AiracCycle.from_identifier("2201")
        assert c.effective_date.isoformat() in qatar.eaip_base(c)
        assert c.identifier not in qatar.eaip_base(c)

    @pytest.mark.parametrize("url", sorted(OBSERVED_URLS.values()))
    def test_every_observed_date_is_a_real_airac_date(self, url):
        # Independent cross-check of the AIRAC calendar against a State's own
        # published paths, spanning 2018 to 2025.
        stamp = url.split("/eaip/")[1].split("-AIRAC")[0]
        day = date.fromisoformat(stamp)
        assert cycle_for(day).effective_date == day


class TestQatar:
    def test_is_registered_and_retrievable(self):
        assert get_profile("OT") is qatar.PROFILE
        assert get_profile("ot") is qatar.PROFILE

    def test_carries_the_dataset_catalogue_the_operator_supplied(self):
        assert qatar.DATASETS_URL in {s.url for s in qatar.PROFILE.sources}

    def test_sources_for_a_cycle_address_that_edition(self):
        c = AiracCycle.from_identifier("2601")
        built = qatar.sources_for(c)
        assert all(qatar.eaip_base(c) in s.url for s in built)
        assert all(not s.is_verified for s in built)

    def test_nothing_claims_to_be_verified(self):
        # The host was unreachable from the build environment. Until a capture
        # proves otherwise, every Qatar source must read as unverified.
        assert qatar.PROFILE.unverified_sources() == qatar.PROFILE.sources
        assert qatar.PROFILE.verified_at is None

    def test_nothing_is_declared_absent_without_checking(self):
        # Saying "Qatar publishes no supplement index" without looking would be
        # a false statement about the State, not a cautious one.
        assert qatar.PROFILE.absent == frozenset()

    def test_the_unchecked_kinds_are_visible_as_unknown(self):
        unknown = qatar.PROFILE.unknown_kinds()
        assert SourceKind.NOTAM in unknown
        assert SourceKind.AMDT_INDEX in unknown
        assert SourceKind.SUP_INDEX in unknown


class TestRegistryOfProfiles:
    def test_profiles_are_keyed_by_location_indicator_prefix(self):
        assert "OT" in profiles()

    def test_unknown_state_error_names_what_is_implemented(self):
        with pytest.raises(KeyError, match="implemented"):
            get_profile("ZZ")
