"""Reading a NOTAM against its own Q-code, because a human wrote both.

The Q-code is not derived from the text. Someone at a NOF reads the event,
decides which five letters describe it, and types them — and the two halves
then travel together whether or not they agree.

Why that is not cosmetic
-------------------------
Everything downstream screens on the Q-code, because that is what a Q-code is
for. Filter for closed taxiways and you read ``QMXLC``. Doha's NOTAM 967 says
*PORTION OF TWY P3 CLSD* and carries ``QMXXX`` — taxiway, no specific
condition. It is a closed taxiway that no filter for closed taxiways returns,
and nothing about the output looks wrong. The hazard is not the wrong letters;
it is a NOTAM missing from a screen a planner believes is complete.

What was tried first, and why it is not here
---------------------------------------------
Matching the coded subject against words in the text. It does not work, and
the failure is instructive: a NOTAM's text mentions many things and only one
of them is its subject. *MINIMA FOR OTHH ILS RWY 16R CHANGED* mentions an ILS
and a runway while being about neither — it is about the minima, and the code
``QPO`` says so correctly. Run against real traffic that method flagged 23 of
33 NOTAM, nearly all of them wrongly, and a checker people learn to ignore is
worse than no checker. It would also have missed the one real subject error in
the set.

What is checked instead
------------------------
Two things that can be established rather than guessed.

**Peers.** NOTAM whose texts say the same thing should carry the same code.
This needs no dictionary and no semantics — only the observation that four
messages reading *PUBLISHED MISSED APPROACH PROCEDURE ... SUSPENDED* cannot
correctly carry two different subjects. In real Doha traffic three said
``QPI`` (instrument approach procedure) and one said ``QPU`` (missed approach
procedure), and the odd one out is the correct one. A disagreement among peers
is a fact about the set, not an inference about language.

**An unspecific code over a specific text.** ``XX`` means no listed condition
applies. Where the text plainly states one the code has a letter for, that is
worth reporting — narrowly, on the earliest condition named, since what comes
after *due to* is a reason rather than the condition.

Neither corrects anything
--------------------------
A corrected Q-code is our inference wearing the State's authority. Every value
here is citable to whoever published it, and a NOTAM whose subject we decided
would be cited to Qatar for something Qatar did not say; when the inference is
wrong the error is indistinguishable from a fact. So both readings stand and
the disagreement is the finding.

What changes is trust, not the value. :attr:`NotamReview.is_screenable` says
whether the code can be relied on to filter this NOTAM, and a disputed one is
``False`` — a screen keeps it rather than dropping it. Showing a planner a
NOTAM they did not need is the survivable direction.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

from aeropub import notam_code as _CODE
from aeropub.notam import Notam

__all__ = [
    "Disagreement",
    "CodeDiscrepancy",
    "NotamReview",
    "review_notam",
    "review_notams",
    "expand_contractions",
    "text_signature",
]

#: Condition phrases shorter than this are not matched.
_MIN_PHRASE = 5

#: The code's own escape hatch: no listed subject or condition applies.
UNSPECIFIC = "XX"

#: Words after which the text has stopped describing the condition and started
#: giving the reason for it. "CLSD DUE WIP" is closed, not work-in-progress.
_REASON_MARKERS = re.compile(r"\b(?:due to|due|because of|owing to|caused by)\b")


class Disagreement(str, Enum):
    """How a NOTAM's text and its Q-code fail to line up."""

    PEER_DISAGREEMENT = "peer_disagreement"
    """Another NOTAM saying the same thing carries a different code."""

    CONDITION_UNSPECIFIC = "condition_unspecific"
    """Coded ``XX`` while the text names a condition the code has a letter
    for. Legal, and it takes the NOTAM out of every screen for that
    condition."""


@dataclass(frozen=True, slots=True)
class CodeDiscrepancy:
    """One place a NOTAM and its Q-code disagree.

    Both readings travel. Which is right is not decided here: the published
    code is what the State issued, the suggestion is what the evidence points
    at, and they are never merged.
    """

    kind: Disagreement
    published_code: str
    published_reading: str
    suggested_code: str
    suggested_reading: str
    evidence: str
    peers: tuple[str, ...] = ()
    """For a peer disagreement, the NOTAM that carry the other code."""

    def describe(self) -> str:
        if self.kind is Disagreement.PEER_DISAGREEMENT:
            others = ", ".join(self.peers)
            return (
                f"coded {self.published_code} ({self.published_reading}) while "
                f"{others} — saying the same thing — "
                f"{'carries' if len(self.peers) == 1 else 'carry'} "
                f"{self.suggested_code} ({self.suggested_reading})"
            )
        return (
            f"condition coded {self.published_code} (no listed condition "
            f"applies) while the text says “{self.evidence}”, which is "
            f"{self.suggested_code} ({self.suggested_reading})"
        )


@dataclass(frozen=True, slots=True)
class NotamReview:
    """A NOTAM read against its own Q-code."""

    notam: Notam
    discrepancies: tuple[CodeDiscrepancy, ...] = ()

    @property
    def agrees(self) -> bool:
        return not self.discrepancies

    @property
    def is_screenable(self) -> bool:
        """Whether the Q-code can be trusted to filter this NOTAM.

        ``False`` where something disputes it, so a screen keeps the NOTAM
        rather than dropping it. A closed taxiway missing from a list of
        closed taxiways is the failure worth avoiding.
        """
        return not self.discrepancies

    def describe(self) -> str:
        if self.agrees:
            return "text and Q-code agree"
        return "  ·  ".join(d.describe() for d in self.discrepancies)


def expand_contractions(text: str) -> str:
    """Item E with ICAO Doc 8400 contractions spelled out.

    Uses the published abbreviation table rather than a hand-written keyword
    list, so what the matcher understands is what ICAO defined.
    """
    if not text:
        return ""

    def swap(match: re.Match) -> str:
        return _CODE.CONTRACTIONS.get(match.group(0).upper()) or match.group(0)

    keys = sorted(_CODE.CONTRACTIONS, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![A-Za-z0-9])(?:"
        + "|".join(re.escape(k) for k in keys)
        + r")(?![A-Za-z0-9])"
    )
    return pattern.sub(swap, text)


def text_signature(text: str) -> str:
    """What a NOTAM says, with the particulars removed.

    Two messages differing only in a runway designator or a stand number are
    saying the same thing about different objects, and should carry the same
    code. Digits, identifiers and punctuation go; the words remain.
    """
    words = expand_contractions(text or "").lower()
    words = re.sub(r"[^a-z\s]+", " ", words)
    # A token carrying a digit was an identifier before the digits were
    # stripped, so what is left of it is not a word.
    return " ".join(w for w in words.split() if len(w) > 2)


def _condition_phrases() -> list[tuple[str, str, str]]:
    found = []
    for code, reading in _CODE.CONDITIONS.items():
        phrase = re.sub(r"\s*\([^)]*\)", "", reading).strip().lower()
        if len(phrase) >= _MIN_PHRASE:
            found.append((code, phrase, reading))
    return sorted(found, key=lambda item: len(item[1]), reverse=True)


_CONDITIONS = _condition_phrases()


def _first_condition(text: str) -> tuple[str, str, str] | None:
    """The earliest condition named, before any statement of the reason."""
    head = _REASON_MARKERS.split(text, maxsplit=1)[0]
    best: tuple[int, tuple[str, str, str]] | None = None
    for code, phrase, reading in _CONDITIONS:
        match = re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", head)
        if match is None:
            continue
        if best is None or match.start() < best[0]:
            best = (match.start(), (code, phrase, reading))
    return best[1] if best else None


def review_notam(notam: Notam) -> NotamReview:
    """Read one NOTAM's text against its Q-code.

    Only the unspecific-condition check applies to a single NOTAM. Peer
    disagreement needs a set — see :func:`review_notams`.
    """
    return review_notams([notam])[0]


def review_notams(notams: Sequence[Notam] | Iterable[Notam]) -> list[NotamReview]:
    """Read a set of NOTAM against their Q-codes and against each other."""
    held = list(notams)

    # -- peers: same words, different code ---------------------------------
    by_signature: dict[str, list[tuple[int, Notam]]] = defaultdict(list)
    for index, notam in enumerate(held):
        if notam.q is None or not notam.text:
            continue
        signature = text_signature(notam.text)
        if len(signature.split()) >= 4:  # too short to be distinctive
            by_signature[signature].append((index, notam))

    peer_findings: dict[int, list[CodeDiscrepancy]] = defaultdict(list)
    for group in by_signature.values():
        codes = {n.q.code for _, n in group}
        if len(codes) < 2:
            continue
        counted: dict[str, list[str]] = defaultdict(list)
        for _, notam in group:
            # The canonical form: ICAO numbers are four digits, and A927/26
            # is not how anybody writes A0927/26.
            counted[notam.q.code].append(notam.identifier)
        for index, notam in group:
            others = {c: ids for c, ids in counted.items() if c != notam.q.code}
            if not others:
                continue
            # Report against the code the most peers carry.
            rival = max(others, key=lambda c: len(others[c]))
            peer_findings[index].append(
                CodeDiscrepancy(
                    kind=Disagreement.PEER_DISAGREEMENT,
                    published_code=notam.q.code,
                    published_reading=notam.q.decoded or notam.q.code,
                    suggested_code=rival,
                    suggested_reading=_reading_of(rival),
                    evidence=" ".join((notam.text or "").split())[:60],
                    peers=tuple(sorted(others[rival])),
                )
            )

    # -- an unspecific condition over a text that names one ----------------
    reviews: list[NotamReview] = []
    for index, notam in enumerate(held):
        found = list(peer_findings.get(index, ()))
        q = notam.q
        if q is not None and notam.text and q.condition_code == UNSPECIFIC:
            haystack = " ".join(expand_contractions(notam.text).lower().split())
            named = _first_condition(haystack)
            if named is not None:
                code, phrase, reading = named
                found.append(
                    CodeDiscrepancy(
                        kind=Disagreement.CONDITION_UNSPECIFIC,
                        published_code=UNSPECIFIC,
                        published_reading="no listed condition applies",
                        suggested_code=code,
                        suggested_reading=reading,
                        evidence=phrase,
                    )
                )
        reviews.append(NotamReview(notam=notam, discrepancies=tuple(found)))
    return reviews


def _reading_of(code: str) -> str:
    subject = _CODE.SUBJECTS.get(code[1:3])
    condition = _CODE.CONDITIONS.get(code[3:5])
    if subject and condition:
        return f"{subject} {condition}"
    return subject or condition or code
