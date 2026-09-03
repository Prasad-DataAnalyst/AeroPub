"""The JSON the API returns.

Two of plan section 25's design rules are structural rather than editorial, and
these tests are what make them true rather than intended: provenance travels
with every value, and redistribution governs verbatim text with unknown
withholding.

The provenance test walks the whole document rather than checking a field,
because the rule has to hold for endpoints nobody has written yet.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from aeropub.aip import AipCoverage, HoldingState, SectionHolding, aerodrome_sections
from aeropub.airac import AiracCycle
from aeropub.api import (
    API_VERSION,
    VERBATIM_THRESHOLD,
    Licensing,
    document,
    dumps,
    ndjson,
    to_json,
)
from aeropub.bulletin import between_cycles
from aeropub.dossier import build
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.horizon import horizon
from aeropub.lenses import Audience, view
from aeropub.provenance import SourceRef
from aeropub.quality import assess_quality
from aeropub.registry import Redistribution

N1 = AiracCycle.from_identifier("2609")
N2 = AiracCycle.from_identifier("2610")
DAY_BEFORE = N2.effective_date - timedelta(days=1)
NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
PROSE = "Pilots shall note that " + "the following applies at all times. " * 8


def ref(document_name="AIP AMDT 09/26", locator="AD 2.13", source_id="QA-CAA"):
    return SourceRef(
        source_id=source_id, document=document_name, locator=locator,
        retrieved_at=datetime(2026, 9, 1, 14, 23, tzinfo=timezone.utc),
        content_hash="b" * 64, parser_id="eaip-eurocontrol", parser_version="0.1.0",
    )


def fact_of(entity, attribute, value, valid_from, valid_to=None,
            precedence=Precedence.AIP, **kwargs):
    return Fact(entity=entity, attribute=attribute, value=value,
                valid_from=valid_from, valid_to=valid_to,
                source=ref(**kwargs), precedence=precedence)


@pytest.fixture
def store():
    facts = FactStore()
    facts.add(fact_of("OTHH/RWY34L", "lda_m", 3900, date(2026, 1, 1), DAY_BEFORE))
    facts.add(fact_of("OTHH/RWY34L", "lda_m", 3500, N2.effective_date,
                      document_name="AIP AMDT 10/26"))
    facts.add(fact_of("OTHH", "rffs_category", 9, date(2026, 1, 1), locator="AD 2.6"))
    return facts


@pytest.fixture
def coverage():
    return AipCoverage([
        SectionHolding(section=s, entity="OTHH", state=HoldingState.HELD,
                       source=ref("AIP", s.code))
        for s in aerodrome_sections()
    ])


@pytest.fixture
def documents(store, coverage):
    dossier = build("OTHH", facts=store, coverage=coverage, as_at=NOW, cycle=N2)
    bulletin = between_cycles(store, "OTHH", N1, N2,
                              coverage_before=coverage, coverage_after=coverage)
    ahead = horizon(store, "OTHH", from_date=NOW.date(), days=60)
    return {
        "aerodrome_dossier": dossier,
        "change_bulletin": bulletin,
        "forward_view": ahead,
        "publication_conduct": assess_quality(as_at=NOW),
        "lens_view": view(Audience.DISPATCH, "OTHH", as_at=NOW,
                          dossier=dossier, bulletin=bulletin, ahead=ahead),
    }


def _walk(node, path="$"):
    """Every dict in a payload, with the path that reached it."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


class TestProvenanceIsNeverOmitted:
    """The rule that has to hold for endpoints nobody has written yet."""

    @pytest.mark.parametrize("kind", [
        "aerodrome_dossier", "change_bulletin", "forward_view", "lens_view",
    ])
    def test_every_value_travels_with_its_citation(self, documents, kind):
        payload = document(documents[kind])
        offenders = [
            path
            for path, node in _walk(payload)
            if "value" in node and "source_ref" not in node and not node.get("withheld")
        ]
        assert offenders == []

    def test_every_citation_is_complete(self, documents):
        # An abbreviated citation is not a citation: without the hash there is
        # no proof the document you find is the one that was parsed.
        required = {
            "source_id", "document", "locator", "retrieved_at", "content_hash",
            "parser_id", "parser_version", "confidence",
        }
        payload = document(documents["change_bulletin"])
        refs = [n for _, n in _walk(payload) if "content_hash" in n]
        assert refs
        for block in refs:
            assert required <= set(block)
            assert len(block["content_hash"]) == 64

    def test_an_unrecognised_object_is_refused_rather_than_guessed(self):
        with pytest.raises(TypeError, match="somebody typed"):
            to_json(object())

    def test_the_refusal_says_to_add_a_serialiser(self):
        with pytest.raises(TypeError, match="Add one here"):
            document({"entity": "OTHH"})


class TestLicensing:
    def test_an_extracted_figure_travels_whatever_the_licence(self, documents):
        strict = Licensing(by_source={"QA-CAA": Redistribution.PROHIBITED})
        payload = document(documents["change_bulletin"], licensing=strict)
        change = payload["data"]["changes"][0]
        assert change["before"]["value"] == 3900
        assert change["after"]["value"] == 3500

    def test_the_assessment_and_the_citation_always_travel(self, documents):
        strict = Licensing(by_source={"QA-CAA": Redistribution.PROHIBITED})
        change = document(documents["change_bulletin"], licensing=strict)["data"]["changes"][0]
        assert "landing distance available" in change["summary"]
        assert change["before"]["source_ref"]["document"] == "AIP AMDT 09/26"

    def test_reproduced_prose_is_withheld(self):
        strict = Licensing(by_source={"QA-CAA": Redistribution.PROHIBITED})
        prose = fact_of("OTHH", "local_regulations", PROSE, date(2026, 1, 1))
        assert len(PROSE) > VERBATIM_THRESHOLD
        payload = to_json(prose, licensing=strict)
        assert payload["value"]["withheld"] is True
        assert payload["value"]["redistribution"] == "prohibited"
        assert payload["source_ref"]["content_hash"] == "b" * 64

    def test_withholding_is_an_object_not_an_empty_string(self):
        # An integrator must see a licence decision, not what looks like
        # missing data.
        strict = Licensing(by_source={"QA-CAA": Redistribution.PROHIBITED})
        value = to_json(fact_of("OTHH", "x", PROSE, date(2026, 1, 1)),
                        licensing=strict)["value"]
        assert isinstance(value, dict)
        assert value["reason"]
        assert value["source_id"] == "QA-CAA"

    def test_an_unrecorded_source_withholds_by_default(self):
        # Assuming permission is the expensive mistake.
        assert Licensing().for_source("anything") is Redistribution.UNKNOWN
        assert not Licensing().may_republish("anything")
        payload = to_json(fact_of("OTHH", "x", PROSE, date(2026, 1, 1)),
                          licensing=Licensing())
        assert payload["value"]["withheld"] is True

    def test_conditional_permits_because_its_conditions_are_the_citation(self):
        # Attribution and currency warnings are satisfied by the source_ref the
        # payload already carries.
        licensing = Licensing(by_source={"QA-CAA": Redistribution.CONDITIONAL})
        assert licensing.may_republish("QA-CAA")

    def test_notam_text_is_governed_too(self, store, coverage):
        from aeropub.notam_register import (
            NotamRegister, RegisteredNotam, Subject, SubjectKind,
        )

        register = NotamRegister([
            RegisteredNotam(
                identifier="A1/26",
                subjects=(Subject(entity="OTHH/RWY34L", kind=SubjectKind.RUNWAY),),
                source=ref("NOTAM A1/26", "NOTAM"),
                text="RWY 34L PAPI U/S",
                effective_start=NOW - timedelta(days=1),
                effective_end=NOW + timedelta(days=1),
            )
        ])
        dossier = build("OTHH", facts=store, coverage=coverage, register=register,
                        as_at=NOW)
        strict = Licensing(by_source={"QA-CAA": Redistribution.PROHIBITED})
        notam = document(dossier, licensing=strict)["data"]["notams"][0]
        assert notam["text"]["withheld"] is True
        assert notam["identifier"] == "A1/26"
        assert notam["source_ref"]["content_hash"]


class TestEnvelope:
    def test_the_envelope_is_versioned_and_names_what_it_holds(self, documents):
        payload = document(documents["aerodrome_dossier"], generated_at=NOW)
        assert payload["aeropub"]["version"] == API_VERSION == "v1"
        assert payload["aeropub"]["kind"] == "aerodrome_dossier"
        assert payload["aeropub"]["generated_at"] == "2026-10-05T06:00:00+00:00"

    @pytest.mark.parametrize("kind", [
        "aerodrome_dossier", "change_bulletin", "forward_view",
        "publication_conduct", "lens_view",
    ])
    def test_every_document_type_names_itself(self, documents, kind):
        assert document(documents[kind])["aeropub"]["kind"] == kind

    def test_the_request_that_produced_it_is_echoed(self, documents):
        payload = document(documents["change_bulletin"],
                           request={"before": "2609", "after": "2610"})
        assert payload["aeropub"]["request"] == {"before": "2609", "after": "2610"}


class TestSerialisation:
    @pytest.mark.parametrize("kind", [
        "aerodrome_dossier", "change_bulletin", "forward_view",
        "publication_conduct", "lens_view",
    ])
    def test_every_document_survives_json(self, documents, kind):
        assert json.loads(dumps(documents[kind], generated_at=NOW))

    def test_output_is_deterministic(self, documents):
        first = dumps(documents["change_bulletin"], generated_at=NOW)
        second = dumps(documents["change_bulletin"], generated_at=NOW)
        assert first == second
        # Sorted keys, so a diff between cycles shows what changed rather than
        # how a dict happened to be ordered.
        assert first.index('"after"') < first.index('"before"')

    def test_a_naive_timestamp_is_refused(self, documents):
        with pytest.raises(ValueError, match="timezone-aware"):
            document(documents["change_bulletin"], generated_at=datetime(2026, 10, 5))

    def test_times_are_iso_with_an_offset(self, documents):
        payload = document(documents["forward_view"], generated_at=NOW)
        assert payload["aeropub"]["generated_at"].endswith("+00:00")

    def test_ndjson_yields_one_document_per_line(self, documents):
        lines = list(ndjson(
            [documents["change_bulletin"], documents["forward_view"]],
            generated_at=NOW,
        ))
        assert len(lines) == 2
        assert all("\n" not in line for line in lines)
        assert [json.loads(l)["aeropub"]["kind"] for l in lines] == [
            "change_bulletin", "forward_view",
        ]


class TestHonesty:
    def test_a_bulletin_carries_its_own_completeness(self, store):
        partial = AipCoverage([
            SectionHolding(section=s, entity="OTHH", state=HoldingState.HELD,
                           source=ref("AIP", s.code))
            for s in aerodrome_sections() if s.code != "AD 2.10"
        ])
        payload = document(between_cycles(store, "OTHH", N1, N2,
                                          coverage_before=partial,
                                          coverage_after=partial))["data"]
        assert payload["conclusive"] is False
        assert payload["sections_not_compared"] == ["AD 2.10"]

    def test_a_dossier_lists_its_coverage_gaps(self, store):
        payload = document(build("OTHH", facts=store, as_at=NOW))["data"]
        assert payload["complete"] is False
        assert len(payload["coverage_gaps"]) == 25

    def test_the_forward_view_says_what_it_is_silent_about(self, documents):
        assert "silent about" in document(documents["forward_view"])["data"]["note"]

    def test_a_lens_view_reports_whether_it_is_sound(self, documents):
        payload = document(documents["lens_view"])["data"]
        assert payload["sound"] is True
        assert payload["depends_on"]
        assert payload["audience"] == "dispatch"

    def test_conduct_findings_carry_the_standard_they_measure_against(self, documents):
        payload = document(documents["publication_conduct"])["data"]
        assert "PANS-AIM" in payload["standard"]
