"""Reading one discovered publication into cited facts.

Three things are decided here and each is easy to get quietly wrong: what the
document actually is, whether the citation it produces can still be resolved
next cycle, and whether "nothing found" is distinguishable from "never looked".
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aeropub.live import Retention
from aeropub.provenance import Confidence
from aeropub.publication import Edition, Kind, Publication
from aeropub.reader import Retrieved, media_type_of, read_publication

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
EDITION = Edition(index_url="https://aim.gov.qa/AIP/x/html/index-en-GB.html")

SECTION = Publication(
    url="https://aim.gov.qa/AIP/x/html/eAIP/QA-ENR-3.2-en-GB.html",
    kind=Kind.AIP_SECTION,
    edition=EDITION,
    code="ENR 3.2",
)
CHART = Publication(
    url="https://aim.gov.qa/AIP/x/graphics/OTHH-ADC.pdf",
    kind=Kind.CHART,
    edition=EDITION,
    title="Aerodrome chart OTHH",
)


def served(body: bytes, media_type: str = "", url: str = SECTION.url) -> Retrieved:
    resolved, declared = media_type_of(url, body, media_type)
    return Retrieved(
        url=url, body=body, media_type=resolved,
        type_was_declared=declared, retrieved_at=NOW,
    )


def retriever(body: bytes, media_type: str = ""):
    def _retrieve(url: str) -> Retrieved:
        return served(body, media_type, url)
    return _retrieve


def archiver(calls: list):
    def _keep(body: bytes, media_type: str) -> str:
        calls.append((len(body), media_type))
        return f"sha256:{len(body):08x}"
    return _keep


def one_fact(got, publication):
    return ("a fact",)


class TestKnowingWhatItIs:

    def test_the_servers_declaration_is_used(self):
        media_type, declared = media_type_of(
            "https://x.test/page", b"<html></html>", "text/html; charset=UTF-8"
        )
        assert (media_type, declared) == ("text/html", True)

    def test_the_extension_stands_in_when_nothing_is_declared(self):
        assert media_type_of("https://x.test/a.pdf", b"junk")[0] == "application/pdf"

    def test_an_undeclared_type_is_marked_undeclared(self):
        """A caller must be able to tell a determination from a guess."""
        assert media_type_of("https://x.test/a.pdf", b"junk")[1] is False

    def test_the_bytes_beat_a_wrong_declaration(self):
        """A State serving a PDF as text/html is common. Archiving its bytes
        as markup wastes exactly the storage retention exists to save."""
        media_type, _ = media_type_of(
            "https://x.test/sup", b"%PDF-1.7\n...", "text/html"
        )
        assert media_type == "application/pdf"

    def test_markup_is_recognised_with_no_extension_and_no_header(self):
        assert media_type_of("https://x.test/page", b"<!DOCTYPE html><p>")[0] == (
            "text/html"
        )

    def test_an_unidentifiable_document_is_not_guessed_as_markup(self):
        assert media_type_of("https://x.test/blob", b"\x00\x01\x02")[0] == (
            "application/octet-stream"
        )


class TestWhatIsKept:

    def test_a_parsed_section_is_archived(self):
        calls: list = []
        result = read_publication(
            SECTION, retriever(b"<html>ENR 3.2</html>"),
            parse=one_fact, keep=archiver(calls),
        )
        assert result.link.retention is Retention.ARCHIVED
        assert result.link.archive_key
        assert calls == [(20, "text/html")]

    def test_a_parsed_pdf_keeps_its_extract_not_its_bytes(self):
        result = read_publication(
            Publication(
                url="https://aim.gov.qa/AIP/x/eSUP/QA-SUP-16-2026.pdf",
                kind=Kind.SUPPLEMENT, edition=EDITION, code="SUP 16/2026",
            ),
            retriever(b"%PDF-1.7 supplement"),
            parse=one_fact, keep=archiver([]),
        )
        assert result.link.retention is Retention.EXTRACT_ONLY

    def test_a_chart_is_never_archived(self):
        calls: list = []
        result = read_publication(CHART, retriever(b"%PDF-1.7 chart"), keep=archiver(calls))
        assert result.link.retention is Retention.LINKED
        assert calls == []

    def test_a_chart_still_gets_a_hash(self):
        """It is what catches a State republishing under the same URL."""
        result = read_publication(CHART, retriever(b"%PDF-1.7 chart"))
        assert len(result.link.content_hash) == 64


class TestACitationThatWillNotResolve:
    """Qatar withdraws editions and keeps no archive. A citation resting on a
    link stops resolving within one AIRAC cycle."""

    def test_facts_are_still_produced_with_no_archive(self):
        """A storage fault of ours must not deny a State's data."""
        result = read_publication(SECTION, retriever(b"<html>x</html>"), parse=one_fact)
        assert result.parsed
        assert result.facts == ("a fact",)

    def test_but_they_are_marked_low(self):
        result = read_publication(SECTION, retriever(b"<html>x</html>"), parse=one_fact)
        assert result.confidence is Confidence.LOW

    def test_and_the_reason_is_recorded(self):
        result = read_publication(SECTION, retriever(b"<html>x</html>"), parse=one_fact)
        assert "resolves only while the State serves" in result.degraded_because

    def test_such_a_value_is_not_operationally_usable(self):
        result = read_publication(SECTION, retriever(b"<html>x</html>"), parse=one_fact)
        assert not result.is_usable_operationally

    def test_an_archived_one_is(self):
        result = read_publication(
            SECTION, retriever(b"<html>x</html>"), parse=one_fact, keep=archiver([])
        )
        assert result.is_usable_operationally

    def test_an_archive_returning_no_key_is_treated_as_no_archive(self):
        """Silently accepting an empty key would produce a citation naming a
        copy that does not exist."""
        result = read_publication(
            SECTION, retriever(b"<html>x</html>"),
            parse=one_fact, keep=lambda body, media_type: "",
        )
        assert result.link.retention is Retention.LINKED
        assert result.confidence is Confidence.LOW


class TestNotLookedIsNotNotFound:

    def test_no_parser_means_not_parsed(self):
        result = read_publication(SECTION, retriever(b"<html>x</html>"), keep=archiver([]))
        assert not result.parsed
        assert result.not_parsed_because == "no parser was given"

    def test_zero_facts_with_a_parser_still_counts_as_parsed(self):
        """Nothing found is a coverage gap. Never looking is a different thing
        with the same output, and they must not read the same."""
        result = read_publication(
            SECTION, retriever(b"<html></html>"),
            parse=lambda got, pub: (), keep=archiver([]),
        )
        assert result.parsed
        assert result.facts == ()

    def test_a_chart_is_not_parsed_even_with_a_parser(self):
        result = read_publication(CHART, retriever(b"%PDF-1.7"), parse=one_fact)
        assert not result.parsed
        assert "carries no values" in result.not_parsed_because

    def test_the_description_says_which_happened(self):
        unparsed = read_publication(SECTION, retriever(b"<x/>"), keep=archiver([]))
        assert "not parsed" in unparsed.describe()
        parsed = read_publication(
            SECTION, retriever(b"<x/>"), parse=one_fact, keep=archiver([])
        )
        assert "1 facts" in parsed.describe()


class TestTheRecordOfWhatWasRead:

    def test_the_hash_is_of_exactly_what_was_read(self):
        import hashlib

        body = b"<html>ENR 3.2</html>"
        result = read_publication(SECTION, retriever(body), keep=archiver([]))
        assert result.link.content_hash == hashlib.sha256(body).hexdigest()

    def test_the_size_is_recorded(self):
        result = read_publication(SECTION, retriever(b"12345"), keep=archiver([]))
        assert result.link.size == 5

    def test_the_state_url_survives(self):
        result = read_publication(SECTION, retriever(b"<x/>"), keep=archiver([]))
        assert result.link.url == SECTION.url


class TestTheCitationNamesTheAuthority:

    def test_the_state_is_named_when_given(self):
        result = read_publication(
            SECTION, retriever(b"<x/>"), state_name="Qatar", keep=archiver([])
        )
        assert result.link.document == "AIP Qatar ENR 3.2"

    def test_without_a_state_the_document_is_still_identified(self):
        """Weaker, but never a stray separator or a bare authority name."""
        result = read_publication(SECTION, retriever(b"<x/>"), keep=archiver([]))
        assert result.link.document == "ENR 3.2"

    def test_a_supplement_is_not_titled_aip(self):
        """Only an AIP section is 'AIP Qatar'. A supplement is not part of it."""
        supplement = Publication(
            url="https://aim.gov.qa/AIP/x/eSUP/QA-SUP-16-2026.pdf",
            kind=Kind.SUPPLEMENT, edition=EDITION, code="SUP 16/2026",
        )
        result = read_publication(
            supplement, retriever(b"%PDF-1.7"), state_name="Qatar",
            parse=one_fact, keep=archiver([]),
        )
        assert result.link.document == "Qatar SUP 16/2026"


class TestKeptBecauseItIsASection:
    """Retention follows the document, not whether a parser happened to run.

    Keying it on "was it parsed" meant a State onboarded before its profile
    was written archived nothing, and when the profile arrived there was no
    history to read it against. Two things need the bytes and only one is
    parsing: a hash says *that* a section changed, and only the previous bytes
    say *what* changed.
    """

    def test_a_section_with_no_parser_is_still_archived(self):
        calls: list = []
        result = read_publication(
            SECTION, retriever(b"<html>ENR 3.2</html>"), keep=archiver(calls)
        )
        assert not result.parsed
        assert result.link.retention is Retention.ARCHIVED
        assert calls

    def test_it_is_the_baseline_a_later_diff_reads_against(self):
        result = read_publication(
            SECTION, retriever(b"<html>ENR 3.2</html>"), keep=archiver([])
        )
        assert not result.citation_will_expire

    def test_a_chart_with_no_parser_is_still_not_archived(self):
        """The rule is the document's kind, not the absence of a parser."""
        calls: list = []
        read_publication(CHART, retriever(b"%PDF-1.7"), keep=archiver(calls))
        assert calls == []
