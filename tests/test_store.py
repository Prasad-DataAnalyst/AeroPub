"""Durable storage, and the invariant the database enforces itself.

Held in memory, append-only is a convention. Written to disk it is a promise,
and the tests that matter here are the ones proving SQLite keeps it whether or
not a future caller remembers to.

The worked example is the design document's, as elsewhere: the no-mock-data
rule governs source data entering the product, not objects constructed to test
storage.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from aeropub.facts import Fact, FactStore, Precedence
from aeropub.provenance import Confidence, SourceRef
from aeropub.store import SCHEMA_VERSION, SqliteFactStore, open_store

RWY = "OTHH/RWY34L"
KNOWN = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="QA-CAA",
        document="AIP AD 2.13",
        locator=RWY,
        retrieved_at=datetime(2026, 9, 1, 14, 23, 11, tzinfo=timezone.utc),
        content_hash="b" * 64,
        parser_id="eaip-eurocontrol",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def fact(value=3900, **overrides) -> Fact:
    fields = dict(
        entity=RWY,
        attribute="lda_m",
        value=value,
        valid_from=date(2026, 1, 1),
        source=ref(),
        precedence=Precedence.AIP,
        recorded_at=KNOWN,
    )
    fields.update(overrides)
    return Fact(**fields)


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "aeropub.db") as opened:
        yield opened


class TestRoundTrip:
    def test_every_field_of_a_fact_survives_exactly(self, store, tmp_path):
        original = fact(
            valid_to=date(2026, 9, 20),
            precedence=Precedence.SUP,
            source=ref(
                confidence=Confidence.MEDIUM,
                published_at=date(2026, 8, 15),
                original_url="https://www.aim.gov.qa/eaip/x.html",
                archive_key="blobs/bb/bb/" + "b" * 64,
            ),
        )
        store.add(original)
        store.close()

        restored = list(open_store(tmp_path / "aeropub.db"))[0]
        assert restored == original

    @pytest.mark.parametrize(
        "value", [3900, 3.25, "GRF", True, False, ["a", "b"], {"k": 1}]
    )
    def test_value_types_survive_without_being_stringified(self, store, value):
        # Storing str(value) would round-trip a number back as text and
        # silently break every comparison that decides whether it changed.
        store.add(fact(value=value, attribute="probe"))
        restored = store.effective(RWY, "probe", date(2026, 6, 1))
        assert restored.value == value
        assert type(restored.value) is type(value)

    def test_a_value_that_cannot_round_trip_is_refused_at_the_door(self, store):
        with pytest.raises(TypeError, match="survive a round trip"):
            store.add(fact(value={1, 2, 3}))
        assert len(store) == 0

    def test_the_refusal_names_where_to_fix_it(self, store):
        with pytest.raises(TypeError, match="at the parser"):
            store.add(fact(value={1, 2, 3}))

    def test_a_batch_that_cannot_be_written_writes_nothing(self, store):
        with pytest.raises(TypeError):
            store.extend([fact(value=3900), fact(value={1, 2}, attribute="bad")])
        # A parser failing halfway through AD 2 must not leave half a section
        # in the store looking complete.
        assert len(store) == 0


class TestAppendOnly:
    """The invariant the schema enforces, tested against raw SQL.

    Going through the store's own API would only prove the API is polite. What
    matters is that a migration script, a debugging session or a future
    maintainer cannot rewrite history either.
    """

    @pytest.fixture
    def raw(self, store, tmp_path):
        store.add(fact())
        connection = sqlite3.connect(str(tmp_path / "aeropub.db"), timeout=5.0)
        yield connection
        connection.close()

    def test_delete_always_aborts(self, raw):
        with pytest.raises(sqlite3.IntegrityError, match="never delete"):
            raw.execute("DELETE FROM facts")

    @pytest.mark.parametrize(
        "column,value",
        [
            ("value_json", "'9999'"),
            ("entity", "'XXXX'"),
            ("attribute", "'tora_m'"),
            ("valid_from", "'2020-01-01'"),
            ("precedence", "40"),
            ("recorded_at", "'2020-01-01T00:00:00+00:00'"),
            ("content_hash", "'\" + \"a'"),
        ],
    )
    def test_no_column_of_a_recorded_fact_can_be_edited(self, raw, column, value):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(f"UPDATE facts SET {column} = {value}")

    def test_clearing_a_supersession_is_refused(self, raw):
        raw.execute("UPDATE facts SET superseded_at = '2026-09-15T00:00:00+00:00'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE facts SET superseded_at = NULL")

    def test_a_row_can_only_be_superseded_once(self, raw):
        raw.execute("UPDATE facts SET superseded_at = '2026-09-15T00:00:00+00:00'")
        with pytest.raises(sqlite3.IntegrityError, match="only once"):
            raw.execute("UPDATE facts SET superseded_at = '2026-09-16T00:00:00+00:00'")


class TestSupersession:
    def test_superseding_closes_transaction_time_without_losing_the_row(self, store):
        store.add(fact())
        assert store.supersede(RWY, "lda_m", datetime(2026, 9, 15, tzinfo=timezone.utc)) == 1
        assert len(store) == 1
        assert store.statistics()["superseded"] == 1

    def test_what_we_believed_before_remains_answerable(self, store):
        store.add(fact())
        store.supersede(RWY, "lda_m", datetime(2026, 9, 15, tzinfo=timezone.utc))

        before = store.effective(RWY, "lda_m", date(2026, 6, 1),
                                 as_known_at=datetime(2026, 9, 14, tzinfo=timezone.utc))
        after = store.effective(RWY, "lda_m", date(2026, 6, 1),
                                as_known_at=datetime(2026, 9, 16, tzinfo=timezone.utc))
        assert before.value == 3900
        assert after is None

    def test_a_naive_supersession_time_is_refused(self, store):
        store.add(fact())
        with pytest.raises(ValueError, match="timezone-aware"):
            store.supersede(RWY, "lda_m", datetime(2026, 9, 15))

    def test_superseding_an_already_closed_key_is_a_no_op(self, store):
        store.add(fact())
        store.supersede(RWY, "lda_m", datetime(2026, 9, 15, tzinfo=timezone.utc))
        assert store.supersede(RWY, "lda_m", datetime(2026, 9, 20, tzinfo=timezone.utc)) == 0


class TestResolution:
    """The CES is delegated, not reimplemented in SQL.

    Two implementations of the layering rule that could disagree would be the
    worst place in this system for a divergence, so the store loads the rows
    and hands them to the tested resolver.
    """

    @pytest.fixture
    def layered(self, store):
        store.extend([
            fact(value=3900),
            fact(value=3500, valid_from=date(2026, 6, 1), valid_to=date(2026, 9, 20),
                 precedence=Precedence.SUP, source=ref(document="AIP SUP 04/26")),
            fact(value=3100, valid_from=date(2026, 8, 1), valid_to=date(2026, 8, 31),
                 precedence=Precedence.NOTAM, source=ref(document="NOTAM A1234/26")),
        ])
        return store

    def test_the_highest_layer_in_force_wins(self, layered):
        assert layered.effective(RWY, "lda_m", date(2026, 8, 15)).value == 3100
        assert layered.effective(RWY, "lda_m", date(2026, 9, 15)).value == 3500
        assert layered.effective(RWY, "lda_m", date(2026, 10, 1)).value == 3900

    def test_the_stack_is_the_receipt(self, layered):
        stack = layered.stack(RWY, "lda_m", date(2026, 8, 15))
        assert [(f.precedence.name, f.value) for f in stack] == [
            ("NOTAM", 3100), ("SUP", 3500), ("AIP", 3900),
        ]
        assert [f.source.document for f in stack] == [
            "NOTAM A1234/26", "AIP SUP 04/26", "AIP AD 2.13",
        ]

    def test_it_agrees_with_the_in_memory_store_exactly(self, layered):
        memory = FactStore(list(layered))
        for day in (date(2026, 5, 1), date(2026, 8, 15), date(2026, 9, 15), date(2026, 10, 1)):
            a = layered.effective(RWY, "lda_m", day)
            b = memory.effective(RWY, "lda_m", day)
            assert (a.value if a else None) == (b.value if b else None), day


class TestQueries:
    def test_only_the_entity_asked_about_is_loaded(self, store):
        store.extend([
            fact(),
            fact(entity="OTHH", attribute="rffs_category", value=9),
            fact(entity="OTBD/RWY15", value=4570),
        ])
        view = store.for_entity("OTHH")
        assert view.entities() == {"OTHH", RWY}
        assert "OTBD/RWY15" not in view.entities()

    def test_the_entity_view_normalises_like_everything_else(self, store):
        store.add(fact())
        assert store.for_entity(" othh ").entities() == {RWY}

    def test_a_prefix_that_is_not_a_path_segment_is_excluded(self, store):
        # The LIKE clause is a coarse filter; entities.covers is the rule.
        store.extend([fact(), fact(entity="OTHHX", value=1)])
        assert "OTHHX" not in store.for_entity("OTHH").entities()

    def test_entities_and_attributes_come_from_the_database(self, store):
        store.extend([fact(), fact(attribute="tora_m", value=4250)])
        assert store.entities() == {RWY}
        assert store.attributes(RWY) == {"lda_m", "tora_m"}

    def test_history_is_in_the_order_we_learned_it(self, store):
        store.extend([
            fact(value=3900),
            fact(value=3500, recorded_at=KNOWN + timedelta(days=1)),
        ])
        assert [f.value for f in store.history(RWY, "lda_m")] == [3900, 3500]


class TestIntegrity:
    def test_a_citation_the_archive_cannot_resolve_is_findable(self, store, tmp_path):
        # A citation that does not resolve is not a citation. This catches an
        # archive restored from a stale backup, or facts imported without the
        # documents behind them.
        from aeropub.archive import Archive

        archive = Archive(tmp_path / "raw")
        body = b"<AD 2.13>"
        entry = archive.put(body, source_id="QA-CAA", url="https://x/y",
                            retrieved_at=KNOWN)

        store.extend([
            fact(source=ref(content_hash=entry.digest)),
            fact(attribute="tora_m", value=4250, source=ref(content_hash="c" * 64)),
        ])
        assert store.unarchived(archive) == ("c" * 64,)

    def test_statistics_count_what_is_held(self, store):
        store.extend([
            fact(),
            fact(attribute="tora_m", value=4250, source=ref(content_hash="c" * 64)),
        ])
        assert store.statistics() == {
            "facts": 2, "entities": 1, "documents": 2, "superseded": 0
        }

    def test_the_schema_version_is_recorded(self, store):
        assert store.schema_version == SCHEMA_VERSION

    def test_reopening_the_same_file_finds_everything(self, tmp_path):
        path = tmp_path / "aeropub.db"
        with open_store(path) as first:
            first.add(fact())
        with open_store(path) as second:
            assert len(second) == 1
            assert second.effective(RWY, "lda_m", date(2026, 6, 1)).value == 3900

    def test_the_parent_directory_is_created(self, tmp_path):
        with open_store(tmp_path / "nested" / "deep" / "aeropub.db") as opened:
            opened.add(fact())
            assert len(opened) == 1


class TestDropIn:
    """The point of the design: analysis code does not know which store it has."""

    @pytest.fixture
    def loaded(self, store):
        store.extend([
            fact(value=3900),
            fact(value=3500, valid_from=date(2026, 9, 1), valid_to=date(2026, 9, 20),
                 precedence=Precedence.SUP, source=ref(document="AIP SUP 04/26")),
            fact(entity="OTHH", attribute="rffs_category", value=9,
                 source=ref(locator="OTHH", document="AIP AD 2.6")),
        ])
        return store

    def test_a_dossier_builds_from_the_database(self, loaded):
        from aeropub.dossier import build

        dossier = build("OTHH", facts=loaded.for_entity("OTHH"),
                        as_at=datetime(2026, 9, 10, tzinfo=timezone.utc))
        assert dossier.section("AD 2.13").values[0].value == 3500
        assert dossier.section("AD 2.6").values[0].value == 9

    def test_a_bulletin_compiles_from_the_database(self, loaded):
        from aeropub.bulletin import compile_bulletin

        bulletin = compile_bulletin(loaded.for_entity("OTHH"), "OTHH",
                                    date(2026, 8, 1), date(2026, 9, 10))
        assert [c.attribute for c in bulletin.changes] == ["lda_m"]
        assert bulletin.changes[0].change.to_value == 3500

    def test_the_forward_view_reads_from_the_database(self, loaded):
        from aeropub.horizon import Trigger, horizon

        ahead = horizon(loaded.for_entity("OTHH"), "OTHH",
                        from_date=date(2026, 9, 10), days=60)
        assert [t.trigger for t in ahead.transitions] == [Trigger.REVERSION]
        assert ahead.transitions[0].on == date(2026, 9, 21)
