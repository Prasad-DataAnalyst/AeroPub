"""What has been read, kept across restarts.

:class:`~aeropub.cycle.InMemoryLedger` forgets when the process does, so every
restart re-reads every AIP: slow, and rude to a hundred and eighty States whose
servers gain nothing from being asked again. This is the durable one.

The hazard it exists to prevent
-------------------------------
A ledger is a claim about what we hold, and a claim can outlive the thing it
describes. Lose the archive — a disk replaced, a volume not restored, a
directory moved — while the ledger survives, and every document answers
UNCHANGED forever. The cycle reports every State complete, every citation
resolves to a key that is not there, and nothing anywhere says so. Total data
loss, reported as health.

So the ledger records the archive key beside the hash, and :meth:`reconcile`
checks the archive actually holds each one. Anything missing is forgotten, so
the next cycle reads it again. That check is cheap and belongs at startup: it
is the only thing standing between a restore that silently dropped the archive
and a platform that believes it has an AIP it does not.

What it is not
--------------
Not the fact store, which is bitemporal and append-only and refuses to forget.
This is operational state — a hash is superseded when a page changes, and
rewriting it is the normal case rather than a violation. Two different jobs,
kept in two tables even when they share a file.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

__all__ = ["LedgerEntry", "Reconciliation", "SqliteLedger"]

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS seen (
    url            TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL,
    archive_key    TEXT,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    reads          INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS seen_hash ON seen(content_hash);

CREATE TABLE IF NOT EXISTS state_health (
    state                TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failed_at       TEXT,
    last_succeeded_at    TEXT
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One document we have read, and what we kept of it."""

    url: str
    content_hash: str
    archive_key: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    reads: int
    """How many times this URL has been read with *different* content.

    Not how often it was checked. A section read once and confirmed unchanged
    on four hundred cycles still reads 1, because that is how many versions of
    it we hold.
    """

    @property
    def claims_a_copy(self) -> bool:
        return bool(self.archive_key)


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What the ledger claimed against what the archive actually holds."""

    checked: int = 0
    present: int = 0
    missing: tuple[LedgerEntry, ...] = ()
    unarchived: tuple[LedgerEntry, ...] = ()
    """Entries that never claimed a copy. Not a fault in themselves — a
    document read before an archive was configured — but they are why the
    next cycle will read them again, so they are named rather than counted
    among the healthy."""

    forgotten: int = 0

    @property
    def is_sound(self) -> bool:
        """Whether every claim the ledger made is backed by an archived copy."""
        return not self.missing and not self.unarchived

    def describe(self) -> str:
        lines = [
            f"LEDGER RECONCILIATION — {self.checked} entries",
            f"  {self.present} backed by an archived copy",
        ]
        if self.unarchived:
            lines.append(
                f"  {len(self.unarchived)} never archived — the next cycle "
                "will read them again"
            )
        if self.missing:
            lines += [
                "",
                f"  {len(self.missing)} CLAIMED A COPY THE ARCHIVE DOES NOT HOLD",
                "  Every one of these would have answered UNCHANGED forever "
                "while holding\n  nothing. This is what a restore that dropped "
                "the archive looks like.",
            ]
            for entry in self.missing[:10]:
                lines.append(f"    {entry.url}")
            if len(self.missing) > 10:
                lines.append(f"    and {len(self.missing) - 10} more")
        if self.forgotten:
            lines.append(
                f"\n  {self.forgotten} forgotten, so the next cycle reads them.\n"
                "  The transport's validators must be dropped for the same "
                "URLs, or it\n  will send an ETag, the server will answer 304, "
                "and nothing will be read."
            )
        elif self.is_sound:
            lines.append("\n  Sound: every claim is backed.")
        return "\n".join(lines)


@dataclass
class SqliteLedger:
    """A ledger that survives the process. Satisfies :class:`~aeropub.cycle.Ledger`."""

    path: Path | str = ":memory:"

    def __post_init__(self) -> None:
        target = str(self.path)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(target, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    # -- the Ledger protocol ----------------------------------------------

    def hash_for(self, url: str) -> str | None:
        row = self._connection.execute(
            "SELECT content_hash FROM seen WHERE url = ?", (url,)
        ).fetchone()
        return row["content_hash"] if row else None

    def record(
        self,
        url: str,
        content_hash: str,
        at: datetime,
        *,
        archive_key: str | None = None,
    ) -> None:
        """Note that this URL now holds this content.

        ``archive_key`` is what makes :meth:`reconcile` possible. Recording a
        hash without one says we read the document and kept nothing, which is
        true for a chart and a problem for a section — so it is stored as
        given rather than defaulted, and reconciliation reports it.
        """
        now = at.isoformat()
        self._connection.execute(
            """
            INSERT INTO seen (url, content_hash, archive_key,
                              first_seen_at, last_seen_at, reads)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(url) DO UPDATE SET
                content_hash = excluded.content_hash,
                archive_key  = excluded.archive_key,
                last_seen_at = excluded.last_seen_at,
                reads        = seen.reads + 1
            WHERE seen.content_hash <> excluded.content_hash
            """,
            (url, content_hash, archive_key, now, now),
        )
        self._connection.commit()

    def failures_for(self, state: str) -> int:
        row = self._connection.execute(
            "SELECT consecutive_failures FROM state_health WHERE state = ?", (state,)
        ).fetchone()
        return row["consecutive_failures"] if row else 0

    def note_state(self, state: str, *, failed: bool) -> None:
        now = _utcnow().isoformat()
        if failed:
            self._connection.execute(
                """
                INSERT INTO state_health (state, consecutive_failures, last_failed_at)
                VALUES (?, 1, ?)
                ON CONFLICT(state) DO UPDATE SET
                    consecutive_failures = state_health.consecutive_failures + 1,
                    last_failed_at = excluded.last_failed_at
                """,
                (state, now),
            )
        else:
            self._connection.execute(
                """
                INSERT INTO state_health (state, consecutive_failures,
                                          last_succeeded_at)
                VALUES (?, 0, ?)
                ON CONFLICT(state) DO UPDATE SET
                    consecutive_failures = 0,
                    last_succeeded_at = excluded.last_succeeded_at
                """,
                (state, now),
            )
        self._connection.commit()

    # -- beyond the protocol ----------------------------------------------

    def entry_for(self, url: str) -> LedgerEntry | None:
        row = self._connection.execute(
            "SELECT * FROM seen WHERE url = ?", (url,)
        ).fetchone()
        return _entry(row) if row else None

    def entries(self) -> Iterator[LedgerEntry]:
        for row in self._connection.execute("SELECT * FROM seen ORDER BY url"):
            yield _entry(row)

    def forget(self, url: str) -> None:
        """Drop what we know of this URL, so the next cycle reads it again."""
        self._connection.execute("DELETE FROM seen WHERE url = ?", (url,))
        self._connection.commit()

    def reconcile(
        self,
        holds: Callable[[str], bool],
        *,
        forget_missing: bool = True,
        on_forget: Callable[[str], None] | None = None,
    ) -> Reconciliation:
        """Check every claim against what the archive actually holds.

        ``holds`` answers whether an archive key resolves. Anything it denies
        is forgotten by default, because a ledger entry claiming a copy that is
        gone is worse than no entry at all: no entry costs one re-read, and a
        false claim costs every future cycle.

        ``on_forget`` must be given the transport's ``forget``, and the reason
        is the whole point of reconciling. Forgetting here makes the *cycle*
        want the document again; it does nothing to the *transport*, which
        still holds an ETag for that URL and will send it. The server answers
        304, the reader reports unchanged, nothing is archived, and the
        re-read that reconciliation exists to force never happens. Two caches,
        and clearing one of them is worse than clearing neither — it looks
        fixed.
        """
        checked = present = forgotten = 0
        missing: list[LedgerEntry] = []
        unarchived: list[LedgerEntry] = []

        for entry in list(self.entries()):
            checked += 1
            if not entry.claims_a_copy:
                unarchived.append(entry)
                continue
            if holds(entry.archive_key):
                present += 1
                continue
            missing.append(entry)
            if forget_missing:
                self.forget(entry.url)
                if on_forget is not None:
                    on_forget(entry.url)
                forgotten += 1

        return Reconciliation(
            checked=checked,
            present=present,
            missing=tuple(missing),
            unarchived=tuple(unarchived),
            forgotten=forgotten,
        )


def _entry(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        url=row["url"],
        content_hash=row["content_hash"],
        archive_key=row["archive_key"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        reads=row["reads"],
    )
