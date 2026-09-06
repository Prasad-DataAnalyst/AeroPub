"""Reading an AIP table, which is mostly one HTML attribute done right.

The failure this module exists to prevent has no symptom. An ENR 3 table
writes the route designator once, in a cell spanning its segment rows. A
reader that walks the markup in order gets it on the first segment and then
shifts every later row one column left — the next segment's route becomes a
waypoint, its start becomes the next waypoint, and its minimum en-route
altitude becomes a designator. That output parses, loads and draws. It is
fiction.

So: spans are expanded, columns are named exactly or by index and never
scored, ragged tables emit nothing rather than being padded, and every row
carries the line of the AIP it came from.

The tables below are shaped like the ones an eAIP publishes. The values in
them are fixtures.
"""

from __future__ import annotations

import pytest

from aeropub.tables import (
    Cell,
    ColumnError,
    Table,
    TableError,
    map_rows,
    pair_points,
    read_tables,
    resolve_columns,
)

# The shape that matters: one route designator spanning its segments, which is
# how every eAIP publishes ENR 3.
ENR3 = """
<table>
  <caption>ENR 3.2 Upper ATS routes</caption>
  <tr><th>Route</th><th>From</th><th>To</th><th>MEA</th><th>MAA</th></tr>
  <tr><td rowspan="2">UM688</td><td>ALSEM</td><td>MIDLE</td>
      <td>24500</td><td>46000</td></tr>
  <tr><td>MIDLE</td><td>KUKLA</td><td>26000</td><td>46000</td></tr>
  <tr><td rowspan="2">L604</td><td>KUKLA</td><td>RASKI</td>
      <td>9500</td><td>24500</td></tr>
  <tr><td>RASKI</td><td>VELOX</td><td>9500</td><td>24500</td></tr>
</table>
"""

MAPPING = {
    "route": "Route",
    "start": "From",
    "end": "To",
    "mea_ft": "MEA",
    "maa_ft": "MAA",
}


def enr3() -> Table:
    return read_tables(ENR3)[0]


# --------------------------------------------------------------------------
# The attribute
# --------------------------------------------------------------------------


class TestRowspan:
    def test_a_spanned_designator_reaches_every_row_it_covers(self):
        """The whole point. Without this the second segment's route is a
        waypoint and its MEA is a designator."""
        rows = map_rows(enr3(), MAPPING)
        assert [r["route"] for r in rows] == ["UM688", "UM688", "L604", "L604"]

    def test_the_columns_do_not_shift_under_a_span(self):
        rows = map_rows(enr3(), MAPPING)
        assert rows[1] == {
            "route": "UM688",
            "start": "MIDLE",
            "end": "KUKLA",
            "mea_ft": "26000",
            "maa_ft": "46000",
            "locator": "table 0 row 2",
        }

    def test_the_grid_is_rectangular(self):
        table = enr3()
        assert table.width == 5
        assert all(len(row) == 5 for row in table.grid)

    def test_a_colspan_fills_the_columns_it_covers(self):
        table = read_tables(
            "<table><tr><th>A</th><th>B</th><th>C</th></tr>"
            "<tr><td colspan='2'>wide</td><td>c</td></tr></table>"
        )[0]
        assert table.body[0] == ("wide", "wide", "c")

    def test_a_span_of_zero_does_not_make_a_cell_vanish(self):
        table = read_tables(
            "<table><tr><td rowspan='0'>x</td><td>y</td></tr></table>"
        )[0]
        assert table.grid[0] == ("x", "y")

    def test_a_span_that_is_not_a_number_is_one(self):
        table = read_tables(
            "<table><tr><td colspan='two'>x</td><td>y</td></tr></table>"
        )[0]
        assert table.grid[0] == ("x", "y")

    def test_a_rowspan_running_past_the_table_does_not_loop(self):
        table = read_tables(
            "<table><tr><td rowspan='9'>x</td><td>a</td></tr>"
            "<tr><td>b</td></tr></table>"
        )[0]
        assert [row[0] for row in table.grid] == ["x", "x"]
        assert len(table.grid) == 2


# --------------------------------------------------------------------------
# Reading the markup
# --------------------------------------------------------------------------


class TestReading:
    def test_the_caption_is_kept(self):
        assert enr3().caption == "ENR 3.2 Upper ATS routes"

    def test_header_rows_are_counted_from_th_alone(self):
        assert enr3().header_rows == 1
        assert enr3().headers == ("Route", "From", "To", "MEA", "MAA")

    def test_a_table_with_no_th_has_no_headers(self):
        """Honest: the caller addresses columns by index."""
        table = read_tables("<table><tr><td>a</td><td>b</td></tr></table>")[0]
        assert table.header_rows == 0
        assert table.headers == ()

    def test_a_two_row_header_joins_group_and_column(self):
        """An eAIP writes the group name over its columns, and the useful name
        is the pair."""
        table = read_tables(
            "<table>"
            "<tr><th colspan='2'>Limits</th><th rowspan='2'>Unit</th></tr>"
            "<tr><th>Lower</th><th>Upper</th></tr>"
            "<tr><td>FL245</td><td>FL660</td><td>Alpha</td></tr>"
            "</table>"
        )[0]
        assert table.header_rows == 2
        assert table.headers == ("Limits Lower", "Limits Upper", "Unit")

    def test_an_empty_row_does_not_confuse_the_header_count(self):
        """An eAIP emits them, and counting one as a header row would take a
        line of data for a column name."""
        table = read_tables(
            "<table><tr><th>Route</th></tr><tr></tr>"
            "<tr><td>UM688</td></tr></table>"
        )[0]
        assert table.header_rows == 1
        assert table.headers == ("Route",)
        assert ("UM688",) in table.body

    def test_non_breaking_spaces_are_collapsed(self):
        """An AIP is full of them and they are not part of a designator."""
        table = read_tables(
            "<table><tr><td>&nbsp;UM688&nbsp;&nbsp;</td></tr></table>"
        )[0]
        assert table.grid[0][0] == "UM688"

    def test_markup_inside_a_cell_becomes_spaces(self):
        table = read_tables(
            "<table><tr><td>ALSEM<br/>compulsory</td></tr></table>"
        )[0]
        assert table.grid[0][0] == "ALSEM compulsory"

    def test_script_and_style_never_reach_a_cell(self):
        table = read_tables(
            "<table><tr><td>UM688<script>var x=1;</script></td></tr></table>"
        )[0]
        assert table.grid[0][0] == "UM688"

    def test_every_table_is_returned_in_document_order(self):
        found = read_tables(
            "<table><tr><td>first</td></tr></table>"
            "<table><tr><td>second</td></tr></table>"
        )
        assert [t.grid[0][0] for t in found] == ["first", "second"]
        assert [t.index for t in found] == [0, 1]

    def test_a_nested_table_is_readable_both_ways(self):
        """An eAIP nests a table in a cell to lay out a pair, and both the
        whole row and the pair are useful."""
        found = read_tables(
            "<table><tr><td>OUTER</td>"
            "<td><table><tr><td>inner</td></tr></table></td></tr></table>"
        )
        assert len(found) == 2
        outer = next(t for t in found if "OUTER" in t.grid[0])
        assert "inner" in outer.grid[0][1]

    def test_an_unclosed_table_is_still_read(self):
        """A malformed document is still evidence, and dropping it would lose
        rows silently."""
        found = read_tables("<table><tr><td>UM688</td><td>ALSEM</td></tr>")
        assert found and found[0].grid[0] == ("UM688", "ALSEM")

    def test_a_document_with_no_tables_reads_as_none(self):
        assert read_tables("<p>nothing here</p>") == ()

    def test_empty_input_does_not_fail(self):
        assert read_tables("") == ()


# --------------------------------------------------------------------------
# Naming columns
# --------------------------------------------------------------------------


class TestColumns:
    def test_a_header_is_matched_exactly(self):
        assert resolve_columns(enr3(), {"route": "Route"}) == {"route": 0}

    def test_case_and_spacing_do_not_matter(self):
        assert resolve_columns(enr3(), {"mea_ft": "  mea "}) == {"mea_ft": 3}

    def test_a_near_miss_is_refused_rather_than_scored(self):
        """MEA and MAA differ by one letter and twenty thousand feet."""
        with pytest.raises(ColumnError, match="nothing here guesses"):
            resolve_columns(enr3(), {"mea_ft": "M.E.A."})

    def test_the_error_lists_the_headers_it_did_read(self):
        with pytest.raises(ColumnError, match="Route, From, To, MEA, MAA"):
            resolve_columns(enr3(), {"x": "Minimum"})

    def test_a_column_index_is_accepted(self):
        assert resolve_columns(enr3(), {"route": 0, "mea_ft": 3}) == {
            "route": 0,
            "mea_ft": 3,
        }

    def test_an_index_as_text_is_accepted(self):
        assert resolve_columns(enr3(), {"route": "0"}) == {"route": 0}

    def test_an_index_outside_the_table_is_refused(self):
        with pytest.raises(ColumnError, match="outside the table"):
            resolve_columns(enr3(), {"route": 9})

    def test_a_header_appearing_twice_is_ambiguous(self):
        table = read_tables(
            "<table><tr><th>Level</th><th>Level</th></tr>"
            "<tr><td>a</td><td>b</td></tr></table>"
        )[0]
        with pytest.raises(ColumnError, match="ambiguous"):
            resolve_columns(table, {"level": "Level"})

    def test_a_table_with_no_columns_is_refused(self):
        with pytest.raises(TableError, match="no columns"):
            resolve_columns(Table(index=0), {"route": "Route"})


# --------------------------------------------------------------------------
# Emitting rows
# --------------------------------------------------------------------------


class TestRows:
    def test_every_row_cites_where_it_came_from(self):
        rows = map_rows(enr3(), MAPPING, locator="ENR 3.2")
        assert rows[0]["locator"] == "ENR 3.2 table 0 row 1"
        assert rows[3]["locator"] == "ENR 3.2 table 0 row 4"

    def test_only_the_mapped_fields_are_emitted(self):
        rows = map_rows(enr3(), {"route": "Route"})
        assert set(rows[0]) == {"route", "locator"}

    def test_a_ragged_table_emits_nothing(self):
        """Padding would shift every value in those rows one column."""
        table = Table(index=0, grid=(("a", "b"), ("c",)), ragged=(1,))
        with pytest.raises(TableError, match="different width"):
            map_rows(table, {"x": 0})

    def test_a_wholly_empty_row_is_dropped(self):
        table = read_tables(
            "<table><tr><th>Route</th></tr><tr><td>UM688</td></tr>"
            "<tr><td></td></tr></table>"
        )[0]
        assert len(map_rows(table, {"route": "Route"})) == 1

    def test_fill_down_carries_a_designator_written_once(self):
        """Some States leave the cell empty instead of spanning it. Same
        intention, different markup, and both have to survive."""
        table = read_tables(
            "<table><tr><th>Route</th><th>From</th></tr>"
            "<tr><td>UM688</td><td>ALSEM</td></tr>"
            "<tr><td></td><td>MIDLE</td></tr></table>"
        )[0]
        rows = map_rows(
            table, {"route": "Route", "start": "From"}, fill_down=["route"]
        )
        assert [r["route"] for r in rows] == ["UM688", "UM688"]

    def test_fill_down_is_opt_in_per_field(self):
        """Filling down a level would invent one."""
        table = read_tables(
            "<table><tr><th>Route</th><th>MEA</th></tr>"
            "<tr><td>UM688</td><td>24500</td></tr>"
            "<tr><td></td><td></td></tr></table>"
        )[0]
        rows = map_rows(
            table, {"route": "Route", "mea_ft": "MEA"}, fill_down=["route"]
        )
        assert rows[1]["route"] == "UM688"
        assert rows[1]["mea_ft"] == ""

    def test_skip_blank_drops_the_sub_headings_inside_a_body(self):
        table = read_tables(
            "<table><tr><th>Route</th><th>From</th></tr>"
            "<tr><td>LOWER ROUTES</td><td></td></tr>"
            "<tr><td>UM688</td><td>ALSEM</td></tr></table>"
        )[0]
        rows = map_rows(
            table, {"route": "Route", "start": "From"}, skip_blank=["start"]
        )
        assert [r["route"] for r in rows] == ["UM688"]

    def test_values_are_not_converted_only_stripped(self):
        """A coordinate stays the text the AIP printed; the manifest loader
        parses it with the same reader everything else uses."""
        table = read_tables(
            "<table><tr><th>Lat</th></tr><tr><td> 251500N </td></tr></table>"
        )[0]
        assert map_rows(table, {"latitude": "Lat"})[0]["latitude"] == "251500N"


# --------------------------------------------------------------------------
# Points into segments
# --------------------------------------------------------------------------

# What an eAIP ENR 3 table actually looks like: one significant point per row,
# the route spanning its points, and the segment values on the arriving row.
POINTS = """
<table>
<tr><th rowspan="2">Route</th><th rowspan="2">Significant points</th>
    <th colspan="2">Track</th><th rowspan="2">Distance</th>
    <th colspan="2">Vertical limits</th></tr>
<tr><th>Fwd</th><th>Rev</th><th>Upper</th><th>Lower</th></tr>
<tr><td rowspan="3">UM688</td><td>ALSEM</td><td>301</td><td>121</td>
    <td>&nbsp;</td><td>FL460</td><td>FL245</td></tr>
<tr><td>MIDLE</td><td>305</td><td>125</td><td>178</td><td>FL460</td><td>FL260</td></tr>
<tr><td>KUKLA</td><td></td><td></td><td>212</td><td>FL460</td><td>FL260</td></tr>
<tr><td rowspan="2">L604</td><td>KUKLA</td><td>288</td><td>108</td>
    <td>&nbsp;</td><td>FL245</td><td>9500</td></tr>
<tr><td>RASKI</td><td></td><td></td><td>246</td><td>FL245</td><td>9500</td></tr>
</table>
"""

POINT_MAP = {
    "route": "Route",
    "point": "Significant points",
    "distance_nm": "Distance",
    "upper_limit_ft": "Vertical limits Upper",
    "mea_ft": "Vertical limits Lower",
}


def point_rows():
    return map_rows(read_tables(POINTS)[0], POINT_MAP, locator="ENR 3.2")


class TestPairing:
    def test_three_points_make_two_segments(self):
        found = pair_points(point_rows())
        assert [(s["start"], s["end"]) for s in found] == [
            ("ALSEM", "MIDLE"),
            ("MIDLE", "KUKLA"),
            ("KUKLA", "RASKI"),
        ]

    def test_pairing_never_crosses_a_route(self):
        """UM688 ends at KUKLA and L604 begins there. They are not one leg."""
        found = pair_points(point_rows())
        assert [s["route"] for s in found] == ["UM688", "UM688", "L604"]

    def test_the_values_come_from_the_row_the_caller_named(self):
        arriving = pair_points(point_rows(), attributes_from="second")
        leaving = pair_points(point_rows(), attributes_from="first")
        assert arriving[0]["distance_nm"] == "178"
        assert leaving[0]["distance_nm"] == ""

    def test_which_convention_was_used_travels_with_the_row(self):
        """Getting it wrong shifts every distance by one leg, invisibly."""
        found = pair_points(point_rows(), attributes_from="second")
        assert found[0]["read_as"] == "attributes from the second point"

    def test_the_convention_must_be_stated(self):
        with pytest.raises(ValueError, match="both conventions are published|Both conventions"):
            pair_points(point_rows(), attributes_from="whichever")

    def test_a_route_with_one_point_joins_nothing(self):
        """Inventing a segment from it would be worse than emitting none."""
        rows = [
            {"route": "Q1", "point": "ALSEM", "locator": "r1"},
            {"route": "Q2", "point": "MIDLE", "locator": "r2"},
        ]
        assert pair_points(rows) == ()

    def test_a_blank_point_does_not_become_an_end_of_a_leg(self):
        rows = [
            {"route": "Q1", "point": "ALSEM", "locator": "r1"},
            {"route": "Q1", "point": "", "locator": "r2"},
            {"route": "Q1", "point": "KUKLA", "locator": "r3"},
        ]
        assert pair_points(rows) == ()

    def test_the_locator_names_both_rows(self):
        found = pair_points(point_rows())
        assert found[0]["locator"] == (
            "ENR 3.2 table 0 row 1 to ENR 3.2 table 0 row 2"
        )

    def test_the_point_column_does_not_survive_into_the_segment(self):
        """A segment has a start and an end, not a point."""
        assert "point" not in pair_points(point_rows())[0]

    def test_paired_segments_load(self, tmp_path):
        import json

        from aeropub.ats import load_ats_structure

        (tmp_path / "enr3.txt").write_text("fixture\n", encoding="utf-8")
        segments = pair_points(point_rows(), attributes_from="second")
        payload = {
            "source": {
                "source_id": "FIXTURE",
                "document": "AIP AA ENR 3.2",
                "document_path": "enr3.txt",
                "retrieved_at": "2026-09-01T12:00:00Z",
            },
            "region": "AAAA",
            "points": [],
            "segments": [
                {k: v for k, v in s.items() if k not in ("read_as",)}
                for s in segments
            ],
        }
        path = tmp_path / "enr3.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        held = load_ats_structure(path)
        assert held.points_on("UM688") == ("ALSEM", "MIDLE", "KUKLA")
        assert held.on("UM688")[1].distance_nm == 212.0


# --------------------------------------------------------------------------
# End to end, into a manifest the loaders already read
# --------------------------------------------------------------------------


class TestIntoAManifest:
    def test_a_table_becomes_ats_segments_that_load(self, tmp_path):
        import json

        from aeropub.ats import load_ats_structure

        document = tmp_path / "enr3.txt"
        document.write_text("fixture ENR 3 page\n", encoding="utf-8")
        rows = map_rows(enr3(), MAPPING, locator="ENR 3.2")
        payload = {
            "source": {
                "source_id": "FIXTURE",
                "document": "AIP AA ENR 3.2",
                "document_path": "enr3.txt",
                "retrieved_at": "2026-09-01T12:00:00Z",
            },
            "region": "AAAA",
            "points": [],
            "segments": [dict(r) for r in rows],
        }
        path = tmp_path / "enr3.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        held = load_ats_structure(path)
        assert held.routes == ("L604", "UM688")
        # The row that the naive reader would have corrupted.
        second = held.on("UM688")[1]
        assert (second.start, second.end) == ("MIDLE", "KUKLA")
        assert second.mea_ft == 26000.0
        assert second.source.locator == "ENR 3.2 table 0 row 2"
