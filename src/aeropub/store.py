"""Durable storage — SQLite, and an invariant the database enforces itself.

The fact model is bitemporal and append-only: a value is never edited, only
covered by a later one, and what we believed on the day of an event has to
remain answerable years afterwards. Held in memory that is a convention.
Written to disk it becomes a promise, and a promise kept only by every future
caller remembering to keep it is not a promise.

So the schema enforces it. ``DELETE`` on the fact table always aborts, and
``UPDATE`` aborts unless it is doing the one legitimate mutation — setting
``superseded_at`` on a row that has not been superseded yet, changing nothing
else. A migration script, a debugging session or a future maintainer cannot
quietly rewrite history: SQLite refuses.

Why SQLite
----------
One file, no daemon, no ops, and it runs the same on a laptop and a small VM.
The heavy part of this system is the content-addressed archive, which is
already files on disk; the relational side is metadata and windows. Postgres
becomes the right answer when several airlines share one instance — and the
fact model does not change when it does, because everything above this module
talks to :class:`FactSource`, not to a database.

What is stored, and what is not
-------------------------------
Facts and their provenance. **Not** the documents: those live in the archive
under their content hash, which each row carries. A row without its archive is
still a complete citation of something you can go and check — the hash is what
proves the document you find is the one that was parsed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from aeropub.facts import Fact, Precedence
from aeropub.provenance import Confidence, SourceRef

__all__ = ["SCHEMA_VERSION", "SqliteFactStore", "open_store"]

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
-- A watcher writing every minute and a report reading at the same time is the
-- normal case, not the exception. Without this the reader fails instantly on a
-- lock it would have got a few milliseconds later.
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    id             INTEGER PRIMARY KEY,
    entity         TEXT    NOT NULL,
    attribute      TEXT    NOT NULL,
    value_json     TEXT    NOT NULL,
    valid_from     TEXT    NOT NULL,
    valid_to       TEXT,
    precedence     INTEGER NOT NULL,
    recorded_at    TEXT    NOT NULL,
    superseded_at  TEXT,
    source_id      TEXT    NOT NULL,
    document       TEXT    NOT NULL,
    locator        TEXT    NOT NULL,
    retrieved_at   TEXT    NOT NULL,
    content_hash   TEXT    NOT NULL,
    parser_id      TEXT    NOT NULL,
    parser_version TEXT    NOT NULL,
    confidence     TEXT    NOT NULL,
    published_at   TEXT,
    original_url   TEXT,
    archive_key    TEXT
);

CREATE INDEX IF NOT EXISTS facts_key    ON facts(entity, attribute);
CREATE INDEX IF NOT EXISTS facts_entity ON facts(entity);
CREATE INDEX IF NOT EXISTS facts_hash   ON facts(content_hash);

-- The archive is never pruned and neither is this. A fact that was believed
-- and later corrected is evidence about what we told somebody at the time,
-- which is exactly what an investigation asks for.
CREATE TRIGGER IF NOT EXISTS facts_no_delete
BEFORE DELETE ON facts
BEGIN
    SELECT RAISE(ABORT,
        'facts are append-only: supersede the row, never delete it');
END;

-- The one legitimate mutation: closing a row's transaction time, once.
CREATE TRIGGER IF NOT EXISTS facts_append_only
BEFORE UPDATE ON facts
BEGIN
    SELECT RAISE(ABORT,
        'facts are append-only: only superseded_at may be set, and only once')
    WHERE OLD.superseded_at IS NOT NULL
       OR NEW.superseded_at IS NULL
       OR NEW.entity       IS NOT OLD.entity
       OR NEW.attribute    IS NOT OLD.attribute
       OR NEW.value_json   IS NOT OLD.value_json
       OR NEW.valid_from   IS NOT OLD.valid_from
       OR NEW.valid_to     IS NOT OLD.valid_to
       OR NEW.precedence   IS NOT OLD.precedence
       OR NEW.recorded_at  IS NOT OLD.recorded_at
       OR NEW.content_hash IS NOT OLD.content_hash;
END;
"""

_COLUMNS = (
    "entity, attribute, value_json, valid_from, valid_to, precedence, "
    "recorded_at, superseded_at, source_id, document, locator, retrieved_at, "
    "content_hash, parser_id, parser_version, confidence, published_at, "
    "original_url, archive_key"
)


def _dump_value(value: Any, entity: str, attribute: str) -> str:
    """Serialise a fact's value, refusing anything that would not survive.

    Storing ``str(value)`` for an unserialisable type would round-trip a number
    back as text and quietly break every comparison downstream, including the
    one that decides whether something changed.
    """
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"{entity} {attribute}: a fact value must survive a round trip through "
            f"the store, and {type(value).__name__} does not ({exc}). Convert it "
            "to a plain type at the parser, where the meaning is still known."
        ) from None


def _row_to_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        entity=row["entity"],
        attribute=row["attribute"],
        value=json.loads(row["value_json"]),
        valid_from=date.fromisoformat(row["valid_from"]),
        valid_to=date.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
        precedence=Precedence(row["precedence"]),
        recorded_at=datetime.fromisoformat(row["recorded_at"]),
        superseded_at=(
            datetime.fromisoformat(row["superseded_at"]) if row["superseded_at"] else None
        ),
        source=SourceRef(
            source_id=row["source_id"],
            document=row["document"],
            locator=row["locator"],
            retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
            content_hash=row["content_hash"],
            parser_id=row["parser_id"],
            parser_version=row["parser_version"],
            confidence=Confidence(row["confidence"]),
            published_at=(
                date.fromisoformat(row["published_at"]) if row["published_at"] else None
            ),
            original_url=row["original_url"],
            archive_key=row["archive_key"],
        ),
    )


def _fact_to_row(fact: Fact) -> tuple:
    ref = fact.source
    return (
        fact.entity,
        fact.attribute,
        _dump_value(fact.value, fact.entity, fact.attribute),
        fact.valid_from.isoformat(),
        fact.valid_to.isoformat() if fact.valid_to else None,
        int(fact.precedence),
        fact.recorded_at.isoformat(),
        fact.superseded_at.isoformat() if fact.superseded_at else None,
        ref.source_id,
        ref.document,
        ref.locator,
        ref.retrieved_at.isoformat(),
        ref.content_hash,
        ref.parser_id,
        ref.parser_version,
        ref.confidence.value,
        ref.published_at.isoformat() if ref.published_at else None,
        ref.original_url,
        ref.archive_key,
    )


class SqliteFactStore:
    """A :class:`~aeropub.facts.FactSource` backed by one SQLite file.

    Resolution — the CES stack, effective values, transaction-time travel — is
    delegated to :class:`~aeropub.facts.FactStore` rather than reimplemented in
    SQL. That logic is the heart of the product and is heavily tested; having
    two implementations of it that could disagree would be the worst possible
    place for a divergence.

    Queries are pushed down to the database, so only the facts for the entity
    being asked about are loaded. A country's worth of NOTAM does not have to
    be in memory to answer a question about one runway.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # timeout covers the connect and every statement; the PRAGMA above
        # covers the rest of the session.
        self._connection = sqlite3.connect(
            str(self.path), isolation_level=None, timeout=5.0
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)
        self._connection.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SqliteFactStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN")
        try:
            yield self._connection
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    # -- writing ---------------------------------------------------------

    def add(self, fact: Fact) -> None:
        """Append one fact. Never replaces an existing row."""
        self.extend((fact,))

    def extend(self, facts: Iterable[Fact]) -> None:
        """Append many facts in one transaction.

        All or nothing: a parser that fails halfway through an AD 2 section
        must not leave half a section in the store looking complete.
        """
        rows = [_fact_to_row(f) for f in facts]
        if not rows:
            return
        placeholders = ", ".join(["?"] * 19)
        with self._transaction() as connection:
            connection.executemany(
                f"INSERT INTO facts ({_COLUMNS}) VALUES ({placeholders})", rows
            )

    def supersede(self, entity: str, attribute: str, at: datetime) -> int:
        """Close the transaction time of every current row for a key.

        Used when a source is re-read and the previous extraction is known to
        be wrong. The rows stay: what we believed, and when we stopped, is the
        audit answer.
        """
        if at.tzinfo is None:
            raise ValueError("supersede(at=) must be timezone-aware (UTC)")
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE facts SET superseded_at = ? "
                "WHERE entity = ? AND attribute = ? AND superseded_at IS NULL",
                (at.isoformat(), entity, attribute),
            )
            return cursor.rowcount

    # -- reading ---------------------------------------------------------

    def _query(self, sql: str, params: tuple = ()) -> list[Fact]:
        return [_row_to_fact(r) for r in self._connection.execute(sql, params)]

    def __len__(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"])

    def __iter__(self) -> Iterator[Fact]:
        yield from self._query(f"SELECT {_COLUMNS} FROM facts ORDER BY id")

    def entities(self) -> set[str]:
        return {r["entity"] for r in self._connection.execute("SELECT DISTINCT entity FROM facts")}

    def attributes(self, entity: str) -> set[str]:
        return {
            r["attribute"]
            for r in self._connection.execute(
                "SELECT DISTINCT attribute FROM facts WHERE entity = ?", (entity,)
            )
        }

    def _for_key(self, entity: str, attribute: str):
        from aeropub.facts import FactStore

        return FactStore(
            self._query(
                f"SELECT {_COLUMNS} FROM facts WHERE entity = ? AND attribute = ?",
                (entity, attribute),
            )
        )

    def stack(self, entity: str, attribute: str, on: date, **kwargs) -> list[Fact]:
        """Every layer in force, highest precedence first. The receipt."""
        return self._for_key(entity, attribute).stack(entity, attribute, on, **kwargs)

    def effective(self, entity: str, attribute: str, on: date, **kwargs) -> Fact | None:
        """The operationally true value, or ``None`` — a coverage gap."""
        return self._for_key(entity, attribute).effective(entity, attribute, on, **kwargs)

    def history(self, entity: str, attribute: str) -> list[Fact]:
        """Everything ever recorded for a key, in the order we learned it."""
        return self._for_key(entity, attribute).history(entity, attribute)

    def for_entity(self, entity: str) -> "FactStoreLike":
        """An in-memory view of one entity and everything beneath it.

        The dossier, bulletin and forward view walk an aerodrome's whole set
        several times over; loading it once is both faster and simpler than
        pushing each of their traversals into SQL.
        """
        from aeropub.entities import covers, normalise
        from aeropub.facts import FactStore

        key = normalise(entity)
        return FactStore(
            f for f in self._query(
                f"SELECT {_COLUMNS} FROM facts WHERE entity = ? OR entity LIKE ?",
                (key, f"{key}/%"),
            )
            if covers(key, f.entity)
        )

    # -- housekeeping ----------------------------------------------------

    def statistics(self) -> dict[str, int]:
        """What the store holds. For the status board, not for reassurance."""
        row = self._connection.execute(
            "SELECT COUNT(*) AS facts, COUNT(DISTINCT entity) AS entities, "
            "COUNT(DISTINCT content_hash) AS documents, "
            "SUM(superseded_at IS NOT NULL) AS superseded FROM facts"
        ).fetchone()
        return {
            "facts": row["facts"] or 0,
            "entities": row["entities"] or 0,
            "documents": row["documents"] or 0,
            "superseded": row["superseded"] or 0,
        }

    def unarchived(self, archive) -> tuple[str, ...]:
        """Content hashes cited by facts that the archive does not hold.

        A citation that cannot be resolved is not a citation. This is the check
        that catches an archive restored from a stale backup, or a fact loaded
        from an export whose documents were left behind.
        """
        hashes = {
            r["content_hash"]
            for r in self._connection.execute("SELECT DISTINCT content_hash FROM facts")
        }
        return tuple(sorted(h for h in hashes if not archive.has(h)))


#: What every consumer actually needs. Both stores satisfy it.
FactStoreLike = Any


def open_store(path: Path | str) -> SqliteFactStore:
    """Open or create the fact store at ``path``."""
    return SqliteFactStore(path)
