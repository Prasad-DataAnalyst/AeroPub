"""The ICAO Annexes as a citation index.

Two things are being protected here. That the index stays a bibliography and
never becomes a copy of the standards — they are sold by ICAO and may not be
reproduced. And that `bears_on` stays true: an index of dependencies that has
drifted from the code is worse than none, because an amendment gets scoped
against it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aeropub.annexes import (
    ANNEX_TEXT_IS_NOT_HELD,
    ANNEXES,
    PROVISIONS,
    annex,
    bearing_on,
)

SOURCE = Path(__file__).parent.parent / "src" / "aeropub"


class TestTheIndex:
    def test_all_nineteen_are_present(self):
        assert sorted(ANNEXES) == list(range(1, 20))

    def test_each_has_a_title_and_says_what_it_governs(self):
        for number, found in ANNEXES.items():
            assert found.title, number
            assert len(found.governs) > 20, number

    def test_asking_for_one_that_does_not_exist_says_so(self):
        with pytest.raises(KeyError, match="1 to 19"):
            annex(20)

    def test_the_multi_volume_annexes_list_their_parts(self):
        """Annex 6 Part I and Part II carry different rules for different
        operations, so a citation without the part cannot be followed."""
        assert len(annex(6).volumes) == 4
        assert len(annex(10).volumes) == 6
        assert len(annex(14).volumes) == 2
        assert len(annex(16).volumes) == 4

    def test_annex_6_includes_the_rpas_part(self):
        """Newer than most references — checked rather than recalled."""
        assert any("Remotely Piloted" in v for v in annex(6).volumes)


class TestCitation:
    def test_a_single_volume_annex_cites_plainly(self):
        assert annex(11).citation() == "ICAO Annex 11 — Air Traffic Services"

    def test_a_detail_travels_with_it(self):
        assert "Appendix 3" in annex(2).citation(detail="Appendix 3")

    def test_a_multi_volume_annex_refuses_to_be_cited_without_one(self):
        """A bare "Annex 6" is ambiguous across four Parts."""
        with pytest.raises(ValueError, match="published in 4 parts"):
            annex(6).citation()

    def test_naming_the_volume_produces_a_citation(self):
        found = annex(14).citation("Volume I", "aerodrome reference code")
        assert "Volume I" in found and "reference code" in found


class TestDependencies:
    def test_the_load_bearing_annexes_are_the_ones_with_modules(self):
        assert {a.number for a in ANNEXES.values() if a.is_load_bearing} == {
            2, 10, 11, 14, 15
        }

    def test_an_annex_nothing_depends_on_says_so(self):
        assert not annex(9).is_load_bearing

    def test_a_module_can_be_asked_what_it_rests_on(self):
        """The question an amendment raises: what does this touch."""
        assert [a.number for a in bearing_on("aeropub.airspace")] == [11]
        assert [a.number for a in bearing_on("obstacles")] == [14]

    def test_every_named_module_exists(self):
        """An index that has drifted from the code is worse than none."""
        for found in ANNEXES.values():
            for module in found.bears_on:
                assert (SOURCE / f"{module}.py").exists(), (
                    f"Annex {found.number} names {module}, which is not a module"
                )

    def test_every_named_module_actually_mentions_its_annex(self):
        """Kept honest against the source rather than against a memory of it."""
        for found in ANNEXES.values():
            for module in found.bears_on:
                text = (SOURCE / f"{module}.py").read_text(encoding="utf-8")
                assert re.search(rf"Annex {found.number}\b", text), (
                    f"{module}.py is listed under Annex {found.number} "
                    "but does not mention it"
                )


class TestProvisions:
    def test_each_names_a_real_annex_and_says_what_it_establishes(self):
        for provision in PROVISIONS:
            assert provision.annex in ANNEXES
            assert len(provision.what) > 30

    def test_each_produces_a_followable_citation(self):
        for provision in PROVISIONS:
            assert provision.citation().startswith("ICAO Annex ")

    def test_the_cruising_level_table_is_cited_to_appendix_3(self):
        found = next(p for p in PROVISIONS if "cruising levels" in p.what)
        assert found.annex == 2
        assert found.detail == "Appendix 3"
        assert "flightrules" in found.relied_on_by

    def test_a_provisions_modules_are_real(self):
        for provision in PROVISIONS:
            for module in provision.relied_on_by:
                assert (SOURCE / f"{module}.py").exists(), module


class TestItIsNotTheStandard:
    def test_the_module_says_the_text_is_not_held(self):
        """Stated once, so nothing has to infer it from an absence."""
        assert "copyrighted" in ANNEX_TEXT_IS_NOT_HELD
        assert "not reproduced here" in ANNEX_TEXT_IS_NOT_HELD

    def test_the_docstring_leads_with_it(self):
        import aeropub.annexes as module

        assert "not open source and are not in this repository" in module.__doc__

    def test_no_entry_is_long_enough_to_be_quoting_a_standard(self):
        """A one-line description of what an Annex governs is metadata. A
        paragraph of it would be something else."""
        for found in ANNEXES.values():
            assert len(found.governs) < 200, found.number
        for provision in PROVISIONS:
            assert len(provision.what) < 300, provision.annex
