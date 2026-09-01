"""Tests for the fixture capture tool.

Capture is exercised against a real ``file://`` URL rather than a stubbed HTTP
layer: real bytes, real hashing, real files written. That keeps the tool's own
tests honest by the same standard it exists to enforce.
"""

import hashlib
import json

import pytest

from aeropub.capture import capture, fixture_dir


class TestCapture:
    def test_writes_the_body_byte_for_byte(self, tmp_path):
        body = b"<html>AIP index</html>\n\x00binary tail"
        src = tmp_path / "served.html"
        src.write_bytes(body)

        capture(src.as_uri(), "example", into=tmp_path / "fx")

        assert (tmp_path / "fx" / "example.raw").read_bytes() == body

    def test_metadata_carries_what_a_citation_needs(self, tmp_path):
        body = b"content"
        src = tmp_path / "served.html"
        src.write_bytes(body)

        meta = capture(src.as_uri(), "example", into=tmp_path / "fx")
        written = json.loads((tmp_path / "fx" / "example.json").read_text())

        assert written == meta
        assert meta["content_hash"] == hashlib.sha256(body).hexdigest()
        assert meta["content_length"] == len(body)
        assert meta["name"] == "example"
        assert meta["url"] == src.as_uri()
        assert meta["fetched_at"].endswith("+00:00")

    def test_creates_the_target_directory(self, tmp_path):
        src = tmp_path / "served.html"
        src.write_bytes(b"x")
        target = tmp_path / "deep" / "nested"
        capture(src.as_uri(), "example", into=target)
        assert (target / "example.raw").exists()

    def test_hash_changes_when_content_does(self, tmp_path):
        src = tmp_path / "served.html"
        src.write_bytes(b"before")
        first = capture(src.as_uri(), "v1", into=tmp_path / "fx")
        src.write_bytes(b"after")
        second = capture(src.as_uri(), "v2", into=tmp_path / "fx")
        assert first["content_hash"] != second["content_hash"]


class TestAuthenticatedCapture:
    """A captured fixture must be safe to commit to a public repository."""

    def test_credentials_never_reach_the_fixture(self, tmp_path):
        src = tmp_path / "served.html"
        src.write_bytes(b"protected content")
        secret = "session=super-secret-token"

        meta = capture(
            src.as_uri(), "protected", into=tmp_path / "fx",
            headers={"Cookie": secret},
        )

        written = (tmp_path / "fx" / "protected.json").read_text()
        assert secret not in written
        assert "super-secret-token" not in written
        assert secret not in json.dumps(meta)

    def test_records_that_a_capture_was_authenticated(self, tmp_path):
        # Enough to know the fixture came from a logged-in session; not enough
        # to repeat the request.
        src = tmp_path / "served.html"
        src.write_bytes(b"x")
        meta = capture(
            src.as_uri(), "protected", into=tmp_path / "fx",
            headers={"Cookie": "session=abc"},
        )
        assert meta["authenticated"] == ["Cookie"]

    def test_unauthenticated_capture_records_nothing(self, tmp_path):
        src = tmp_path / "served.html"
        src.write_bytes(b"x")
        meta = capture(src.as_uri(), "open", into=tmp_path / "fx")
        assert meta["authenticated"] == []


class TestHeaderParsing:
    def test_splits_on_the_first_colon(self):
        from aeropub.capture import _parse_header
        assert _parse_header("Cookie: a=1; b=2") == ("Cookie", "a=1; b=2")

    def test_rejects_a_header_without_a_colon(self):
        from aeropub.capture import _parse_header
        with pytest.raises(ValueError, match="Name: value"):
            _parse_header("Cookie")


class TestFixtureLocation:
    def test_points_at_the_repository_fixtures_directory(self):
        path = fixture_dir()
        assert path.name == "fixtures"
        assert path.parent.name == "tests"
