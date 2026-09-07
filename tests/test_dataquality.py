"""Aeronautical data quality, as each State publishes it for itself.

PANS-AIM's data catalogue is copyrighted and is not in this repository. What
is held is each State's own published accuracy, resolution and integrity,
cited to the AIP section it came from — which is the figure a State can be
held to, and the one a finding can quote back at an authority.

So the assertions are mostly about absence again. A State that published no
integrity class has not published "routine"; a State nobody has read has not
met anybody's standard; and a resolution check that cannot compare two values
answers no rather than guessing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aeropub.dataquality import (
    CATALOGUE_IS_NOT_HELD,
    DataQualityRegister,
    DataRequirement,
    Integrity,
    data_quality_template,
    load_data_quality,
    view_data_quality,
)
from aeropub.manifest import ManifestError
from aeropub.provenance import SourceRef

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="GEN 2.1",
        retrieved_at=NOW,
        content_hash="e" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def requirement(**overrides) -> DataRequirement:
    fields = dict(
        item="significant point",
        source=ref(),
        region="OTDF",
        accuracy="100 m",
        publication_resolution="1/100 sec",
        integrity=Integrity.ESSENTIAL,
    )
    fields.update(overrides)
    return DataRequirement(**fields)


def register(*requirements, covers=()) -> DataQualityRegister:
    held = requirements or (requirement(),)
    return DataQualityRegister(
        requirements=held, covers=frozenset(covers) | {r.region for r in held}
    )


class TestIntegrity:
    def test_the_three_classes_rank(self):
        assert Integrity.CRITICAL.at_least(Integrity.ESSENTIAL) is True
        assert Integrity.ROUTINE.at_least(Integrity.CRITICAL) is False

    def test_an_unread_class_has_no_rank(self):
        """Sorting below routine would read as safer than the safest class,
        which is the opposite of not knowing."""
        assert Integrity.UNREAD.rank is None
        assert Integrity.NOT_CLASSIFIED.rank is None

    def test_comparing_against_an_unknown_answers_nothing(self):
        assert Integrity.CRITICAL.at_least(Integrity.UNREAD) is None
        assert Integrity.UNREAD.at_least(Integrity.ROUTINE) is None

    def test_published_but_unclassified_is_not_routine(self):
        assert not Integrity.NOT_CLASSIFIED.is_known
        assert Integrity.NOT_CLASSIFIED is not Integrity.ROUTINE


class TestLookup:
    def test_an_item_a_state_published_is_found(self):
        assert register().for_item("significant point", "OTDF") is not None

    def test_an_item_it_did_not_publish_is_none_not_a_default(self):
        """A State that published no accuracy for a threshold has not thereby
        agreed to anybody else's."""
        assert register().for_item("runway threshold", "OTDF") is None

    def test_an_unpublished_items_integrity_is_unread(self):
        assert register().integrity_of("runway threshold", "OTDF") is Integrity.UNREAD


class TestView:
    def test_a_region_never_read_is_a_row_not_a_silence(self):
        view = view_data_quality(DataQualityRegister(), regions=["OTDF"])
        assert view.unread_regions == ("OTDF",)
        assert not view.is_conclusive
        assert "has not thereby met anybody's" in view.render()

    def test_read_and_publishing_nothing_is_kept_apart_from_unread(self):
        empty = DataQualityRegister(covers=frozenset({"OTDF"}))
        view = view_data_quality(empty, regions=["OTDF"])
        assert view.unread_regions == ()
        assert view.silent_regions == ("OTDF",)

    def test_an_unclassified_item_is_reported_separately(self):
        view = view_data_quality(
            register(requirement(integrity=Integrity.NOT_CLASSIFIED)),
            regions=["OTDF"],
        )
        assert len(view.unclassified) == 1
        assert "Unstated, not routine" in view.render()

    def test_critical_items_can_be_listed(self):
        view = view_data_quality(
            register(requirement(integrity=Integrity.CRITICAL)), regions=["OTDF"]
        )
        assert len(view.critical_items) == 1


class TestResolution:
    def held(self, value):
        return view_data_quality(
            register(), regions=["OTDF"], held={("OTDF", "significant point"): value}
        )

    def test_a_coarser_value_than_published_is_a_finding(self):
        """Whole seconds held where the State publishes hundredths — the
        coarser value came from our chain, not the AIP."""
        view = self.held("251612N0513748E")
        assert len(view.findings) == 1
        assert "came from somewhere in our chain" in view.findings[0].describe()

    def test_a_value_at_the_published_resolution_is_not_a_finding(self):
        assert self.held("251612.34N0513748.12E").findings == ()

    def test_a_value_it_cannot_compare_is_not_a_finding(self):
        """A resolution check that guessed would produce findings about its
        own arithmetic."""
        assert self.held("somewhere near the threshold").findings == ()

    def test_an_item_with_no_published_resolution_raises_nothing(self):
        view = view_data_quality(
            register(requirement(publication_resolution="")),
            regions=["OTDF"],
            held={("OTDF", "significant point"): "251612N0513748E"},
        )
        assert view.findings == ()


class TestItIsNotTheCatalogue:
    def test_the_module_says_the_catalogue_is_not_held(self):
        assert "copyrighted" in CATALOGUE_IS_NOT_HELD
        assert "not reproduced here" in CATALOGUE_IS_NOT_HELD

    def test_the_docstring_says_whose_figures_these_are(self):
        import aeropub.dataquality as module

        assert "each State publishes for itself" in module.__doc__


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "TEST",
            "document": "AIP OT GEN 2.1",
            "retrieved_at": "2026-09-01T00:00:00Z",
            "content_hash": "a" * 64,
        },
        "region": "OTDF",
        "requirements": [
            {
                "item": "significant point",
                "accuracy": "100 m",
                "publication_resolution": "1/100 sec",
                "integrity": "essential",
                "locator": "GEN 2.1 table 1",
            }
        ],
    }
    payload.update(overrides)
    return payload


def write(tmp_path, payload):
    path = tmp_path / "dq.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLoading:
    def test_a_requirement_is_read_with_its_citation(self, tmp_path):
        held = load_data_quality(write(tmp_path, manifest()))
        found = held.for_item("significant point", "OTDF")
        assert found.source.locator == "GEN 2.1 table 1"
        assert found.integrity is Integrity.ESSENTIAL

    def test_a_row_with_no_locator_is_refused(self, tmp_path):
        payload = manifest()
        del payload["requirements"][0]["locator"]
        with pytest.raises(ManifestError, match="locator is required"):
            load_data_quality(write(tmp_path, payload))

    def test_an_invented_integrity_class_is_refused(self, tmp_path):
        payload = manifest()
        payload["requirements"][0]["integrity"] = "quite important"
        with pytest.raises(ManifestError, match="not.*routine"):
            load_data_quality(write(tmp_path, payload))

    def test_a_region_read_with_no_rows_is_covered_not_absent(self, tmp_path):
        held = load_data_quality(write(tmp_path, manifest(requirements=[])))
        assert held.is_read("OTDF")
        assert held.for_item("significant point", "OTDF") is None

    def test_the_template_defaults_integrity_to_unread(self):
        """Rather than to a class nobody published."""
        blank = json.loads(data_quality_template())
        assert blank["requirements"][0]["integrity"] == "unread"
