"""Coastlines and borders, so a chart of published airspace has a world under it.

Every other module here reads an aeronautical publication. This one does not,
and the distinction is the reason it is a separate module with a loud name.

What this is
------------
Natural Earth 110m coastline and land boundary lines, public domain, simplified
to about eight thousand vertices for the whole world. It exists so that an FIR
boundary drawn from ENR 2 has a coast beside it and a reader can see at a
glance which part of the world they are looking at.

What this is not
----------------
**It is not an aeronautical source and nothing may be decided from it.** It
never answers which State an airspace belongs to — the AIP does, because the
State that published the section is the State whose airspace it is. It never
answers which FIR a point is in; nothing here answers that at all. A national
border on this map and a flight information region boundary are different
lines drawn for different purposes, and they disagree in a great many places:
FIRs extend over the high seas, they are delegated between States, and one of
them is not evidence about the other.

So the layer is drawn beneath everything, in a colour that reads as background,
and every page carrying it says where it came from. Orientation, not authority.

Accuracy
--------
110m data simplified at 0.03° and rounded to two decimal places — a couple of
kilometres, which is invisible at the scale a route structure is drawn at and
useless at any scale where it would not be. That is deliberate: a coastline
precise enough to look like it means something would invite somebody to
measure against it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

from aeropub.geo import Bounds, Position, unmercator

__all__ = [
    "BASEMAP_PATH",
    "Basemap",
    "attribution",
    "borders",
    "coastline",
    "load_basemap",
]

#: The vendored data file. Shipped with the package because a map that has to
#: fetch its own coastline is a map that is blank behind a firewall.
BASEMAP_PATH = Path(__file__).with_name("data") / "basemap.json"

#: What every page carrying this layer has to say.
ATTRIBUTION = "Coastline and borders: Natural Earth, public domain"

#: Said wherever the layer is described, because the temptation this data
#: creates is to answer a question it cannot answer.
NOT_AERONAUTICAL = (
    "Geography for orientation. A national border and a flight information "
    "region boundary are different lines drawn for different purposes, and "
    "one is not evidence about the other."
)


@dataclass(frozen=True, slots=True)
class Basemap:
    """Two sets of polylines, in longitude/latitude order as GeoJSON gives them."""

    coastline: tuple[tuple[Position, ...], ...] = ()
    borders: tuple[tuple[Position, ...], ...] = ()
    attribution: str = ATTRIBUTION

    @property
    def vertices(self) -> int:
        return sum(len(line) for line in self.coastline) + sum(
            len(line) for line in self.borders
        )

    def clipped(self, window: Bounds | None, *, margin_deg: float = 4.0) -> "Basemap":
        """Only the lines that reach the window being drawn.

        A regional chart carrying the whole world's coastline is mostly bytes
        nobody sees. ``None`` keeps everything, which is what a world view
        wants.

        The clip is by bounding box on whole polylines, not by cutting them:
        a line that enters the window is kept entire. Cutting would create
        endpoints that look like coastline and are not.
        """
        if window is None:
            return self
        # Bounds is in projected Mercator, not degrees. Comparing its y against
        # a latitude is a category error, and one this module made once.
        south_west = unmercator(window.min_x, window.min_y)
        north_east = unmercator(window.max_x, window.max_y)
        low_lat = south_west.latitude - margin_deg
        high_lat = north_east.latitude + margin_deg
        low_lon = south_west.longitude - margin_deg
        high_lon = north_east.longitude + margin_deg

        def reaches(line: Sequence[Position]) -> bool:
            return any(
                low_lat <= p.latitude <= high_lat
                and low_lon <= p.longitude <= high_lon
                for p in line
            )

        return Basemap(
            coastline=tuple(line for line in self.coastline if reaches(line)),
            borders=tuple(line for line in self.borders if reaches(line)),
            attribution=self.attribution,
        )


def _lines(raw: Iterable[Iterable[Sequence[float]]]) -> tuple[tuple[Position, ...], ...]:
    return tuple(
        tuple(Position(latitude=float(lat), longitude=float(lon)) for lon, lat in line)
        for line in raw
    )


@lru_cache(maxsize=1)
def load_basemap(path: str | None = None) -> Basemap:
    """Read the vendored geography. Cached: it never changes at runtime."""
    source = Path(path) if path else BASEMAP_PATH
    payload = json.loads(source.read_text(encoding="utf-8"))
    return Basemap(
        coastline=_lines(payload.get("coastline", [])),
        borders=_lines(payload.get("borders", [])),
        attribution=str(payload.get("attribution", ATTRIBUTION)),
    )


def coastline() -> tuple[tuple[Position, ...], ...]:
    return load_basemap().coastline


def borders() -> tuple[tuple[Position, ...], ...]:
    return load_basemap().borders


def attribution() -> str:
    return load_basemap().attribution
