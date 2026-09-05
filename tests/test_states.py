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
from aeropub.states import qatar, saudi_arabia


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


#: A current Qatar edition root, given first-hand by an operator who uses the
#: site on 05 SEP 2026. Not fetched — the egress policy refuses the host — but
#: it is a real address on the authority's own domain, and it is a *different*
#: layout from every 2018-2025 observation above.
CURRENT_EDITION = (
    "https://aim.gov.qa/AIP/03-SEP-2026/AIP-30/2026-10-01-000000"
    "/html/index-en-GB.html"
)


class TestQatarCurrentLayout:
    """Qatar changed its scheme. A builder that only knew the old one would
    404 on every current edition."""

    def test_the_builder_reproduces_the_observed_edition_exactly(self):
        c = AiracCycle.from_identifier("2610")
        assert qatar.edition_index_url(c, 30) == CURRENT_EDITION

    def test_the_effective_date_in_the_path_is_a_real_airac_date(self):
        # Independent cross-check: the calendar was not consulted to build the
        # URL, and it agrees with the State's own path.
        day = date.fromisoformat("2026-10-01")
        assert cycle_for(day).effective_date == day

    def test_the_publication_date_is_the_recipient_deadline(self):
        """T-28, which this repository computes from the calendar alone. One
        observation and one independent derivation agreeing."""
        c = AiracCycle.from_identifier("2610")
        assert qatar.publication_date(c) == date(2026, 9, 3)
        assert qatar.publication_date(c) == c.recipient_deadline

    def test_the_month_is_upper_case_in_the_path(self):
        c = AiracCycle.from_identifier("2610")
        assert "03-SEP-2026" in qatar.edition_base(c, 30)

    def test_the_amendment_number_is_not_derivable_from_the_cycle(self):
        """The finding that matters for automation: a current edition cannot be
        addressed from the cycle alone, which is why the history page is the
        entry point rather than a convenience."""
        c = AiracCycle.from_identifier("2610")
        assert qatar.edition_base(c, 30) != qatar.edition_base(c, 31)

    def test_an_amendment_number_that_is_not_a_count_is_refused(self):
        c = AiracCycle.from_identifier("2610")
        with pytest.raises(ValueError, match="running number"):
            qatar.edition_base(c, 0)

    def test_a_section_follows_the_eurocontrol_convention(self):
        c = AiracCycle.from_identifier("2610")
        built = qatar.edition_section_url(c, 30, "ENR-3.1")
        assert built.startswith(qatar.edition_base(c, 30))
        assert built.endswith("/html/eAIP/ENR-3.1-en-GB.html")

    def test_the_legacy_builder_still_reproduces_the_old_observations(self):
        """A State that changed its scheme once may still serve old editions
        at old addresses."""
        c = AiracCycle.from_identifier("2201")
        assert qatar.eaip_section_url(c, "GEN-0.1") == OBSERVED_URLS["2201-html"]

    def test_the_two_layouts_are_not_the_same_address(self):
        c = AiracCycle.from_identifier("2610")
        assert qatar.eaip_base(c) not in qatar.edition_base(c, 30)

    def test_the_history_page_is_registered_as_a_source(self):
        assert qatar.HISTORY_URL in {s.url for s in qatar.PROFILE.sources}

    def test_the_history_page_is_unverified_like_everything_else(self):
        found = next(
            s for s in qatar.PROFILE.sources if s.url == qatar.HISTORY_URL
        )
        assert not found.is_verified


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


SAUDI_OBSERVED = {
    # Live Saudi eAIP addresses seen in a public search index on 01 SEP 2026.
    # Not fetched — the host is blocked from the build environment — but real
    # addresses on the authority's own domain. The builders must reproduce them
    # exactly, or the structure decoding is wrong.
    "aic-2025-05": "https://aimss.sans.com.sa/assets/FileManagerFiles/AIC/OE-eAIC-2025-05-en-SA.pdf",
    "aic-2024-08": "https://aimss.sans.com.sa/assets/FileManagerFiles/AIC/OE-eAIC-2024-08-en-SA.pdf",
    "amdt-2405": (
        "https://aimss.sans.com.sa/assets/FileManagerFiles/"
        "AIRAC%20AIP%20AMDT%2005_24_2024_05_16/eAIC/03-2019_2019_03_28/OE-AIC-en-GB.html"
    ),
    "amdt-2511": (
        "https://aimss.sans.com.sa/assets/FileManagerFiles/"
        "AIRAC%20AIP%20AMDT%2011_25_2025_10_30/eAIC/07-2024_2024_10_29/OE-AIC-en-GB.html"
    ),
    "amdt-2601": (
        "https://aimss.sans.com.sa/assets/FileManagerFiles/"
        "AIRAC%20AIP%20AMDT%2001_26_2026_01_22/eAIC/05-2025_2025_10_30/OE-AIC-en-GB.html"
    ),
}


class TestSaudiUrlStructure:
    """The builders must reproduce real observed addresses, exactly."""

    def test_standalone_circular_pdf(self):
        assert saudi_arabia.standalone_circular_url(2025, 5) == SAUDI_OBSERVED["aic-2025-05"]
        assert saudi_arabia.standalone_circular_url(2024, 8) == SAUDI_OBSERVED["aic-2024-08"]

    @pytest.mark.parametrize(
        "edition,number,year,issued,key",
        [
            ("2405", 3, 2019, date(2019, 3, 28), "amdt-2405"),
            ("2511", 7, 2024, date(2024, 10, 29), "amdt-2511"),
            ("2601", 5, 2025, date(2025, 10, 30), "amdt-2601"),
        ],
    )
    def test_circular_inside_an_amendment_edition(self, edition, number, year, issued, key):
        url = saudi_arabia.circular_url(
            AiracCycle.from_identifier(edition), number, year, issued
        )
        assert url == SAUDI_OBSERVED[key]

    def test_a_circular_date_is_not_always_an_airac_date(self):
        # 29 October 2024 sits inside edition 2411, which became effective on
        # the 31st. Typing the circular date as a cycle would have produced
        # confidently wrong URLs that 404 in silence.
        assert cycle_for(date(2024, 10, 29)).effective_date != date(2024, 10, 29)
        assert AiracCycle.from_identifier("2411").effective_date == date(2024, 10, 31)

    @pytest.mark.parametrize(
        "identifier,expected",
        [
            ("2405", "AIRAC AIP AMDT 05_24_2024_05_16"),
            ("2511", "AIRAC AIP AMDT 11_25_2025_10_30"),
            ("2601", "AIRAC AIP AMDT 01_26_2026_01_22"),
        ],
    )
    def test_amendment_directory_name(self, identifier, expected):
        assert saudi_arabia.amendment_name(AiracCycle.from_identifier(identifier)) == expected

    def test_the_amendment_number_is_the_cycle_ordinal(self):
        # Decoded, not assumed: three editions spanning 2024 to 2026 agree.
        for identifier in ("2405", "2511", "2601"):
            cycle = AiracCycle.from_identifier(identifier)
            name = saudi_arabia.amendment_name(cycle)
            assert name.split()[3].startswith(f"{cycle.ordinal:02d}_")

    def test_spaces_in_the_directory_name_are_encoded(self):
        url = saudi_arabia.amendment_base(AiracCycle.from_identifier("2601"))
        assert " " not in url
        assert "%20" in url

    @pytest.mark.parametrize(
        "identifier,stamp",
        [("2405", date(2024, 5, 16)), ("2511", date(2025, 10, 30)), ("2601", date(2026, 1, 22))],
    )
    def test_every_edition_date_is_a_real_airac_date(self, identifier, stamp):
        # Independent cross-check of the calendar against a second State's own
        # published paths. Edition dates are regular; circular dates are not.
        assert cycle_for(stamp).effective_date == stamp
        assert AiracCycle.from_identifier(identifier).effective_date == stamp


class TestSaudiProfile:
    def test_is_registered_under_its_location_indicator_prefix(self):
        assert get_profile("OE") is saudi_arabia.PROFILE
        assert saudi_arabia.PROFILE.code == "OE"

    def test_nothing_claims_to_be_verified(self):
        assert saudi_arabia.PROFILE.unverified_sources() == saudi_arabia.PROFILE.sources

    def test_nothing_is_declared_absent_without_checking(self):
        assert saudi_arabia.PROFILE.absent == frozenset()

    def test_submission_cutoff_is_distinct_from_the_icao_distribution_deadline(self):
        # 70/84 days is an internal originator deadline; ICAO's 42/56 governs
        # AIS to recipients. Different stages, and conflating them misreads both.
        from aeropub.airac import DISTRIBUTION_LEAD_DAYS, MAJOR_CHANGE_LEAD_DAYS
        assert saudi_arabia.SUBMISSION_CUTOFF_DAYS > DISTRIBUTION_LEAD_DAYS
        assert saudi_arabia.MAJOR_SUBMISSION_CUTOFF_DAYS > MAJOR_CHANGE_LEAD_DAYS


class TestStatesDifferFromEachOther:
    def test_two_gulf_states_address_editions_differently(self):
        # Same region, same ICAO framework, different URL grammar — which is why
        # each State gets a module rather than one shared guess.
        cycle = AiracCycle.from_identifier("2601")
        qa = qatar.eaip_base(cycle)
        sa = saudi_arabia.amendment_base(cycle)
        assert cycle.effective_date.isoformat() in qa
        assert cycle.effective_date.isoformat() not in sa
        assert f"{cycle.ordinal:02d}_" in sa

    def test_both_are_registered(self):
        assert {"OT", "OE"} <= set(profiles())
