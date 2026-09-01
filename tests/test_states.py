"""Tests for per-State profiles.

The Qatar profile is real configuration — the addresses came from the operator
who works that State. What it deliberately does not contain is invented
structure, and these tests hold that line: nothing may claim to be verified
that has not been reached, and nothing may be declared absent that has not been
checked.
"""

import pytest

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


class TestQatar:
    def test_is_registered_and_retrievable(self):
        assert get_profile("OT") is qatar.PROFILE
        assert get_profile("ot") is qatar.PROFILE

    def test_carries_the_addresses_the_operator_supplied(self):
        urls = {s.url for s in qatar.PROFILE.sources}
        assert qatar.AIM_URL in urls
        assert qatar.DATASETS_URL in urls

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
