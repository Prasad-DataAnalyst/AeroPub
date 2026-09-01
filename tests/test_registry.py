"""Tests for the source registry and status board.

As in the other suites, constructing objects to exercise logic is not the same
as fabricating source data. No aeronautical values appear here, and the URLs are
placeholders in a test fixture rather than claims about where a State publishes.
"""

from datetime import datetime, timedelta, timezone

import pytest

from aeropub.registry import (
    CheckOutcome,
    CredentialRef,
    CredentialStatus,
    DetectionTier,
    Freshness,
    Redistribution,
    Source,
    SourceFormat,
    SourceKind,
    SourceRegistry,
    SourceState,
    mask_secret,
    render_board,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def source(**overrides) -> Source:
    fields = dict(
        source_id="us-notam",
        authority="US",
        name="FAA NOTAM API",
        kind=SourceKind.NOTAM,
        url="https://example.invalid/notams",
        fmt=SourceFormat.REST_API,
        tier=DetectionTier.FAST_POLL,
    )
    fields.update(overrides)
    return Source(**fields)


def check(state=SourceState.WATCHING, at=NOW, **kw) -> CheckOutcome:
    return CheckOutcome(source_id=kw.pop("source_id", "us-notam"),
                        checked_at=at, state=state, **kw)


class TestSecretHandling:
    """The registry must never be able to leak a key."""

    def test_long_secrets_reveal_only_the_tail(self):
        assert mask_secret("abcdefghijklmnop") == "****mnop"

    def test_short_secrets_are_masked_entirely(self):
        # Four characters of an eight-character key is a real disclosure.
        assert mask_secret("abc12345") == "****"

    def test_the_reference_holds_no_secret(self):
        ref = CredentialRef(env_var="FAA_KEY", label="FAA").with_hint_from("abcdefghijklmnop")
        rendered = repr(ref)
        assert "abcdefghijklmnop" not in rendered
        assert "mnop" in rendered  # the hint survives, the key does not

    def test_resolve_reads_the_environment_at_point_of_use(self):
        ref = CredentialRef(env_var="FAA_KEY", label="FAA")
        assert ref.resolve({"FAA_KEY": "secret-value"}) == "secret-value"
        assert ref.resolve({}) is None

    def test_blank_environment_values_count_as_absent(self):
        ref = CredentialRef(env_var="FAA_KEY", label="FAA")
        assert not ref.is_present({"FAA_KEY": "   "})


class TestCredentialStatus:
    def test_missing_when_not_in_the_environment(self):
        ref = CredentialRef(env_var="FAA_KEY", label="FAA")
        assert ref.status({}) is CredentialStatus.MISSING

    def test_unverified_when_present_but_never_used(self):
        ref = CredentialRef(env_var="FAA_KEY", label="FAA")
        assert ref.status({"FAA_KEY": "k"}) is CredentialStatus.UNVERIFIED

    def test_configured_once_verified(self):
        ref = CredentialRef(env_var="FAA_KEY", label="FAA").verified(NOW)
        assert ref.status({"FAA_KEY": "k"}, now=NOW) is CredentialStatus.CONFIGURED

    def test_invalid_when_the_authority_rejected_it(self):
        ref = CredentialRef(env_var="FAA_KEY", label="FAA").verified(NOW)
        assert ref.status({"FAA_KEY": "k"}, rejected=True) is CredentialStatus.INVALID

    def test_expired_takes_effect_on_the_expiry_moment(self):
        ref = CredentialRef(env_var="FAA_KEY", label="FAA", expires_at=NOW).verified(NOW)
        assert ref.status({"FAA_KEY": "k"}, now=NOW) is CredentialStatus.EXPIRED
        earlier = NOW - timedelta(seconds=1)
        assert ref.status({"FAA_KEY": "k"}, now=earlier) is CredentialStatus.CONFIGURED


class TestSourceValidation:
    @pytest.mark.parametrize("field", ["source_id", "authority", "name", "url"])
    def test_identifying_fields_cannot_be_blank(self, field):
        with pytest.raises(ValueError, match=field):
            source(**{field: "  "})

    def test_url_must_be_http(self):
        with pytest.raises(ValueError, match="http"):
            source(url="ftp://example.invalid/aip")

    def test_interval_must_be_positive(self):
        with pytest.raises(ValueError, match="positive"):
            source(interval=timedelta(0))

    def test_tier_supplies_a_default_interval(self):
        assert source(tier=DetectionTier.PUSH).check_interval == timedelta(minutes=1)
        assert source(tier=DetectionTier.SCHEDULED).check_interval == timedelta(hours=6)

    def test_explicit_interval_overrides_the_tier(self):
        s = source(tier=DetectionTier.SCHEDULED, interval=timedelta(minutes=2))
        assert s.check_interval == timedelta(minutes=2)


class TestUrlChanges:
    """States move their eAIP. Where it used to live gets asked about later."""

    def test_moving_records_the_old_address(self):
        original = source(url="https://old.invalid/eaip")
        moved = original.moved_to("https://new.invalid/eaip", note="State reorganised site", at=NOW)
        assert moved.url == "https://new.invalid/eaip"
        assert len(moved.url_history) == 1
        assert moved.url_history[0].old_url == "https://old.invalid/eaip"
        assert moved.url_history[0].note == "State reorganised site"

    def test_moving_to_the_same_url_is_a_no_op(self):
        original = source()
        assert original.moved_to(original.url) is original

    def test_history_accumulates_across_moves(self):
        s = source(url="https://a.invalid").moved_to("https://b.invalid").moved_to("https://c.invalid")
        assert [c.old_url for c in s.url_history] == ["https://a.invalid", "https://b.invalid"]

    def test_registry_move_keeps_check_history(self):
        registry = SourceRegistry([source()])
        registry.record_check(check())
        registry.move("us-notam", "https://new.invalid/notams")
        assert registry.get("us-notam").url == "https://new.invalid/notams"
        assert len(registry.checks("us-notam")) == 1


class TestFreshness:
    """The operator's real question: is the system actually checking?"""

    def test_never_checked_is_its_own_state(self):
        registry = SourceRegistry([source()])
        row = registry.status("us-notam", now=NOW)
        assert row.freshness is Freshness.NEVER_CHECKED
        assert row.last_checked_at is None

    def test_on_time_within_the_interval(self):
        registry = SourceRegistry([source(tier=DetectionTier.FAST_POLL)])
        registry.record_check(check(at=NOW))
        row = registry.status("us-notam", now=NOW + timedelta(minutes=3))
        assert row.freshness is Freshness.ON_TIME

    def test_late_once_past_due(self):
        registry = SourceRegistry([source(tier=DetectionTier.FAST_POLL)])
        registry.record_check(check(at=NOW))
        row = registry.status("us-notam", now=NOW + timedelta(minutes=12))
        assert row.freshness is Freshness.LATE

    def test_stale_beyond_three_intervals(self):
        registry = SourceRegistry([source(tier=DetectionTier.FAST_POLL)])
        registry.record_check(check(at=NOW))
        row = registry.status("us-notam", now=NOW + timedelta(hours=2))
        assert row.freshness is Freshness.STALE
        assert row.state is SourceState.STALE
        assert row.needs_attention

    def test_next_due_is_derived_from_the_interval(self):
        registry = SourceRegistry([source(tier=DetectionTier.FAST_POLL)])
        registry.record_check(check(at=NOW))
        assert registry.status("us-notam", now=NOW).next_due_at == NOW + timedelta(minutes=5)


class TestDerivedState:
    def test_a_missing_credential_outranks_everything(self):
        # It explains the staleness, so it is what the operator must act on.
        cred = CredentialRef(env_var="FAA_KEY", label="FAA")
        registry = SourceRegistry([source(credential=cred)])
        registry.record_check(check(at=NOW - timedelta(days=1)))
        row = registry.status("us-notam", now=NOW, environ={})
        assert row.state is SourceState.CREDENTIAL_MISSING
        assert row.needs_attention

    def test_disabled_sources_report_as_disabled(self):
        registry = SourceRegistry([source(enabled=False)])
        assert registry.status("us-notam", now=NOW).state is SourceState.DISABLED

    def test_an_exception_from_the_last_check_persists(self):
        registry = SourceRegistry([source()])
        registry.record_check(check(state=SourceState.BLOCKED, error="429 rate limited"))
        row = registry.status("us-notam", now=NOW)
        assert row.state is SourceState.BLOCKED
        assert row.last_error == "429 rate limited"
        assert row.needs_attention

    def test_healthy_sources_do_not_need_attention(self):
        registry = SourceRegistry([source()])
        registry.record_check(check(state=SourceState.PUBLISHED, at=NOW))
        assert not registry.status("us-notam", now=NOW).needs_attention


class TestFailureCounting:
    def test_counts_only_the_current_run_of_failures(self):
        registry = SourceRegistry([source()])
        registry.record_check(check(state=SourceState.FETCH_FAILED, at=NOW - timedelta(minutes=30)))
        registry.record_check(check(state=SourceState.PUBLISHED, at=NOW - timedelta(minutes=20)))
        registry.record_check(check(state=SourceState.FETCH_FAILED, at=NOW - timedelta(minutes=10)))
        registry.record_check(check(state=SourceState.FETCH_FAILED, at=NOW))
        assert registry.status("us-notam", now=NOW).consecutive_failures == 2

    def test_three_consecutive_failures_needs_attention(self):
        registry = SourceRegistry([source()])
        for n in range(3):
            registry.record_check(check(state=SourceState.FETCH_FAILED, at=NOW - timedelta(minutes=n)))
        assert registry.status("us-notam", now=NOW).needs_attention


class TestChangeTracking:
    def test_last_change_reflects_the_most_recent_actual_change(self):
        registry = SourceRegistry([source()])
        registry.record_check(check(at=NOW - timedelta(hours=3), changed=True))
        registry.record_check(check(at=NOW - timedelta(hours=1), changed=False))
        assert registry.status("us-notam", now=NOW).last_change_at == NOW - timedelta(hours=3)

    def test_no_change_yet_reads_as_none(self):
        registry = SourceRegistry([source()])
        registry.record_check(check(at=NOW))
        assert registry.status("us-notam", now=NOW).last_change_at is None


class TestRegistry:
    def test_duplicate_ids_are_rejected(self):
        registry = SourceRegistry([source()])
        with pytest.raises(ValueError, match="already registered"):
            registry.add(source())

    def test_unknown_source_raises_clearly(self):
        with pytest.raises(KeyError, match="no source registered"):
            SourceRegistry().get("nope")

    def test_check_for_unknown_source_is_rejected(self):
        with pytest.raises(KeyError, match="no source registered"):
            SourceRegistry().record_check(check())

    def test_credential_can_be_attached_after_registration(self):
        # The workflow when a key finally arrives.
        registry = SourceRegistry([source()])
        assert registry.status("us-notam", now=NOW).credential_status is None
        cred = CredentialRef(env_var="FAA_KEY", label="FAA NOTAM API")
        registry.set_credential("us-notam", cred)
        row = registry.status("us-notam", now=NOW, environ={"FAA_KEY": "k"})
        assert row.credential_status is CredentialStatus.UNVERIFIED

    def test_enabling_and_disabling(self):
        registry = SourceRegistry([source()])
        registry.set_enabled("us-notam", False)
        assert not registry.get("us-notam").enabled
        registry.set_enabled("us-notam", True)
        assert registry.get("us-notam").enabled

    def test_membership_and_iteration(self):
        registry = SourceRegistry([source(), source(source_id="qa-aip", authority="QA")])
        assert len(registry) == 2
        assert "us-notam" in registry
        assert {s.source_id for s in registry} == {"us-notam", "qa-aip"}


class TestBoard:
    def test_problems_sort_to_the_top(self):
        healthy = source(source_id="a-ok", authority="AA", name="Healthy")
        broken = source(source_id="z-bad", authority="ZZ", name="Broken")
        registry = SourceRegistry([healthy, broken])
        registry.record_check(check(source_id="a-ok", state=SourceState.PUBLISHED, at=NOW))
        registry.record_check(check(source_id="z-bad", state=SourceState.BLOCKED, at=NOW))
        rows = registry.board(now=NOW)
        assert rows[0].source.source_id == "z-bad"

    def test_render_shows_what_an_operator_needs(self):
        cred = CredentialRef(env_var="FAA_KEY", label="FAA NOTAM API")
        registry = SourceRegistry([source(credential=cred)])
        registry.record_check(check(state=SourceState.FETCH_FAILED, at=NOW, error="connection reset"))
        text = render_board(registry.board(now=NOW, environ={}), now=NOW)
        assert "FAA NOTAM API" in text
        assert "credential_missing" in text
        assert "connection reset" in text
        assert "1 needing attention" in text

    def test_empty_registry_renders_without_error(self):
        assert "0 sources" in render_board(SourceRegistry().board(now=NOW), now=NOW)


class TestRedistribution:
    def test_defaults_to_unknown_rather_than_permitted(self):
        # Assuming permission is the expensive mistake; assume nothing instead.
        assert source().redistribution is Redistribution.UNKNOWN

    def test_can_be_recorded_per_source(self):
        assert source(redistribution=Redistribution.PROHIBITED).redistribution is Redistribution.PROHIBITED
