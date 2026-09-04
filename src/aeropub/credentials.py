"""Where secrets live, so nobody has to type one twice — and nobody commits one.

The problem this solves is a real one and it is about *use*, not about ceremony:
an operator should set a key once and never think about it again. Retyping a
client secret every session is how people end up pasting it into a file that
gets committed, which is the failure this module exists to prevent by making the
convenient path the safe one.

Where a secret is looked for, in order
--------------------------------------
1. **The process environment.** In a hosted or CI setting this is the right
   answer: the value is set on the *environment* rather than in any file, it
   survives container restarts, and it never touches a disk this project can
   see.
2. **A credentials file outside the repository**, ``~/.aeropub/credentials.json``
   by default and overridable with ``AEROPUB_CREDENTIALS``.

The default path being outside the working tree is the whole design. A secrets
file *inside* a repository gets committed eventually — by a stray ``git add
-A``, by a new clone that has not read the ignore file, by somebody in a hurry.
One that lives in the home directory cannot be committed by accident, because
git has no way to reach it.

What is stored, and what is never stored
----------------------------------------
The value, and nothing else. No copy is taken into any object's state, nothing
is written to a log, and :func:`describe` reports only whether a secret is
present and the shape of it — never the value, not even truncated, because a
prefix of a key is still a prefix of a key.

Rotation
--------
``set_secret`` overwrites in place. When a credential leaks — an emailed
SoapUI project with the secret in plain text is the classic case — rotate it at
the issuer and set the new one here; nothing else in the platform needs to
change, because nothing else ever held it.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

__all__ = [
    "CREDENTIALS_PATH_VAR",
    "DEFAULT_CREDENTIALS_PATH",
    "CredentialStore",
    "MissingCredential",
    "describe",
]

#: Points the store at a different file. Set it in a hosted environment that
#: mounts secrets somewhere specific.
CREDENTIALS_PATH_VAR = "AEROPUB_CREDENTIALS"

#: Deliberately outside any working tree. See the module docstring.
DEFAULT_CREDENTIALS_PATH = Path.home() / ".aeropub" / "credentials.json"


class MissingCredential(LookupError):
    """A secret was needed and is not set anywhere the store looks.

    The message names the variable and the file, because the person hitting
    this is usually not the person who wrote the connector and should not have
    to read the source to find out what to set.
    """


@dataclass(frozen=True, slots=True)
class CredentialStore:
    """Resolves named secrets. Holds none of them.

    ``environ`` is injected rather than read from :mod:`os` at point of use so
    that tests can drive it without touching the real environment. The store
    keeps the caller's mapping; it never copies a value out of it.
    """

    environ: Mapping[str, str] | None = None
    path: Path | None = None

    @property
    def _env(self) -> Mapping[str, str]:
        return self.environ if self.environ is not None else os.environ

    @property
    def file(self) -> Path:
        if self.path is not None:
            return self.path
        override = self._env.get(CREDENTIALS_PATH_VAR)
        return Path(override) if override else DEFAULT_CREDENTIALS_PATH

    def _from_file(self) -> dict[str, str]:
        try:
            loaded = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A missing or unreadable file is not an error: the environment may
            # be supplying everything, which is the better arrangement anyway.
            return {}
        return {
            str(k): str(v)
            for k, v in loaded.items()
            if isinstance(loaded, dict) and v is not None
        }

    def get(self, name: str) -> str | None:
        """The secret, or ``None``. Never raises, never logs."""
        value = self._env.get(name)
        if value:
            return value
        return self._from_file().get(name) or None

    def require(self, name: str, *, purpose: str = "") -> str:
        """The secret, or a message saying exactly how to supply it."""
        value = self.get(name)
        if value:
            return value
        what = f" ({purpose})" if purpose else ""
        raise MissingCredential(
            f"{name} is not set{what}. Supply it either as an environment "
            f"variable — which is the better answer for anything hosted, since "
            f"it survives restarts and touches no disk — or with:\n"
            f"    aeropub credentials --set {name}\n"
            f"which writes to {self.file}, outside any repository so it cannot "
            f"be committed."
        )

    def names(self) -> tuple[str, ...]:
        """Every name the file holds. Names only; no values are returned."""
        return tuple(sorted(self._from_file()))

    def set_secret(self, name: str, value: str) -> Path:
        """Write one secret to the file, creating it 0600.

        Overwrites in place, which is what rotation needs. The directory and
        file are created with owner-only permissions; on a shared machine a
        world-readable secrets file is the same as no secrets file.
        """
        if not name.strip():
            raise ValueError("a credential needs a name")
        if not value.strip():
            raise ValueError(
                f"refusing to store an empty value for {name}. To remove it, "
                "delete the entry rather than blanking it — an empty string "
                "reads as 'set' everywhere it is checked."
            )
        target = self.file
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(target.parent, stat.S_IRWXU)
        except OSError:
            pass

        held = self._from_file()
        held[name.strip()] = value
        target.write_text(json.dumps(held, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return target

    def forget(self, name: str) -> bool:
        held = self._from_file()
        if name not in held:
            return False
        del held[name]
        self.file.write_text(
            json.dumps(held, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return True

    def where(self, name: str) -> str:
        """Which source answered, for a status board. Never the value."""
        if self._env.get(name):
            return "environment"
        if self._from_file().get(name):
            return f"file ({self.file})"
        return "not set"


def describe(value: str | None) -> str:
    """A safe description of a secret: its shape, never its content.

    Not even a truncated prefix. A prefix of a key is still a prefix of a key,
    and the habit of printing "starts with abc..." is how key material ends up
    in screenshots and support tickets.
    """
    if not value:
        return "not set"
    kind = "hex" if re.fullmatch(r"[0-9a-fA-F]+", value) else "mixed"
    return f"set ({len(value)} characters, {kind})"
