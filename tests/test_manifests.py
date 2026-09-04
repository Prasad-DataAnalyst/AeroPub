"""Citation manifests — the hand-written way in, and what it refuses.

A manifest is provenance done by hand, and the whole value of the format is
that doing it wrong fails rather than passing quietly. So most of what is
tested here is refusal: a document that cannot be identified, a figure with no
locator, a supplement loaded as an AIP, a hash that no longer matches the file
on disk.

The manifests below are written into a tmp_path and cite files created beside
them. That is the format working as intended — a citation is only writable
when the document is there to be hashed — and it is why these tests create
files rather than mocking a hash.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from aeropub.acap import load_aircraft, merge
from aeropub.acap import template as aircraft_template
from aeropub.aircraft import AircraftType, Origin
from aeropub.facts import Precedence
from aeropub.ingest import load_facts
from aeropub.ingest import template as fact_template
from aeropub.manifest import ManifestError, sha256_of, sub_source
from aeropub.provenance import Confidence, SourceRef

READ_AT = "2026-09-01T12:00:00Z"


@pytest.fixture
def document(tmp_path: Path) -> Path:
    """A file standing in for the publication a manifest cites."""
    path = tmp_path / "source.txt"
    path.write_text("a document, standing in for one somebody read\n", encoding="utf-8")
    return path


def write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def aircraft_manifest(**overrides) -> dict:
    payload = {
        "designator": "TEST",
        "manufacturer": "Example",
        "model": "Type",
        "source": {
            "source_id": "EXAMPLE",
            "document": "Airplane Characteristics for Airport Planning",
            "document_path": "source.txt",
            "retrieved_at": READ_AT,
        },
        "origin": "acap",
        "characteristics": [
            {"attribute": "wingspan_m", "value": 60.0, "unit": "m",
             "locator": "Table 2.1.1"},
        ],
    }
    payload.update(overrides)
    return payload


def fact_manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE-CAA",
            "document": "AIP Example AD 2 XXXX",
            "document_path": "source.txt",
            "retrieved_at": READ_AT,
            "published_at": "2026-09-03",
        },
        "precedence": "aip",
        "valid_from": "2026-09-03",
        "facts": [
            {"entity": "XXXX/RWY34L", "attribute": "pcn", "value": "80/F/A/W/T",
             "locator": "AD 2.12, RWY 34L, strength"},
        ],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# The document has to be identifiable
# --------------------------------------------------------------------------


class TestTheDocumentIsHashedAsItIsRead:
    def test_the_hash_comes_from_the_file_on_disk(self, tmp_path, document):
        path = write(tmp_path, "plane.json", aircraft_manifest())
        loaded = load_aircraft(path)
        assert loaded.characteristics[0].source.content_hash == sha256_of(document)

    def test_a_manifest_citing_a_missing_document_is_refused(self, tmp_path):
        payload = aircraft_manifest()
        payload["source"]["document_path"] = "not-here.pdf"
        path = write(tmp_path, "plane.json", payload)
        with pytest.raises(ManifestError) as caught:
            load_aircraft(path)
        assert "is not a file" in str(caught.value)

    def test_a_stale_hash_is_an_error_not_a_preference(self, tmp_path, document):
        # The document on disk is not the one the manifest was written against.
        # Silently preferring the file would hide that a source changed.
        payload = aircraft_manifest()
        payload["source"]["content_hash"] = "a" * 64
        path = write(tmp_path, "plane.json", payload)
        with pytest.raises(ManifestError) as caught:
            load_aircraft(path)
        assert "does not match" in str(caught.value)

    def test_a_matching_hash_alongside_the_path_is_accepted(self, tmp_path, document):
        payload = aircraft_manifest()
        payload["source"]["content_hash"] = sha256_of(document)
        assert load_aircraft(write(tmp_path, "plane.json", payload))

    def test_a_bare_hash_is_accepted_for_a_document_held_elsewhere(self, tmp_path):
        payload = aircraft_manifest()
        del payload["source"]["document_path"]
        payload["source"]["content_hash"] = "b" * 64
        loaded = load_aircraft(write(tmp_path, "plane.json", payload))
        assert loaded.characteristics[0].source.content_hash == "b" * 64

    def test_neither_a_path_nor_a_hash_is_refused(self, tmp_path):
        payload = aircraft_manifest()
        del payload["source"]["document_path"]
        with pytest.raises(ManifestError) as caught:
            load_aircraft(write(tmp_path, "plane.json", payload))
        assert "not citable" in str(caught.value)

    def test_a_naive_timestamp_is_refused(self, tmp_path, document):
        payload = aircraft_manifest()
        payload["source"]["retrieved_at"] = "2026-09-01T12:00:00"
        with pytest.raises(ManifestError) as caught:
            load_aircraft(write(tmp_path, "plane.json", payload))
        assert "timezone" in str(caught.value)

    def test_a_manifest_that_is_not_json_names_the_file(self, tmp_path):
        path = tmp_path / "plane.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ManifestError) as caught:
            load_aircraft(path)
        assert "plane.json" in str(caught.value)


class TestSubSource:
    def test_only_the_locator_changes(self):
        document = SourceRef(
            source_id="X", document="D", locator="(whole document)",
            retrieved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            content_hash="c" * 64, parser_id="p", parser_version="1",
            confidence=Confidence.MEDIUM, published_at=date(2026, 9, 1),
            original_url="https://example.invalid/d",
        )
        inside = sub_source(document, "Table 2.1.1")
        assert inside.locator == "Table 2.1.1"
        for field in (
            "source_id", "document", "retrieved_at", "content_hash",
            "parser_id", "parser_version", "confidence", "published_at",
            "original_url",
        ):
            assert getattr(inside, field) == getattr(document, field)


# --------------------------------------------------------------------------
# Aircraft manifests
# --------------------------------------------------------------------------


class TestAircraftManifest:
    def test_a_figure_without_a_locator_is_refused(self, tmp_path, document):
        # Naming the document alone is not a citation a reviewer can resolve.
        payload = aircraft_manifest()
        del payload["characteristics"][0]["locator"]
        with pytest.raises(ManifestError) as caught:
            load_aircraft(write(tmp_path, "plane.json", payload))
        assert "locator" in str(caught.value)

    def test_a_figure_with_no_value_is_refused(self, tmp_path, document):
        payload = aircraft_manifest()
        payload["characteristics"][0]["value"] = None
        with pytest.raises(ManifestError) as caught:
            load_aircraft(write(tmp_path, "plane.json", payload))
        assert "gap in what is held" in str(caught.value)

    def test_a_per_figure_origin_is_refused(self, tmp_path, document):
        # The citation a figure would carry is the manifest's document, so a
        # figure from a different source comes out cited to the wrong page —
        # worse than uncited, because it resolves.
        payload = aircraft_manifest()
        payload["characteristics"][0]["origin"] = "operator"
        with pytest.raises(ManifestError) as caught:
            load_aircraft(write(tmp_path, "plane.json", payload))
        assert "own manifest" in str(caught.value)

    def test_an_unknown_origin_is_not_defaulted(self, tmp_path, document):
        payload = aircraft_manifest(origin="probably fine")
        with pytest.raises(ManifestError) as caught:
            load_aircraft(write(tmp_path, "plane.json", payload))
        assert "leave this tenant" in str(caught.value)

    def test_no_designator_is_refused(self, tmp_path, document):
        payload = aircraft_manifest(designator="  ")
        with pytest.raises(ManifestError):
            load_aircraft(write(tmp_path, "plane.json", payload))

    def test_an_empty_characteristics_list_is_refused(self, tmp_path, document):
        payload = aircraft_manifest(characteristics=[])
        with pytest.raises(ManifestError) as caught:
            load_aircraft(write(tmp_path, "plane.json", payload))
        assert "coverage gap" in str(caught.value)

    def test_one_bad_figure_fails_the_whole_file(self, tmp_path, document):
        # A library that is nearly all cited is one people stop checking.
        payload = aircraft_manifest()
        payload["characteristics"].append(
            {"attribute": "omgws_m", "value": 12.0}  # no locator
        )
        with pytest.raises(ManifestError):
            load_aircraft(write(tmp_path, "plane.json", payload))

    def test_units_and_variants_survive(self, tmp_path, document):
        payload = aircraft_manifest()
        payload["characteristics"] = [
            {"attribute": "acn", "value": 62.0, "variant": "F/A at MTOW",
             "locator": "Table 5.3.1"},
        ]
        item = load_aircraft(write(tmp_path, "plane.json", payload)).get("acn")
        assert item.variant == "F/A at MTOW"


class TestMerge:
    def build(self, tmp_path, document):
        acap = write(tmp_path, "acap.json", aircraft_manifest())
        operator_doc = tmp_path / "afm.txt"
        operator_doc.write_text("an operator's own document\n", encoding="utf-8")
        operator = write(tmp_path, "op.json", {
            "designator": "TEST",
            "source": {"source_id": "OPERATOR", "document": "Company AFM extract",
                       "document_path": "afm.txt", "retrieved_at": READ_AT},
            "origin": "operator",
            "characteristics": [
                {"attribute": "mtow_kg", "value": 300000.0, "unit": "kg",
                 "locator": "Section 1.2"},
            ],
        })
        return load_aircraft(acap), load_aircraft(operator)

    def test_each_figure_keeps_the_citation_it_was_read_with(self, tmp_path, document):
        public, private = self.build(tmp_path, document)
        both = merge(public, private)
        by_attribute = {c.attribute: c for c in both.characteristics}
        assert "Airport Planning" in by_attribute["wingspan_m"].source.document
        assert "AFM" in by_attribute["mtow_kg"].source.document
        assert by_attribute["mtow_kg"].source.content_hash != (
            by_attribute["wingspan_m"].source.content_hash
        )

    def test_the_operator_half_stays_out_of_the_redistributable_view(
        self, tmp_path, document
    ):
        both = merge(*self.build(tmp_path, document))
        assert {c.attribute for c in both.redistributable} == {"wingspan_m"}

    def test_merging_different_types_is_refused(self, tmp_path, document):
        # Merging them would produce one aeroplane with another's wingspan.
        public, private = self.build(tmp_path, document)
        other = AircraftType(designator="OTHER")
        with pytest.raises(ValueError) as caught:
            merge(public, other)
        assert "different types" in str(caught.value)

    def test_one_manifest_merges_to_itself(self, tmp_path, document):
        public, _ = self.build(tmp_path, document)
        assert merge(public) == public

    def test_merging_nothing_is_refused(self):
        with pytest.raises(ValueError):
            merge()


# --------------------------------------------------------------------------
# Fact manifests
# --------------------------------------------------------------------------


class TestFactManifest:
    def test_a_document_loads_into_cited_dated_facts(self, tmp_path, document):
        facts = load_facts(write(tmp_path, "ad2.json", fact_manifest()))
        assert len(facts) == 1
        one = facts[0]
        assert one.entity == "XXXX/RWY34L"
        assert one.precedence is Precedence.AIP
        assert one.valid_from == date(2026, 9, 3)
        assert one.source.locator == "AD 2.12, RWY 34L, strength"
        assert one.source.published_at == date(2026, 9, 3)

    def test_precedence_is_required_and_never_defaulted(self, tmp_path, document):
        # A supplement loaded as an AIP sits beneath the base it is meant to
        # override, and the effective state comes out wrong with nothing
        # downstream able to tell.
        payload = fact_manifest()
        del payload["precedence"]
        with pytest.raises(ManifestError) as caught:
            load_facts(write(tmp_path, "ad2.json", payload))
        assert "precedence is required" in str(caught.value)

    def test_an_unknown_precedence_is_refused(self, tmp_path, document):
        with pytest.raises(ManifestError):
            load_facts(write(tmp_path, "ad2.json", fact_manifest(precedence="probably")))

    def test_precedence_may_not_be_set_per_fact(self, tmp_path, document):
        payload = fact_manifest()
        payload["facts"][0]["precedence"] = "sup"
        with pytest.raises(ManifestError) as caught:
            load_facts(write(tmp_path, "ad2.json", payload))
        assert "belongs to the document" in str(caught.value)

    def test_a_supplement_loads_at_its_own_layer(self, tmp_path, document):
        payload = fact_manifest(precedence="sup", valid_to="2026-11-15")
        facts = load_facts(write(tmp_path, "sup.json", payload))
        assert facts[0].precedence is Precedence.SUP
        assert facts[0].valid_to == date(2026, 11, 15)

    def test_a_fact_may_carry_its_own_window(self, tmp_path, document):
        payload = fact_manifest()
        payload["facts"][0]["valid_from"] = "2026-10-01"
        payload["facts"][0]["valid_to"] = "2026-10-31"
        facts = load_facts(write(tmp_path, "ad2.json", payload))
        assert facts[0].valid_from == date(2026, 10, 1)
        assert facts[0].valid_to == date(2026, 10, 31)

    def test_a_missing_valid_from_is_refused(self, tmp_path, document):
        payload = fact_manifest()
        del payload["valid_from"]
        with pytest.raises(ManifestError) as caught:
            load_facts(write(tmp_path, "ad2.json", payload))
        assert "valid_from is required" in str(caught.value)

    def test_a_window_that_ends_before_it_starts_is_refused(self, tmp_path, document):
        payload = fact_manifest(valid_to="2026-09-01")
        with pytest.raises(ManifestError) as caught:
            load_facts(write(tmp_path, "ad2.json", payload))
        assert "expires" in str(caught.value)

    def test_a_value_without_a_locator_is_refused(self, tmp_path, document):
        payload = fact_manifest()
        del payload["facts"][0]["locator"]
        with pytest.raises(ManifestError) as caught:
            load_facts(write(tmp_path, "ad2.json", payload))
        assert "locator" in str(caught.value)

    def test_a_value_of_none_is_a_coverage_gap_not_a_fact(self, tmp_path, document):
        payload = fact_manifest()
        payload["facts"][0]["value"] = None
        with pytest.raises(ManifestError) as caught:
            load_facts(write(tmp_path, "ad2.json", payload))
        assert "coverage gap" in str(caught.value)

    def test_no_entity_is_refused(self, tmp_path, document):
        payload = fact_manifest()
        payload["facts"][0]["entity"] = ""
        with pytest.raises(ManifestError):
            load_facts(write(tmp_path, "ad2.json", payload))

    def test_entity_keys_are_normalised_on_the_way_in(self, tmp_path, document):
        # The same grammar as everywhere else, applied once at the boundary,
        # so a lower-case manifest does not create a second entity.
        payload = fact_manifest()
        payload["facts"][0]["entity"] = "  xxxx/rwy34l  "
        assert load_facts(write(tmp_path, "ad2.json", payload))[0].entity == "XXXX/RWY34L"

    def test_the_parser_records_that_a_person_read_it(self, tmp_path, document):
        # A transcription error and a parser defect have different failure
        # modes and must trace separately.
        facts = load_facts(write(tmp_path, "ad2.json", fact_manifest()))
        assert facts[0].source.parser_id == "aip-manifest"

    def test_an_empty_facts_list_is_refused(self, tmp_path, document):
        with pytest.raises(ManifestError) as caught:
            load_facts(write(tmp_path, "ad2.json", fact_manifest(facts=[])))
        assert "coverage gap" in str(caught.value)

    def test_one_bad_value_fails_the_whole_document(self, tmp_path, document):
        payload = fact_manifest()
        payload["facts"].append(
            {"entity": "XXXX", "attribute": "rffs_category", "value": 9}
        )
        with pytest.raises(ManifestError):
            load_facts(write(tmp_path, "ad2.json", payload))


class TestTemplates:
    @pytest.mark.parametrize("render", [aircraft_template, fact_template])
    def test_a_template_is_valid_json(self, render):
        assert isinstance(json.loads(render()), dict)

    @pytest.mark.parametrize("render", [aircraft_template, fact_template])
    def test_a_template_carries_no_figures(self, render):
        # Nothing here for somebody to keep who did not open the document.
        def values(node):
            if isinstance(node, dict):
                for key, item in node.items():
                    if key == "value":
                        yield item
                    yield from values(item)
            elif isinstance(node, list):
                for item in node:
                    yield from values(item)

        assert all(v is None for v in values(json.loads(render())))

    @pytest.mark.parametrize("render", [aircraft_template, fact_template])
    def test_a_template_does_not_load_as_it_stands(self, render, tmp_path):
        # It is a form, not a working manifest: filling it in is the point.
        path = tmp_path / "blank.json"
        path.write_text(render(), encoding="utf-8")
        loader = load_aircraft if render is aircraft_template else load_facts
        with pytest.raises(ManifestError):
            loader(path)
