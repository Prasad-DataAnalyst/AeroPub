"""Reading an AIP table, which is mostly the story of one HTML attribute.

An eAIP page is tables. So is a PDF once it has been converted, and so is
anything pasted out of one. This module turns them into the manifest rows the
loaders already read, and it exists mainly because of ``rowspan``.

The attribute that decides whether the data is right
-----------------------------------------------------
An ENR 3 table does not repeat the route designator on every line. It writes
``UM688`` once, in a cell spanning the six segment rows beneath it::

    ┌────────┬─────────┬─────────┬──────┬──────┐
    │ Route  │ From    │ To      │ MEA  │ MAA  │
    ├────────┼─────────┼─────────┼──────┼──────┤
    │        │ ALSEM   │ MIDLE   │ 24500│ 46000│
    │ UM688  ├─────────┼─────────┼──────┼──────┤   ← one cell, rowspan 6
    │        │ MIDLE   │ KUKLA   │ 26000│ 46000│
    └────────┴─────────┴─────────┴──────┴──────┘

A reader that walks ``<tr>`` and takes cells in order gets ``UM688`` on the
first segment and then shifts every later row one column left: the second
segment's route becomes ``MIDLE``, its start becomes ``KUKLA``, and its
minimum en-route altitude becomes a designator. Nothing about that output looks
wrong. It parses, it loads, it draws. It is entirely fiction.

So every table here is expanded into a rectangular grid first, with a spanned
value repeated into each cell it covers. Whatever comes out has one value per
column per row, or the table is reported as ragged and nothing is emitted.

Columns are named, never guessed
---------------------------------
A header is matched exactly, after collapsing whitespace and case, or a column
is given by index. There is no fuzzy match, no "closest header", no scoring.
The failure mode of a fuzzy match here is silent: ``MEA`` and ``MAA`` differ by
one letter and by twenty thousand feet, and a reader that picked the wrong one
would produce a manifest that is correct in every respect except the numbers.

A header the mapping names and the table does not have is an error. A header
appearing twice is an error. Both are recoverable by a person in ten seconds
and unrecoverable by anybody once the data is downstream.

An ENR 3 table is points, not segments
---------------------------------------
The other thing about ENR 3 that a column mapping alone cannot express: the
table lists one *significant point* per row, and a segment is the gap between
two consecutive rows. ALSEM, MIDLE, KUKLA is three rows and two segments.
:func:`pair_points` does that pairing, within each route.

It cannot infer one thing, and refuses to: whether the track, distance and
limits printed on a row describe the leg *arriving* at that point or the leg
*leaving* it. States publish both conventions. Guessing puts every distance on
the wrong segment — an error of one leg, invisible in the output, and wrong all
the way down the route. So the caller says which, and the answer is recorded on
every row that results.

What this does not do
---------------------
It does not decide what a column means. The mapping is supplied — by a person
reading the page, or drafted by :mod:`aeropub.eaip.probe` from the page's own
headers and then checked. This module applies a mapping and reports what it
could not apply; it never infers one.

It does not convert values either, beyond stripping whitespace. A coordinate
stays the text the AIP printed, and the manifest loader parses it with the same
reader everything else uses — so a cell that cannot be read fails in one place,
with one message, naming the row of the AIP it came from.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable, Mapping, Sequence

__all__ = [
    "Cell",
    "ColumnError",
    "Table",
    "TableError",
    "map_rows",
    "pair_points",
    "read_tables",
    "resolve_columns",
]

#: Tags whose text is never part of a cell.
_IGNORED = {"script", "style"}

#: Tags that mean a line break inside a cell rather than a space.
_BREAKS = {"br", "p", "div", "li", "tr"}


class TableError(ValueError):
    """A table could not be read as a table."""


class ColumnError(TableError):
    """A mapping names a column the table does not have, or names it twice."""


@dataclass(frozen=True, slots=True)
class Cell:
    """One cell, with the spans it was published with."""

    text: str = ""
    colspan: int = 1
    rowspan: int = 1
    header: bool = False


def _normalise(text: str) -> str:
    """Collapse whitespace, including the non-breaking kind AIPs are full of."""
    return " ".join(str(text).replace("\xa0", " ").split())


class _TableReader(HTMLParser):
    """Collect every table in the document, cells and spans intact.

    Nested tables are collected as tables in their own right *and* their text
    stays in the containing cell. An eAIP puts a table inside a cell to lay out
    a pair of values, and both readings are useful: the outer one keeps the row
    whole, the inner one is addressable when the pair is what you want.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[Cell]]] = []
        self.captions: list[str] = []
        self._open: list[dict] = []
        self._ignoring = 0

    # -- structure -------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        got = {k: (v or "") for k, v in attrs}
        if tag in _IGNORED:
            self._ignoring += 1
            return
        if tag == "table":
            self._open.append({"rows": [], "row": None, "cell": None, "caption": []})
            return
        if not self._open:
            return
        top = self._open[-1]
        if tag == "tr":
            self._close_cell()
            top["row"] = []
        elif tag in ("td", "th"):
            self._close_cell()
            if top["row"] is None:
                # A cell outside any row. Malformed, and still evidence.
                top["row"] = []
            top["cell"] = {
                "parts": [],
                "colspan": _span(got.get("colspan")),
                "rowspan": _span(got.get("rowspan")),
                "header": tag == "th",
            }
        elif tag == "caption":
            top["cell"] = {"parts": [], "colspan": 1, "rowspan": 1, "header": False,
                           "caption": True}
        elif tag in _BREAKS and top["cell"] is not None:
            top["cell"]["parts"].append(" ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _BREAKS and self._open and self._open[-1]["cell"] is not None:
            self._open[-1]["cell"]["parts"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _IGNORED:
            self._ignoring = max(0, self._ignoring - 1)
            return
        if not self._open:
            return
        top = self._open[-1]
        if tag in ("td", "th", "caption"):
            self._close_cell()
        elif tag == "tr":
            self._close_cell()
            if top["row"] is not None:
                top["rows"].append(top["row"])
                top["row"] = None
        elif tag == "table":
            self._close_cell()
            if top["row"]:
                top["rows"].append(top["row"])
            done = self._open.pop()
            self.tables.append(done["rows"])
            self.captions.append(_normalise("".join(done["caption"])))
            # A nested table's text belongs to the cell that contains it too.
            if self._open and self._open[-1]["cell"] is not None:
                flattened = " ".join(
                    c.text for row in done["rows"] for c in row if c.text
                )
                self._open[-1]["cell"]["parts"].append(" " + flattened + " ")

    def handle_data(self, data: str) -> None:
        if self._ignoring or not self._open:
            return
        cell = self._open[-1]["cell"]
        if cell is not None:
            cell["parts"].append(data)

    def _close_cell(self) -> None:
        if not self._open:
            return
        top = self._open[-1]
        cell = top.get("cell")
        if cell is None:
            return
        top["cell"] = None
        text = _normalise("".join(cell["parts"]))
        if cell.get("caption"):
            top["caption"].append(text)
            return
        if top["row"] is None:
            top["row"] = []
        top["row"].append(
            Cell(
                text=text,
                colspan=cell["colspan"],
                rowspan=cell["rowspan"],
                header=cell["header"],
            )
        )

    def close(self) -> None:  # pragma: no cover - exercised through read_tables
        super().close()
        # Anything still open at the end of a malformed document is still a
        # table somebody wrote, and dropping it would lose rows silently.
        while self._open:
            self._close_cell()
            top = self._open.pop()
            if top["row"]:
                top["rows"].append(top["row"])
            self.tables.append(top["rows"])
            self.captions.append(_normalise("".join(top["caption"])))


def _span(value: str | None) -> int:
    """A span attribute, or 1. Never zero: a zero-span cell would vanish."""
    try:
        found = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return found if found >= 1 else 1


@dataclass(frozen=True, slots=True)
class Table:
    """One table, expanded to a rectangle with spans filled in."""

    index: int
    grid: tuple[tuple[str, ...], ...] = ()
    header_rows: int = 0
    caption: str = ""
    ragged: tuple[int, ...] = ()
    """Rows whose width did not match the table's. Reported rather than padded:
    a padded row silently shifts every value in it."""

    @property
    def width(self) -> int:
        return len(self.grid[0]) if self.grid else 0

    @property
    def headers(self) -> tuple[str, ...]:
        """The header row, joined down the header block.

        An eAIP header is often two rows — a group name over its columns — and
        the useful name is the pair. Joined with a space, in reading order.
        """
        if not self.header_rows:
            return ()
        columns = []
        for index in range(self.width):
            parts = [
                self.grid[row][index]
                for row in range(self.header_rows)
                if self.grid[row][index]
            ]
            # A header that spans its group repeats the group name into every
            # column; saying it twice helps nobody.
            seen: list[str] = []
            for part in parts:
                if part not in seen:
                    seen.append(part)
            columns.append(" ".join(seen))
        return tuple(columns)

    @property
    def body(self) -> tuple[tuple[str, ...], ...]:
        return self.grid[self.header_rows :]

    def describe(self) -> str:
        parts = [f"table {self.index}"]
        if self.caption:
            parts.append(self.caption)
        parts.append(f"{len(self.body)} rows × {self.width} columns")
        if self.ragged:
            parts.append(f"{len(self.ragged)} ragged rows")
        return "  ·  ".join(parts)


def _expand(rows: Sequence[Sequence[Cell]]) -> tuple[list[list[str]], list[int]]:
    """Lay cells into a rectangular grid, repeating spans into what they cover.

    This is the whole point of the module. A ``rowspan`` route designator is
    written into every segment row it covers, so the row that reaches a caller
    is the row the AIP means rather than the row the markup happens to hold.
    """
    grid: list[list[str]] = []
    # Values still descending from a rowspan above, by column.
    pending: dict[int, tuple[str, int]] = {}

    for row in rows:
        line: dict[int, str] = {}
        # Anything spanning down from an earlier row occupies its column first.
        for column, (text, left) in list(pending.items()):
            line[column] = text
            if left <= 1:
                del pending[column]
            else:
                pending[column] = (text, left - 1)

        at = 0
        for cell in row:
            while at in line:
                at += 1
            for offset in range(cell.colspan):
                line[at + offset] = cell.text
                if cell.rowspan > 1:
                    pending[at + offset] = (cell.text, cell.rowspan - 1)
            at += cell.colspan
        if line:
            width = max(line) + 1
            grid.append([line.get(i, "") for i in range(width)])

    if not grid:
        return [], []
    width = max(len(line) for line in grid)
    ragged = [i for i, line in enumerate(grid) if len(line) != width]
    for line in grid:
        line.extend([""] * (width - len(line)))
    return grid, ragged


def _count_header_rows(rows: Sequence[Sequence[Cell]]) -> int:
    """How many leading rows are header.

    A row is header if every cell in it is a ``<th>``. Nothing cleverer: an
    eAIP that marks its headers as data cells gets zero header rows and a
    caller who addresses columns by index, which is honest.
    """
    count = 0
    for row in rows:
        if row and all(cell.header for cell in row):
            count += 1
            continue
        break
    return count


def read_tables(html: str) -> tuple[Table, ...]:
    """Every table in the document, in order, expanded to a rectangle."""
    reader = _TableReader()
    reader.feed(html or "")
    reader.close()
    found: list[Table] = []
    for index, rows in enumerate(reader.tables):
        grid, ragged = _expand(rows)
        found.append(
            Table(
                index=index,
                grid=tuple(tuple(line) for line in grid),
                header_rows=_count_header_rows(rows),
                caption=reader.captions[index] if index < len(reader.captions) else "",
                ragged=tuple(ragged),
            )
        )
    return tuple(found)


# --------------------------------------------------------------------------
# Applying a mapping
# --------------------------------------------------------------------------


def _key(text: str) -> str:
    return _normalise(text).casefold()


def resolve_columns(
    table: Table, mapping: Mapping[str, str | int]
) -> dict[str, int]:
    """Turn ``field -> header or index`` into ``field -> column``.

    Exact after collapsing whitespace and case, or an index. No fuzzy match:
    ``MEA`` and ``MAA`` differ by one letter and twenty thousand feet, and a
    reader that scored headers would pick one of them silently.
    """
    if table.width == 0:
        raise TableError(f"table {table.index} has no columns")

    by_header: dict[str, list[int]] = {}
    for column, header in enumerate(table.headers):
        by_header.setdefault(_key(header), []).append(column)

    resolved: dict[str, int] = {}
    for field_name, wanted in mapping.items():
        if isinstance(wanted, int) or (
            isinstance(wanted, str) and re.fullmatch(r"-?\d+", wanted.strip())
        ):
            column = int(wanted)
            if not 0 <= column < table.width:
                raise ColumnError(
                    f"table {table.index}: column {column} for {field_name!r} is "
                    f"outside the table, which has {table.width} columns"
                )
            resolved[field_name] = column
            continue

        found = by_header.get(_key(str(wanted)), [])
        if not found:
            available = ", ".join(h for h in table.headers if h) or "none"
            raise ColumnError(
                f"table {table.index}: no column headed {wanted!r} for "
                f"{field_name!r}. Headers read: {available}. Name it exactly, "
                "or give a column index — nothing here guesses at a near miss."
            )
        if len(found) > 1:
            raise ColumnError(
                f"table {table.index}: {wanted!r} heads {len(found)} columns "
                f"({', '.join(str(c) for c in found)}), so {field_name!r} is "
                "ambiguous. Give a column index."
            )
        resolved[field_name] = found[0]
    return resolved


def map_rows(
    table: Table,
    mapping: Mapping[str, str | int],
    *,
    locator: str = "",
    fill_down: Iterable[str] = (),
    skip_blank: Iterable[str] = (),
) -> tuple[dict[str, str], ...]:
    """Apply a mapping and emit manifest rows, each citing where it came from.

    ``locator`` names the section the table is in — ``ENR 3.1`` — and each row
    gets ``"{locator} table {n} row {m}"``, so a value that turns out wrong
    traces to the line of the AIP it was read from.

    ``fill_down`` carries the previous row's value where a cell is blank. Some
    States express the ENR 3 route designator as an empty cell rather than a
    span, which is the same intention written differently, and both have to
    survive. It is opt-in per field because filling down a *level* would invent
    one.

    ``skip_blank`` drops rows where any of those fields is empty — the
    sub-heading rows an AIP puts inside a table body.
    """
    if table.ragged:
        raise TableError(
            f"table {table.index} has {len(table.ragged)} rows of a different "
            f"width from the rest (rows {', '.join(str(r) for r in table.ragged)}). "
            "Padding them would shift every value in those rows one column, so "
            "nothing is emitted."
        )

    columns = resolve_columns(table, mapping)
    carried: dict[str, str] = {}
    filled = {str(f) for f in fill_down}
    required = {str(f) for f in skip_blank}

    rows: list[dict[str, str]] = []
    for offset, line in enumerate(table.body):
        row: dict[str, str] = {}
        for field_name, column in columns.items():
            value = line[column] if column < len(line) else ""
            if not value and field_name in filled:
                value = carried.get(field_name, "")
            if value and field_name in filled:
                carried[field_name] = value
            row[field_name] = value
        if required and any(not row.get(f) for f in required):
            continue
        if not any(row.values()):
            continue
        row["locator"] = (
            f"{locator + ' ' if locator else ''}table {table.index} "
            f"row {offset + 1}"
        )
        rows.append(row)
    return tuple(rows)


def pair_points(
    rows: Sequence[Mapping[str, str]],
    *,
    group: str = "route",
    point: str = "point",
    attributes_from: str = "second",
    start_field: str = "start",
    end_field: str = "end",
) -> tuple[dict[str, str], ...]:
    """Turn one-point-per-row into one-segment-per-row, within each group.

    An ENR 3 table lists significant points; a segment is the gap between two
    consecutive rows of the same route. ALSEM, MIDLE, KUKLA is three rows and
    two segments.

    ``attributes_from`` decides where the segment's own values come from — the
    row of the point it *arrives* at (``"second"``) or the one it *leaves*
    (``"first"``). Both conventions are published, this cannot tell which a
    State used, and getting it wrong puts every distance and every level on the
    neighbouring segment. So it is required to be stated, and every row records
    the answer in ``read_as`` so the choice travels with the data.

    A group with one point produces no segment. That is not a failure: a route
    with one published point in this table joins nothing here, and inventing a
    segment from it would be worse.
    """
    if attributes_from not in ("first", "second"):
        raise ValueError(
            "attributes_from must be 'first' or 'second' — whether a row's "
            "track, distance and limits describe the leg arriving at that "
            "point or the leg leaving it. Both conventions are published and "
            "this cannot tell which; guessing shifts every value by one leg."
        )

    out: list[dict[str, str]] = []
    for index in range(len(rows) - 1):
        here, following = rows[index], rows[index + 1]
        if here.get(group, "") != following.get(group, ""):
            continue
        if not here.get(point) or not following.get(point):
            continue
        source = following if attributes_from == "second" else here
        segment = {
            key: value
            for key, value in source.items()
            if key not in (point, "locator")
        }
        segment[group] = here.get(group, "")
        segment[start_field] = here[point]
        segment[end_field] = following[point]
        segment["read_as"] = f"attributes from the {attributes_from} point"
        segment["locator"] = (
            f"{here.get('locator', '')} to {following.get('locator', '')}"
        ).strip()
        out.append(segment)
    return tuple(out)
