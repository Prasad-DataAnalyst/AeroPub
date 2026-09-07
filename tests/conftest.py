"""Suite-wide isolation from anything real.

The connector resolves a credential through :class:`CredentialStore`, which
reads the environment and then ``~/.aeropub/credentials.json``. That is right
in production and wrong in a test: a developer with real FAA keys installed
would see tests pass that fail in CI, or — worse — a test asserting "reports
the credential as missing" would quietly start exercising the path where it is
present, and stop testing anything.

So every test runs against a credentials file that does not exist. A test that
wants one builds its own with an explicit path.
"""

from __future__ import annotations

import pytest

from aeropub.credentials import CREDENTIALS_PATH_VAR


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch, tmp_path_factory):
    """Point the credential store at a file nothing has written.

    Both halves are needed. The environment variable covers a store reading
    the real environment; the default path covers the case that actually bit —
    a test injecting its own ``environ`` mapping, where the store looks for the
    override *in that mapping*, does not find it, and falls back to the
    developer's own home directory.
    """
    import aeropub.credentials as module

    nowhere = tmp_path_factory.mktemp("no-credentials") / "credentials.json"
    monkeypatch.setenv(CREDENTIALS_PATH_VAR, str(nowhere))
    monkeypatch.setattr(module, "DEFAULT_CREDENTIALS_PATH", nowhere)
    return nowhere
