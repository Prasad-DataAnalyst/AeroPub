"""Tests for the fixture capture tool.

Capture is exercised against a real ``file://`` URL rather than a stubbed HTTP
layer: real bytes, real hashing, real files written. That keeps the tool's own
tests honest by the same standard it exists to enforce.
"""

import hashlib
import json

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


class TestFixtureLocation:
    def test_points_at_the_repository_fixtures_directory(self):
        path = fixture_dir()
        assert path.name == "fixtures"
        assert path.parent.name == "tests"
