"""The ICAO Annexes, as a citation index. Not as text.

**The Annexes are not open source and are not in this repository.** They are
copyrighted publications sold by ICAO, and the store's own terms say they may
not be reproduced. Nothing here quotes one, and nothing here should ever be
made to. What is held is the bibliographic frame — which Annex governs what,
how it is divided, and which parts of this platform stand on it — all of which
is freely published metadata rather than the standard itself.

Why an index earns its place anyway
------------------------------------
A finding is worth more when it names the instrument behind it. "FL360
eastbound is against the direction of flight" is an assertion; "against the
table of cruising levels in Annex 2 Appendix 3, as the State publishes it in
ENR 1.3" is a citation somebody can check. This module is what lets the second
sentence be written without a person going to look up which Annex it was.

It also makes a quiet dependency visible. Eighteen modules here rest on
Annexes 2, 10, 11, 14 and 15 — that was true before this file existed, spread
across docstrings where nothing could count it. An amendment to Annex 14
touches six modules, and knowing which six is the difference between a review
and a search.

What is deliberately not claimed
---------------------------------
Edition and amendment numbers move, and a stale edition asserted confidently is
worse than none: it invites somebody to conclude they are current when they are
not. Where an edition is recorded it is marked with when it was checked, and
where it is not, :attr:`Annex.edition` is empty rather than guessed.

Chapter and paragraph references are given only where this platform already
relies on them and they are stable enough to state. Anything finer belongs in
the publication, which the reader has to hold anyway — this index tells them
which one to open, not what it says.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "Annex",
    "ANNEXES",
    "Provision",
    "PROVISIONS",
    "annex",
    "bearing_on",
    "ANNEX_TEXT_IS_NOT_HELD",
]

#: Stated once, so nothing has to infer it from an absence.
ANNEX_TEXT_IS_NOT_HELD = (
    "The ICAO Annexes are copyrighted publications sold by ICAO and are not "
    "reproduced here. This index names them and says what they govern; the "
    "standards themselves have to be held under licence from ICAO."
)


@dataclass(frozen=True, slots=True)
class Annex:
    """One Annex to the Convention on International Civil Aviation."""

    number: int
    title: str
    governs: str
    """One line on what it covers, for a reader who needs to pick one."""

    volumes: tuple[str, ...] = ()
    """Its parts or volumes, where it has them. An Annex cited without its
    volume is not citable — Annex 6 Part I and Part II carry different rules
    for different operations."""

    bears_on: tuple[str, ...] = ()
    """Modules in this platform that rest on it. Derived from the code, so an
    amendment can be scoped rather than searched for."""

    edition: str = ""
    """Recorded only where it has been checked, with the date of checking.
    Empty rather than guessed: a stale edition asserted confidently invites a
    reader to conclude they are current when they are not."""

    @property
    def is_load_bearing(self) -> bool:
        """Whether anything here depends on it today."""
        return bool(self.bears_on)

    def citation(self, volume: str = "", detail: str = "") -> str:
        """A citation string. The volume is required where the Annex has any.

        A bare "Annex 6" is ambiguous across four Parts governing different
        operations, so asking for one raises rather than producing a citation
        that cannot be followed.
        """
        if self.volumes and not volume:
            raise ValueError(
                f"Annex {self.number} is published in "
                f"{len(self.volumes)} parts — name one: "
                + "; ".join(self.volumes)
            )
        parts = [f"ICAO Annex {self.number} — {self.title}"]
        if volume:
            parts.append(volume)
        if detail:
            parts.append(detail)
        return ", ".join(parts)


ANNEXES: dict[int, Annex] = {
    1: Annex(1, "Personnel Licensing",
             "Licences and ratings for flight crew, controllers and engineers."),
    2: Annex(2, "Rules of the Air",
             "The rules of flight themselves: right of way, cruising levels, "
             "signals, interception.",
             bears_on=("flightrules", "interception", "supps", "route", "cli")),
    3: Annex(3, "Meteorological Service for International Air Navigation",
             "METAR, TAF, SIGMET, volcanic ash and the meteorological watch."),
    4: Annex(4, "Aeronautical Charts",
             "What each chart type must show and how it must show it."),
    5: Annex(5, "Units of Measurement to be Used in Air and Ground Operations",
             "Which units are used for what, and the permitted alternatives."),
    6: Annex(6, "Operation of Aircraft",
             "Operating rules, equipment and crew requirements.",
             volumes=(
                 "Part I — International Commercial Air Transport, Aeroplanes",
                 "Part II — International General Aviation, Aeroplanes",
                 "Part III — International Operations, Helicopters",
                 "Part IV — International Operations, Remotely Piloted Aircraft Systems",
             )),
    7: Annex(7, "Aircraft Nationality and Registration Marks",
             "How an aircraft is marked and registered."),
    8: Annex(8, "Airworthiness of Aircraft",
             "Certification and continuing airworthiness."),
    9: Annex(9, "Facilitation",
             "Border formalities, entry and clearance of aircraft and people."),
    10: Annex(10, "Aeronautical Telecommunications",
              "Radio navigation aids, communication systems and procedures, "
              "surveillance, and spectrum.",
              volumes=(
                  "Volume I — Radio Navigation Aids",
                  "Volume II — Communication Procedures",
                  "Volume III — Communication Systems",
                  "Volume IV — Surveillance and Collision Avoidance Systems",
                  "Volume V — Aeronautical Radio Frequency Spectrum Utilization",
                  "Volume VI — Communication Systems and Procedures relating to "
                  "Remotely Piloted Aircraft Systems C2 Link",
              ),
              bears_on=("interception",)),
    11: Annex(11, "Air Traffic Services",
              "ATS airspace classes, the services provided in each, and the "
              "division of airspace.",
              bears_on=("airspace",)),
    12: Annex(12, "Search and Rescue",
              "SAR organisation, procedures and signals."),
    13: Annex(13, "Aircraft Accident and Incident Investigation",
              "How occurrences are investigated and reported."),
    14: Annex(14, "Aerodromes",
              "Aerodrome physical characteristics, obstacle limitation "
              "surfaces, markings, lighting and rescue and fire fighting.",
              volumes=(
                  "Volume I — Aerodrome Design and Operations",
                  "Volume II — Heliports",
              ),
              bears_on=("obstacles", "aircraft", "holding", "suitability",
                        "charts", "api")),
    15: Annex(15, "Aeronautical Information Services",
              "The AIP, AIRAC, NOTAM, AIC and the integrated aeronautical "
              "information package — what this platform reads.",
              bears_on=("airac", "notam", "aip", "quality", "cli")),
    16: Annex(16, "Environmental Protection",
              "Noise, engine emissions and aeroplane CO2.",
              volumes=(
                  "Volume I — Aircraft Noise",
                  "Volume II — Aircraft Engine Emissions",
                  "Volume III — Aeroplane CO2 Emissions",
                  "Volume IV — Carbon Offsetting and Reduction Scheme for "
                  "International Aviation (CORSIA)",
              )),
    17: Annex(17, "Aviation Security",
              "Safeguarding international civil aviation against acts of "
              "unlawful interference."),
    18: Annex(18, "The Safe Transport of Dangerous Goods by Air",
              "What may be carried, how it is packed, marked and declared."),
    19: Annex(19, "Safety Management",
              "State safety programmes and operator safety management systems."),
}


@dataclass(frozen=True, slots=True)
class Provision:
    """One provision this platform actually rests on.

    Recorded because the module that relies on it says so in prose, and prose
    cannot be counted, checked or listed. Nothing here reproduces the
    provision — it names where to read it.
    """

    annex: int
    volume: str
    detail: str
    what: str
    """What it establishes, in this platform's own words."""

    relied_on_by: tuple[str, ...] = ()

    def citation(self) -> str:
        return ANNEXES[self.annex].citation(self.volume, self.detail)


#: Only provisions the code already stands on, at a granularity stable enough
#: to state. Anything finer belongs in the publication.
PROVISIONS: tuple[Provision, ...] = (
    Provision(
        annex=2, volume="", detail="Appendix 3",
        what="The table of cruising levels — which set of levels a track "
             "takes. States depart from it, and the departure is what ENR 1.3 "
             "publishes.",
        relied_on_by=("flightrules",),
    ),
    Provision(
        annex=2, volume="", detail="Appendix 1",
        what="Interception visual signals. A crew flying these into a State "
             "that publishes its own is doing the wrong thing confidently.",
        relied_on_by=("interception",),
    ),
    Provision(
        annex=10, volume="Volume V", detail="",
        what="121.500 MHz as the international aeronautical emergency "
             "frequency. Why a published interception frequency list without "
             "it is worth flagging.",
        relied_on_by=("interception",),
    ),
    Provision(
        annex=11, volume="", detail="",
        what="ATS airspace classes A to G and what each promises about "
             "clearance, separation and VFR.",
        relied_on_by=("airspace",),
    ),
    Provision(
        annex=14, volume="Volume I", detail="",
        what="The aerodrome reference code, obstacle limitation surfaces and "
             "rescue and fire fighting categories.",
        relied_on_by=("obstacles", "aircraft", "suitability", "charts"),
    ),
    Provision(
        annex=15, volume="", detail="",
        what="The AIRAC system — the 28-day cycle and its publication "
             "deadlines — and the integrated aeronautical information "
             "package this platform reads.",
        relied_on_by=("airac", "aip", "quality", "notam"),
    ),
)


def annex(number: int) -> Annex:
    """One Annex by number."""
    try:
        return ANNEXES[number]
    except KeyError:
        raise KeyError(
            f"there is no Annex {number}; the Convention has 1 to 19"
        ) from None


def bearing_on(module: str) -> tuple[Annex, ...]:
    """Every Annex a module rests on.

    The question an amendment raises: what does this touch.
    """
    name = module.rsplit(".", 1)[-1]
    return tuple(a for a in ANNEXES.values() if name in a.bears_on)
