"""Coordinates — read as published, and the arithmetic that follows from them.

Everything else in this platform has refused to hold geometry, and said so on
every page: no coordinates, so no containment claim, so no map. That refusal
was about *inventing* geometry. It was never about the geometry a State
publishes.

ENR 4.4 prints the latitude and longitude of every name-code designator. ENR
4.1 prints them for every navigation aid. AD 2.2 prints the aerodrome reference
point. Those are published figures with a citation like any other, and reading
them is reading the AIP — not guessing at it. This module is what turns that
column of a table into a position, and the positions into the two things a
plan view needs: where to draw a point, and what path actually joins two of
them.

A route is not a straight line
-------------------------------
The path between two points is a great circle, and on any flat projection it is
a curve. Drawing it straight is not a simplification, it is a different route:
between distant points the straight line on a Mercator sheet can sit hundreds
of miles from the track flown, over different terrain and different airspace.
:func:`great_circle_path` returns the intermediate positions so a drawing can
follow the track rather than the paper.

Computed is not published
--------------------------
:func:`great_circle_nm` gives the distance between two published positions. It
is a *derived* figure and it is never a substitute for the distance ENR 3
publishes for a segment — a State's figure is what the route is planned and
charged on, and the two can differ legitimately (a segment is not always the
great circle between its ends).

The useful thing is the comparison. :func:`distance_disagreement` reports where
a published distance and the distance between the published coordinates
disagree by more than a stated tolerance, which means one of the two is wrong
and somebody should find out which. That is a finding nobody gets from either
figure alone.

What it will not read
---------------------
Coordinate formats in an AIP run to prose — "as depicted", "along the FIR
boundary", "thence clockwise". :func:`parse_coordinate` reads the numeric forms
States actually print in a coordinate column and refuses everything else. A
coordinate parser that guessed would place a waypoint somewhere nobody
published, and a point drawn in the wrong place is worse than a point not
drawn: one is a gap and the other is a map.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "EARTH_RADIUS_NM",
    "Bounds",
    "CoordinateError",
    "Position",
    "bounds_of",
    "destination",
    "distance_disagreement",
    "format_coordinate",
    "great_circle_nm",
    "great_circle_path",
    "initial_bearing",
    "intermediate",
    "mercator",
    "parse_coordinate",
    "parse_position",
]

#: Mean Earth radius in nautical miles, from the IUGG mean radius of
#: 6371.0088 km at 1852 m to the nautical mile. Spherical, which is what a
#: plan view and a route distance want; an ellipsoidal figure would differ by
#: less than the rounding on any published segment distance.
EARTH_RADIUS_NM = 6371.0088 / 1.852

#: Where the Mercator projection is cut off. The projection sends the poles to
#: infinity, so a drawing has to stop somewhere; this is the conventional
#: square-aspect limit and it is far outside any airway.
MERCATOR_LIMIT_DEG = 85.05113


@dataclass(frozen=True, slots=True)
class Position:
    """One published position, in degrees.

    Positive is north and east, which is the convention every published
    decimal coordinate uses. The hemisphere letters an AIP prints are resolved
    by :func:`parse_coordinate` on the way in, so nothing downstream has to
    carry a sign convention of its own.
    """

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ValueError(
                f"latitude {self.latitude} is outside -90 to 90 — a position "
                "that cannot exist is refused rather than clamped, because a "
                "clamped one would be drawn somewhere real"
            )
        if not -180.0 <= self.longitude <= 180.0:
            raise ValueError(
                f"longitude {self.longitude} is outside -180 to 180"
            )

    def describe(self) -> str:
        return (
            f"{format_coordinate(self.latitude, is_latitude=True)} "
            f"{format_coordinate(self.longitude, is_latitude=False)}"
        )


# --------------------------------------------------------------------------
# Reading what an AIP prints
# --------------------------------------------------------------------------

#: Degrees-minutes-seconds as a coordinate column prints it: ``251530N``,
#: ``0513015E``, with optional decimal seconds. Latitude takes two degree
#: digits and longitude three, which is how the two are told apart.
_DMS = re.compile(
    r"^(?P<deg>\d{2,3})(?P<min>[0-5]\d)(?P<sec>[0-5]\d(?:\.\d+)?)"
    r"(?P<hemi>[NSEW])$"
)

#: Degrees and minutes only: ``2515N``, ``05130E``.
_DM = re.compile(r"^(?P<deg>\d{2,3})(?P<min>[0-5]\d(?:\.\d+)?)(?P<hemi>[NSEW])$")

#: The symbol form, which appears in prose and in some tables.
_SYMBOL = re.compile(
    r"^(?P<deg>\d{1,3})[°\s](?P<min>\d{1,2})['′\s]"
    r"(?:(?P<sec>\d{1,2}(?:\.\d+)?)[\"″]?)?\s*(?P<hemi>[NSEW])$"
)

#: Signed decimal degrees, which is what a machine-readable extract gives.
_DECIMAL = re.compile(r"^[-+]?\d{1,3}(?:\.\d+)?$")


class CoordinateError(ValueError):
    """A coordinate that could not be read as published.

    Its own type so a caller can tell "this cell was prose" from "this cell
    was a number out of range", and so a manifest loader can turn it into the
    kind of refusal the rest of the platform makes.
    """


def parse_coordinate(text: object, *, is_latitude: bool | None = None) -> float:
    """Read one coordinate the way an AIP prints it, in degrees.

    Accepts degrees-minutes-seconds with a hemisphere letter, degrees and
    minutes, the symbol form, and signed decimal degrees. Refuses everything
    else: a coordinate parser that guessed would place a waypoint somewhere
    nobody published, and a point drawn in the wrong place is worse than a
    point not drawn — one is a gap and the other is a map.

    ``is_latitude`` is checked where it is given, because a value that is a
    valid longitude and an impossible latitude is exactly the transposition
    this catches.
    """
    if text is None or (isinstance(text, str) and not text.strip()):
        raise CoordinateError("no coordinate given")
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        value = float(text)
        return _range_check(value, is_latitude=is_latitude)

    cleaned = "".join(str(text).split()).upper()

    for pattern in (_DMS, _DM, _SYMBOL):
        match = pattern.match(cleaned)
        if not match:
            continue
        parts = match.groupdict()
        degrees = float(parts["deg"])
        minutes = float(parts.get("min") or 0.0)
        seconds = float(parts.get("sec") or 0.0)
        if minutes >= 60.0 or seconds >= 60.0:
            raise CoordinateError(
                f"{text!r}: minutes and seconds run 0 to 59"
            )
        value = degrees + minutes / 60.0 + seconds / 3600.0
        hemisphere = parts["hemi"]
        if hemisphere in ("S", "W"):
            value = -value
        found_latitude = hemisphere in ("N", "S")
        if is_latitude is not None and found_latitude is not is_latitude:
            raise CoordinateError(
                f"{text!r} is a "
                f"{'latitude' if found_latitude else 'longitude'} where a "
                f"{'latitude' if is_latitude else 'longitude'} was expected. "
                "A transposed pair puts a waypoint in a different hemisphere."
            )
        return _range_check(value, is_latitude=found_latitude)

    if _DECIMAL.match(cleaned):
        return _range_check(float(cleaned), is_latitude=is_latitude)

    raise CoordinateError(
        f"{text!r} could not be read as a coordinate. Formats read are "
        "DDMMSS with a hemisphere letter, DDMM, the degree-symbol form, and "
        "signed decimal degrees. Anything else is left unread rather than "
        "guessed."
    )


def _range_check(value: float, *, is_latitude: bool | None) -> float:
    limit = 90.0 if is_latitude else 180.0
    if abs(value) > limit:
        raise CoordinateError(
            f"{value} is outside ±{limit:.0f}"
            + (" for a latitude" if is_latitude else "")
        )
    return value


def parse_position(latitude: object, longitude: object) -> Position:
    """Read a published pair. Each half is checked as the half it should be."""
    return Position(
        latitude=parse_coordinate(latitude, is_latitude=True),
        longitude=parse_coordinate(longitude, is_latitude=False),
    )


def format_coordinate(value: float, *, is_latitude: bool) -> str:
    """Print a coordinate the way a chart does — DDMMSS with a hemisphere."""
    hemisphere = ("N" if value >= 0 else "S") if is_latitude else (
        "E" if value >= 0 else "W"
    )
    magnitude = abs(value)
    degrees = int(magnitude)
    minutes_full = (magnitude - degrees) * 60.0
    minutes = int(minutes_full)
    seconds = (minutes_full - minutes) * 60.0
    # Rounding can carry: 59.9995 seconds is a whole minute.
    if round(seconds) >= 60:
        seconds = 0.0
        minutes += 1
    if minutes >= 60:
        minutes = 0
        degrees += 1
    width = 2 if is_latitude else 3
    return f"{degrees:0{width}d}{minutes:02d}{round(seconds):02d}{hemisphere}"


# --------------------------------------------------------------------------
# Great-circle arithmetic
# --------------------------------------------------------------------------


def great_circle_nm(start: Position, end: Position) -> float:
    """Distance between two published positions, in nautical miles.

    **Computed, never published.** A State's segment distance is what the
    route is planned and charged on, and the two can differ legitimately — a
    published segment is not always the great circle between its ends. Use
    :func:`distance_disagreement` to compare them rather than substituting one
    for the other.
    """
    lat1, lon1 = math.radians(start.latitude), math.radians(start.longitude)
    lat2, lon2 = math.radians(end.latitude), math.radians(end.longitude)
    d_lat = lat2 - lat1
    d_lon = lon2 - lon1
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    )
    return EARTH_RADIUS_NM * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def initial_bearing(start: Position, end: Position) -> float:
    """True bearing at the start of the great circle, in degrees.

    *Initial*, and the word matters: a great circle changes bearing along its
    length, so this is not the track flown for the whole leg. It is what a
    drawing needs at the departure end and what a plan states as an initial
    track. Returned in the aviation range, 001 to 360.
    """
    lat1, lat2 = math.radians(start.latitude), math.radians(end.latitude)
    d_lon = math.radians(end.longitude - start.longitude)
    y = math.sin(d_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(
        d_lon
    )
    turned = math.degrees(math.atan2(y, x)) % 360.0
    return 360.0 if turned == 0.0 else turned


def destination(start: Position, bearing_deg: float, distance_nm: float) -> Position:
    """Where you arrive going that far on that bearing, on the sphere.

    What an arc needs. An AIP defines a great many boundaries as "an arc of
    N NM radius centred on X", and drawing one means walking the circle about
    the centre — which is this, once per step.

    Computed on the sphere for the same reason everything else here is: a
    circle of 30 NM drawn by adding degrees to a latitude and a longitude is
    an ellipse everywhere except the equator, and at 50°N it is out by half
    its radius in one axis.
    """
    if distance_nm < 0:
        raise ValueError("distance_nm must not be negative")
    angular = distance_nm / EARTH_RADIUS_NM
    bearing = math.radians(bearing_deg % 360.0)
    lat1 = math.radians(start.latitude)
    lon1 = math.radians(start.longitude)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular)
        + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    # Normalised into -180..180 rather than left to run past the antimeridian.
    degrees = (math.degrees(lon2) + 540.0) % 360.0 - 180.0
    return Position(latitude=math.degrees(lat2), longitude=degrees)


def intermediate(start: Position, end: Position, fraction: float) -> Position:
    """A position a given fraction along the great circle.

    Interpolated on the sphere rather than between the numbers: averaging two
    longitudes across the antimeridian puts the midpoint on the far side of
    the world, and averaging two latitudes on a long leg puts it hundreds of
    miles off the track.
    """
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0 and 1")
    lat1, lon1 = math.radians(start.latitude), math.radians(start.longitude)
    lat2, lon2 = math.radians(end.latitude), math.radians(end.longitude)
    delta = great_circle_nm(start, end) / EARTH_RADIUS_NM
    if delta == 0.0:
        return start
    a = math.sin((1.0 - fraction) * delta) / math.sin(delta)
    b = math.sin(fraction * delta) / math.sin(delta)
    x = a * math.cos(lat1) * math.cos(lon1) + b * math.cos(lat2) * math.cos(lon2)
    y = a * math.cos(lat1) * math.sin(lon1) + b * math.cos(lat2) * math.sin(lon2)
    z = a * math.sin(lat1) + b * math.sin(lat2)
    return Position(
        latitude=math.degrees(math.atan2(z, math.sqrt(x * x + y * y))),
        longitude=math.degrees(math.atan2(y, x)),
    )


def great_circle_path(
    start: Position, end: Position, *, steps: int = 24
) -> tuple[Position, ...]:
    """The track between two positions, as points a drawing can follow.

    A straight line on a flat projection is not a simplification of a great
    circle, it is a different route: between distant points it can sit
    hundreds of miles from the track flown, over different terrain and
    different airspace. A drawing that used one would be showing a path nobody
    flies.
    """
    if steps < 1:
        raise ValueError("steps must be at least 1")
    return tuple(
        intermediate(start, end, index / steps) for index in range(steps + 1)
    )


def distance_disagreement(
    published_nm: float | None,
    start: Position | None,
    end: Position | None,
    *,
    tolerance_nm: float = 2.0,
    tolerance_fraction: float = 0.02,
) -> float | None:
    """How far a published distance is from the distance between its ends.

    ``None`` where either side is missing, or where they agree inside the
    tolerance. A figure back means one of the two is wrong and somebody should
    find out which — a transposed coordinate and a mistyped distance both show
    up here and nowhere else.

    The tolerance is generous on purpose, and it is both absolute and
    proportional: a published segment is not always the great circle between
    its ends, magnetic-versus-true routings differ slightly, and States round.
    A small disagreement is not evidence of anything; a large one is.
    """
    if published_nm is None or start is None or end is None:
        return None
    computed = great_circle_nm(start, end)
    gap = abs(published_nm - computed)
    allowed = max(tolerance_nm, published_nm * tolerance_fraction)
    return gap if gap > allowed else None


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


def mercator(position: Position) -> tuple[float, float]:
    """Project to unit Mercator coordinates, x and y in the range ±1 across.

    Mercator because it is what aeronautical plan charts use and what a reader
    expects: it preserves angles, so a track drawn on it meets a meridian at
    the angle it really does. It does not preserve area or distance, and the
    distortion grows with latitude — which is why nothing here measures
    anything off the projection. Distance and bearing come from
    :func:`great_circle_nm` and :func:`initial_bearing`, on the sphere.

    Latitude is clamped at the conventional limit: the projection sends the
    poles to infinity, and a drawing has to stop somewhere.
    """
    x = position.longitude / 180.0
    latitude = max(-MERCATOR_LIMIT_DEG, min(MERCATOR_LIMIT_DEG, position.latitude))
    y = math.log(math.tan(math.pi / 4.0 + math.radians(latitude) / 2.0)) / math.pi
    return (x, y)


@dataclass(frozen=True, slots=True)
class Bounds:
    """The projected extent of a set of positions."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def padded(self, fraction: float = 0.08) -> "Bounds":
        """Room around the content, and never a zero-sized box.

        A single position has no extent, and a projection window with no
        extent divides by zero on the first point drawn into it.
        """
        pad_x = max(self.width * fraction, 0.004)
        pad_y = max(self.height * fraction, 0.004)
        return Bounds(
            min_x=self.min_x - pad_x,
            min_y=self.min_y - pad_y,
            max_x=self.max_x + pad_x,
            max_y=self.max_y + pad_y,
        )


def bounds_of(positions: Iterable[Position]) -> Bounds | None:
    """The projected box containing every position, or ``None`` for none.

    ``None`` rather than a default window: a map drawn over an arbitrary
    extent because nothing was held would show empty ocean and read as a place
    with nothing in it.
    """
    projected = [mercator(p) for p in positions]
    if not projected:
        return None
    xs = [x for x, _ in projected]
    ys = [y for _, y in projected]
    return Bounds(min_x=min(xs), min_y=min(ys), max_x=max(xs), max_y=max(ys))
