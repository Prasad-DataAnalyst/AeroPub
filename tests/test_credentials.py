"""Where secrets live, and the guard that stops one reaching the repository.

Two things are tested. The store itself — which must never print, copy or log a
value — and the repository, which must never contain one.

The second is here because of a real incident: the FAA's own onboarding pack
arrived with a SoapUI project carrying the client id, the client secret and a
live bearer token in plain text, and the FAA's own FAQ tells registrants not to
send those in the clear. A file like that is easy to commit by accident and
impossible to un-commit, since git history is permanent. So the test scans the
tree on every run.
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
from pathlib import Path

import pytest

from aeropub.credentials import (
    CREDENTIALS_PATH_VAR,
    CredentialStore,
    MissingCredential,
    describe,
)


@pytest.fixture
def store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(environ={}, path=tmp_path / "credentials.json")


class TestResolution:
    def test_the_environment_wins(self, tmp_path):
        held = CredentialStore(
            environ={"KEY": "from-environment"}, path=tmp_path / "c.json"
        )
        held.set_secret("KEY", "from-file")
        # Hosted deployments set the environment, and it must not be shadowed
        # by a stale file somebody left behind.
        assert held.get("KEY") == "from-environment"

    def test_the_file_answers_when_the_environment_does_not(self, store):
        store.set_secret("KEY", "from-file")
        assert store.get("KEY") == "from-file"

    def test_an_absent_secret_is_none_rather_than_an_error(self, store):
        assert store.get("NOTHING") is None

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        # The environment may be supplying everything, which is the better
        # arrangement anyway.
        assert CredentialStore(environ={}, path=tmp_path / "absent.json").get("K") is None

    def test_a_corrupt_file_is_not_an_error(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{not json", encoding="utf-8")
        assert CredentialStore(environ={}, path=path).get("K") is None

    def test_the_path_variable_redirects_the_store(self, tmp_path):
        elsewhere = tmp_path / "mounted" / "secrets.json"
        held = CredentialStore(environ={CREDENTIALS_PATH_VAR: str(elsewhere)})
        assert held.file == elsewhere

    def test_the_default_path_is_outside_any_repository(self):
        # The whole design. A secrets file inside a working tree gets committed
        # eventually; one in the home directory cannot be, because git has no
        # way to reach it.
        default = CredentialStore(environ={}).file
        assert default.is_absolute()
        assert not Path.cwd() in default.parents


class TestRequire:
    def test_it_names_the_variable_and_the_file(self, store):
        # The person hitting this is usually not the one who wrote the
        # connector, and should not have to read the source to find out what
        # to set.
        with pytest.raises(MissingCredential) as caught:
            store.require("AEROPUB_FAA_CLIENT_SECRET", purpose="FAA NMS-API")
        message = str(caught.value)
        assert "AEROPUB_FAA_CLIENT_SECRET" in message
        assert "FAA NMS-API" in message
        assert str(store.file) in message
        assert "credentials --set" in message

    def test_it_returns_the_value_when_set(self, store):
        store.set_secret("KEY", "value")
        assert store.require("KEY") == "value"


class TestWriting:
    def test_it_overwrites_in_place_for_rotation(self, store):
        store.set_secret("KEY", "old")
        store.set_secret("KEY", "new")
        assert store.get("KEY") == "new"

    def test_other_secrets_survive_a_rotation(self, store):
        store.set_secret("A", "one")
        store.set_secret("B", "two")
        store.set_secret("A", "three")
        assert store.get("B") == "two"

    def test_the_file_is_owner_only(self, store):
        written = store.set_secret("KEY", "value")
        mode = stat.S_IMODE(written.stat().st_mode)
        # On a shared machine a world-readable secrets file is the same as no
        # secrets file.
        assert not mode & (stat.S_IRGRP | stat.S_IROTH)

    def test_an_empty_value_is_refused(self, store):
        # An empty string reads as "set" everywhere it is checked.
        with pytest.raises(ValueError) as caught:
            store.set_secret("KEY", "   ")
        assert "reads as 'set'" in str(caught.value)

    def test_forgetting_removes_only_that_one(self, store):
        store.set_secret("A", "one")
        store.set_secret("B", "two")
        assert store.forget("A")
        assert store.get("A") is None
        assert store.get("B") == "two"

    def test_forgetting_something_absent_says_so(self, store):
        assert not store.forget("NOTHING")


class TestNothingLeaks:
    def test_describe_reports_shape_and_never_content(self):
        # Not even a truncated prefix: a prefix of a key is still a prefix of a
        # key, and "starts with abc..." is how key material reaches screenshots
        # and support tickets.
        secret = "nEOBbhbk0ODE58cLAGIEEzYNnNQ4VOQp"
        shown = describe(secret)
        assert secret not in shown
        for length in range(4, len(secret)):
            assert secret[:length] not in shown
        assert "32 characters" in shown

    def test_describe_handles_nothing(self):
        assert describe(None) == "not set"
        assert describe("") == "not set"

    def test_names_returns_names_and_not_values(self, store):
        store.set_secret("KEY", "the-secret-value")
        assert store.names() == ("KEY",)
        assert "the-secret-value" not in str(store.names())

    def test_where_reports_the_source_and_not_the_value(self, store):
        store.set_secret("KEY", "the-secret-value")
        assert "the-secret-value" not in store.where("KEY")
        assert "file" in store.where("KEY")

    def test_the_store_holds_no_copy_of_any_value(self, store):
        store.set_secret("KEY", "the-secret-value")
        store.get("KEY")
        # It reads through to the file every time rather than caching, so there
        # is no copy to leak through a repr, a traceback or a pickle.
        assert "the-secret-value" not in repr(store)
        # slots, so there is no __dict__ to inspect; check every declared field.
        held = {
            name: getattr(store, name, None)
            for name in getattr(type(store), "__slots__", ())
        }
        assert "the-secret-value" not in str(held)


class TestTheRepositoryHoldsNoSecret:
    """A scan of the tree, run on every test run.

    The FAA's onboarding pack arrived with a SoapUI project carrying a client
    id, a client secret and a live bearer token in plain text. Files like that
    are easy to commit and impossible to un-commit — git history is permanent
    and a repository's visibility can change after the fact.
    """

    def repository(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def tracked_files(self) -> list[Path]:
        root = self.repository()
        try:
            listed = subprocess.run(
                ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
            ).stdout.decode()
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("not a git checkout")
        return [root / name for name in listed.split("\0") if name]

    #: Shapes that are credentials rather than code. Long unbroken runs of
    #: base64-ish characters are what an OAuth2 client secret looks like.
    SUSPECT = (
        re.compile(r"\bclientSecret\b", re.I),
        re.compile(r"\bclient_secret\b\s*[:=]\s*['\"][A-Za-z0-9+/=_-]{16,}"),
        re.compile(r"\baccessToken\b\s*>\s*[A-Za-z0-9+/=_-]{16,}"),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    )

    def test_no_tracked_file_carries_credential_shaped_content(self):
        offenders: list[str] = []
        for path in self.tracked_files():
            if path.suffix in (".png", ".jpg", ".gz", ".pdf", ".xlsx"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if path.name == "test_credentials.py":
                continue  # this file names the patterns it looks for
            for pattern in self.SUSPECT:
                found = pattern.search(text)
                if found:
                    offenders.append(
                        f"{path.relative_to(self.repository())}: "
                        f"matches /{pattern.pattern}/"
                    )
        assert offenders == [], (
            "these tracked files look like they carry a credential. Git history "
            "is permanent and a repository's visibility can change, so remove "
            "the value, rotate it at the issuer, and store it with "
            "`aeropub credentials --set`:\n  " + "\n  ".join(offenders)
        )

    def test_the_credentials_file_is_not_tracked(self):
        tracked = {p.name for p in self.tracked_files()}
        assert "credentials.json" not in tracked
