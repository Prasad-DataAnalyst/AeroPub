"""The review gate — who is accountable before a verdict reaches a crew.

The plan is direct about why this exists: a system with no attestation is
buildable and not sellable, because a regulator asks who is accountable for the
data feeding an operational decision and "the system decided" ends the
conversation. That framing changes what the module must do. It is not enough
for a person to click; it must be provable years later that they clicked on
*this*, and reproducible why one finding was audited and another was not.

So the tests below are mostly about binding and reproducibility rather than
about routing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aeropub.aircraft import PavementRating
from aeropub.gate import (
    DEFAULT_AUTO_PUBLISH,
    Attestation,
    Disposition,
    GateLog,
    ReviewGate,
    StaleAttestation,
    decide,
    fingerprint,
    review,
)
from aeropub.operator import Exposure, ExposureFinding, Role
from aeropub.provenance import SourceRef
from aeropub.suitability import Assessment, Check

NOW = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)


def ref() -> SourceRef:
    return SourceRef(
        source_id="TEST", document="AIP AD 2", locator="AD 2.6",
        retrieved_at=NOW, content_hash="a" * 64,
        parser_id="test", parser_version="1",
    )


def finding(
    *,
    exposure=Exposure.CRITICAL,
    designator="WIDE",
    name="Rescue and fire fighting",
    detail="the aeroplane requires Category 9; the aerodrome publishes 7",
    reason="the aerodrome cannot take this type as a destination.",
    role=Role.DESTINATION,
    sole=False,
) -> ExposureFinding:
    return ExposureFinding(
        designator=designator,
        check=Check(
            name=name,
            assessment=Assessment.NOT_SUITABLE,
            detail=detail,
            section="AD 2.6",
        ),
        exposure=exposure,
        reason=reason,
        role=role,
        sole_suitable=sole,
    )


def gate(**overrides) -> ReviewGate:
    fields = dict(tenant="Example Airways")
    fields.update(overrides)
    return ReviewGate(**fields)


# --------------------------------------------------------------------------
# An attestation binds to what was attested
# --------------------------------------------------------------------------


class TestTheSignatureCoversTheContent:
    def test_an_attestation_releases_the_finding_it_covers(self):
        one = finding()
        signed = Attestation(by="J. Morgan", at=NOW, finding=fingerprint(one))
        released = decide(gate(), one, attestation=signed, at=NOW)
        assert released.disposition is Disposition.ATTESTED
        assert released.is_released
        assert "J. Morgan" in released.reason

    def test_a_changed_finding_breaks_the_attestation(self):
        # The whole point of provability. Signing for one wording must not
        # silently release a different one.
        signed = Attestation(by="J. Morgan", at=NOW, finding=fingerprint(finding()))
        moved = finding(detail="the aerodrome publishes Category 5")
        with pytest.raises(StaleAttestation) as caught:
            decide(gate(), moved, attestation=signed)
        assert "has changed since it was signed" in str(caught.value)

    def test_a_stale_attestation_raises_rather_than_falling_back_to_held(self):
        # Silently declining to apply it would look identical to never having
        # one, and the reviewer would not learn their signature had lapsed.
        signed = Attestation(by="J. Morgan", at=NOW, finding=fingerprint(finding()))
        with pytest.raises(StaleAttestation):
            decide(gate(), finding(reason="a different reason"), attestation=signed)

    def test_the_fingerprint_ignores_when_the_run_happened(self):
        # Re-assessing an unchanged aerodrome must not invalidate a signature.
        assert fingerprint(finding()) == fingerprint(finding())

    def test_the_fingerprint_covers_the_verdict(self):
        assert fingerprint(finding()) != fingerprint(finding(exposure=Exposure.LOW))

    def test_it_covers_the_role_because_the_same_check_means_different_things(self):
        assert fingerprint(finding(role=Role.DESTINATION)) != fingerprint(
            finding(role=Role.EDTO_ALTERNATE)
        )

    def test_it_covers_sole_suitability(self):
        assert fingerprint(finding(sole=False)) != fingerprint(finding(sole=True))

    def test_an_attestation_needs_a_person(self):
        with pytest.raises(ValueError) as caught:
            Attestation(by="  ", at=NOW, finding="a" * 64)
        assert "the entire point of it" in str(caught.value)

    def test_an_attestation_needs_a_fingerprint(self):
        with pytest.raises(ValueError) as caught:
            Attestation(by="J. Morgan", at=NOW, finding="short")
        assert "the signature covers nothing" in str(caught.value)

    def test_an_attestation_must_be_timestamped_in_utc(self):
        with pytest.raises(ValueError):
            Attestation(by="J. Morgan", at=datetime(2026, 9, 4, 8, 0),
                        finding="a" * 64)

    def test_declining_is_a_decision_and_is_recorded_as_one(self):
        one = finding()
        declined = Attestation(by="J. Morgan", at=NOW, finding=fingerprint(one),
                               released=False, note="chart says otherwise")
        released = decide(gate(), one, attestation=declined, at=NOW)
        assert released.disposition is Disposition.WITHHELD
        assert not released.is_released
        assert "not released" in released.reason


# --------------------------------------------------------------------------
# Sampling has to be answerable
# --------------------------------------------------------------------------


class TestSamplingIsDeterministic:
    def test_the_same_finding_is_always_in_or_always_out(self):
        # An auditor asking "why this one and not that one" needs an answer,
        # and "chance" is not one.
        sampling = gate(sample_rate=0.5)
        mark = fingerprint(finding(exposure=Exposure.LOW))
        assert sampling.samples(mark) == sampling.samples(mark)

    def test_it_reproduces_from_the_finding_alone_years_later(self):
        sampling = gate(sample_rate=0.5)
        one = finding(exposure=Exposure.LOW)
        first = decide(sampling, one, at=NOW).disposition
        later = decide(sampling, one, at=NOW + timedelta(days=900)).disposition
        assert first is later

    def test_a_zero_rate_samples_nothing(self):
        never = gate(sample_rate=0.0)
        assert not never.samples(fingerprint(finding(exposure=Exposure.LOW)))

    def test_a_full_rate_samples_everything(self):
        always = gate(sample_rate=1.0)
        assert always.samples(fingerprint(finding(exposure=Exposure.LOW)))

    def test_a_sampled_finding_is_still_released(self):
        # Sampling is a check on the system, not a hold on the finding.
        released = decide(gate(sample_rate=1.0), finding(exposure=Exposure.LOW), at=NOW)
        assert released.disposition is Disposition.SAMPLED
        assert released.is_released
        assert not released.needs_a_person

    def test_the_rate_is_a_share(self):
        with pytest.raises(ValueError):
            gate(sample_rate=1.5)
        with pytest.raises(ValueError):
            gate(sample_rate=-0.1)

    def test_the_rate_is_roughly_honoured_across_many_findings(self):
        sampling = gate(sample_rate=0.2)
        drawn = sum(
            1
            for n in range(400)
            if sampling.samples(fingerprint(finding(designator=f"T{n:03d}")))
        )
        assert 0.10 < drawn / 400 < 0.32


# --------------------------------------------------------------------------
# Unknown never releases unattended
# --------------------------------------------------------------------------


class TestUnknownIsNeverAutoPublished:
    def test_at_the_default_threshold(self):
        held = decide(gate(), finding(exposure=Exposure.UNKNOWN), at=NOW)
        assert held.disposition is Disposition.HELD

    def test_and_at_the_widest_threshold_a_tenant_can_set(self):
        # An unmade check is not a low-severity finding, it is the absence of
        # one. Releasing it unattended publishes "we did not look" as though it
        # were "nothing to report".
        wide = gate(auto_publish_at_or_below=Exposure.CRITICAL)
        held = decide(wide, finding(exposure=Exposure.UNKNOWN), at=NOW)
        assert held.disposition is Disposition.HELD
        assert "we did not look" in held.reason

    def test_a_person_can_still_attest_it(self):
        one = finding(exposure=Exposure.UNKNOWN)
        signed = Attestation(by="J. Morgan", at=NOW, finding=fingerprint(one))
        assert decide(gate(), one, attestation=signed, at=NOW).is_released


# --------------------------------------------------------------------------
# The threshold, and moving it
# --------------------------------------------------------------------------


class TestTheThreshold:
    @pytest.mark.parametrize(
        "exposure,expected",
        [
            (Exposure.NONE, Disposition.PUBLISHED),
            (Exposure.LOW, Disposition.PUBLISHED),
            (Exposure.MEDIUM, Disposition.PUBLISHED),
            (Exposure.HIGH, Disposition.HELD),
            (Exposure.CRITICAL, Disposition.HELD),
        ],
    )
    def test_the_default_matches_the_plan(self, exposure, expected):
        # Info and low auto-publish, medium auto-publishes with sampling, high
        # and critical need attestation.
        assert gate(sample_rate=0.0).auto_publishes(exposure) is (
            expected is Disposition.PUBLISHED
        )

    def test_the_default_is_medium(self):
        assert DEFAULT_AUTO_PUBLISH is Exposure.MEDIUM
        assert gate().is_default

    def test_a_narrower_gate_holds_more(self):
        strict = gate(auto_publish_at_or_below=Exposure.LOW)
        assert decide(strict, finding(exposure=Exposure.MEDIUM), at=NOW).needs_a_person

    def test_a_widened_gate_is_recorded_as_a_choice(self):
        # So a regulator reading the log sees that a wider gate was somebody's
        # decision rather than how the product arrived.
        wide = gate(auto_publish_at_or_below=Exposure.HIGH)
        assert wide.is_widened
        assert "WIDENED" in wide.describe()

    def test_the_default_is_not_marked_as_widened(self):
        assert not gate().is_widened

    def test_a_narrower_gate_is_not_marked_as_widened_either(self):
        assert not gate(auto_publish_at_or_below=Exposure.LOW).is_widened

    def test_a_gate_belongs_to_a_tenant(self):
        with pytest.raises(ValueError):
            ReviewGate(tenant="  ")


# --------------------------------------------------------------------------
# The log, and the metric the plan wants tracked
# --------------------------------------------------------------------------


class TestTheLog:
    def mixed(self):
        return [
            finding(exposure=Exposure.NONE, designator="A"),
            finding(exposure=Exposure.MEDIUM, designator="B"),
            finding(exposure=Exposure.CRITICAL, designator="C"),
            finding(exposure=Exposure.UNKNOWN, designator="D"),
        ]

    def test_it_records_every_decision(self):
        log = review(gate(sample_rate=0.0), self.mixed(), at=NOW)
        assert log.summary()["findings"] == 4
        assert log.summary()["published"] == 2
        assert log.summary()["held"] == 2

    def test_the_auto_published_share_is_the_metric(self):
        log = review(gate(sample_rate=0.0), self.mixed(), at=NOW)
        assert log.auto_published_share == 0.5
        assert "auto-published without a person: 50%" in log.render()

    def test_an_attested_release_is_not_counted_as_auto_published(self):
        # A person was in that path, which is the whole distinction being
        # measured.
        findings = self.mixed()
        signed = Attestation(by="J. Morgan", at=NOW,
                             finding=fingerprint(findings[2]))
        log = review(gate(sample_rate=0.0), findings, attestations=[signed], at=NOW)
        assert log.summary()["attested"] == 1
        assert log.auto_published_share == 0.5

    def test_an_empty_log_reports_nothing_auto_published(self):
        assert GateLog(gate=gate()).auto_published_share == 0.0

    def test_held_findings_are_in_front_of_a_reviewer_not_invisible(self):
        printed = review(gate(sample_rate=0.0), self.mixed(), at=NOW).render()
        assert "AWAITING ATTESTATION" in printed
        assert "in front of a reviewer, not a crew" in printed

    def test_a_widened_gate_says_so_in_its_own_log(self):
        printed = review(
            gate(auto_publish_at_or_below=Exposure.HIGH, sample_rate=0.0),
            self.mixed(), at=NOW,
        ).render()
        assert "releases more without a person than the product default" in printed
        assert "visible in an audit" in printed

    def test_an_attestation_for_a_finding_that_is_gone_is_not_an_error(self):
        # It is a signature for something that has since changed or gone away.
        stale = Attestation(by="J. Morgan", at=NOW,
                            finding=fingerprint(finding(designator="VANISHED")))
        log = review(gate(sample_rate=0.0), self.mixed(),
                     attestations=[stale], at=NOW)
        assert log.summary()["findings"] == 4
        assert log.summary()["attested"] == 0


class TestTheGateIsOnlyOnTheOutputPlane:
    def test_it_takes_findings_and_returns_findings(self):
        # The data plane is fully autonomous. This module never fetches,
        # parses, validates or assesses anything - it decides what reaches a
        # consumer, and nothing else.
        import inspect

        import aeropub.gate as module

        source = inspect.getsource(module)
        for forbidden in ("urllib", "requests", "open(", "FactStore", "build("):
            assert forbidden not in source, (
                f"the gate reached for {forbidden!r}; it decides what is "
                "released and does no data work"
            )
