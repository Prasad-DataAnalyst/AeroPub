"""Reading a credential out of the pack an authority actually ships.

The FAA does not hand over a client id and a secret in a form anything can
consume. It ships a SoapUI project — an XML file with the OAuth2 profile
filled in, the client id, the client secret and whatever bearer token the
person who exported it happened to be holding, all in plain text. The FAA's
own FAQ says not to send those in the clear, and then the onboarding pack does
exactly that.

What happens next is the part worth engineering away. Somebody opens the file,
finds the secret, and copies it — into a terminal, where it lands in shell
history and in the process list; into a chat window, to ask a colleague which
field is which; into a screenshot for a ticket. Every one of those is a copy of
a live credential in a place nobody is tracking, and the secret is 64
characters of opaque text so nobody notices it in a scrollback.

So this reads the pack directly into the credential store. The value goes from
the file the authority sent to a mode-600 file outside any repository, and is
never rendered on the way. Nothing here returns a secret to a caller that did
not already have the file, nothing prints one, and the summary an operator sees
names fields and lengths.

Two shapes, one problem
------------------------
The FAA sends the credential twice: as an encrypted spreadsheet with a **Key**
and **Secret** row — the password arriving in a separate email — and inside the
SoapUI project's OAuth2 profile. Both are read here, because the alternative
in both cases is a person retyping a 64-character opaque string.

That is not a hypothetical cost. Transcribing this project's own secret from a
photograph of the spreadsheet got one character wrong out of sixty-four: a
lowercase ``l`` read as a capital ``I``, which in most screen fonts is the same
picture. The resulting 401 is indistinguishable from a revoked key, so the
hours go into chasing the credential rather than the typo. Reading the file is
the only version of this that cannot be wrong.

What is deliberately not imported
----------------------------------
The **access token**. A bearer in an exported project is minutes old at best
and is a credential in its own right; storing one buys nothing — the client
mints its own from the id and secret on demand — and leaves a third secret
lying around to leak. It is reported as present so the operator knows the pack
is hazardous, and dropped.

This does not make the pack safe. A file that has been emailed carries a
credential that has been emailed, and the remedy for that is rotation, which
only the authority can do. The importer says so every time.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "PackError",
    "OnboardingPack",
    "read_soapui_pack",
    "read_spreadsheet_pack",
    "read_pack",
    "SOAPUI_FIELDS",
    "SPREADSHEET_ROWS",
]

#: The row labels the FAA's onboarding spreadsheet uses, and what each is to
#: us. Matched case-insensitively on the label cell; the value is the cell
#: beside it.
SPREADSHEET_ROWS: dict[str, str] = {
    "key": "AEROPUB_FAA_CLIENT_ID",
    "secret": "AEROPUB_FAA_CLIENT_SECRET",
}


class PackError(ValueError):
    """The file is not a pack we can read, or does not carry what we need."""


#: The OAuth2 profile fields SoapUI writes, and what each one is to us.
#: ``None`` means recognised and deliberately not imported.
SOAPUI_FIELDS: dict[str, str | None] = {
    "clientID": "AEROPUB_FAA_CLIENT_ID",
    "clientSecret": "AEROPUB_FAA_CLIENT_SECRET",
    "accessToken": None,
    "refreshToken": None,
}


def _text(raw: str, tag: str) -> str | None:
    """One element's text, namespace prefix or not."""
    found = re.search(rf"<(?:\w+:)?{re.escape(tag)}>([^<]*)</(?:\w+:)?{re.escape(tag)}>", raw)
    if found is None:
        return None
    value = found.group(1).strip()
    return value or None


@dataclass(frozen=True, slots=True)
class OnboardingPack:
    """What a pack carries. Secrets are held, never rendered."""

    path: Path
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    """Credential-store name to value. Excluded from ``repr`` deliberately:
    a dataclass that printed itself into a log would undo the whole point."""

    token_url: str = ""
    endpoint: str = ""
    dropped: tuple[str, ...] = ()
    """Credential-shaped fields recognised and not imported, by pack name."""

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return (
            f"OnboardingPack(path={self.path!r}, "
            f"installs={sorted(self.secrets)!r}, dropped={self.dropped!r})"
        )

    @property
    def environment_hint(self) -> str:
        """Which environment the pack is for, read off its own token URL.

        Worth having: a pack is issued for one environment, and running its
        credentials against another produces a 401 that says nothing about
        why. The FAA's pre-production host is not its production host.
        """
        host = self.token_url or self.endpoint
        if "api-staging." in host:
            return "staging"
        if "api-sit." in host:
            return "fit"
        if "aim.faa.gov" in host:
            return "prod"
        return ""

    def summary(self) -> str:
        """What was found, by name and length. Never a value."""
        lines = [f"pack: {self.path}"]
        if self.endpoint:
            lines.append(f"  endpoint      {self.endpoint}")
        if self.token_url:
            lines.append(f"  token URL     {self.token_url}")
        if self.environment_hint:
            lines.append(f"  environment   {self.environment_hint} (from the pack's own host)")
        for name in sorted(self.secrets):
            lines.append(f"  will install  {name}  ({len(self.secrets[name])} characters)")
        for name in self.dropped:
            lines.append(
                f"  will DROP     {name} — a bearer token in an exported "
                "project is stale, and the client mints its own"
            )
        return "\n".join(lines)


def read_soapui_pack(path: Path | str) -> OnboardingPack:
    """Read a SoapUI project's OAuth2 profile.

    Raises :class:`PackError` rather than returning something half-read: a
    partially imported credential pair produces a 401 that says nothing about
    which half is wrong, which is the failure this whole module exists to make
    impossible.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise PackError(f"{path}: cannot be read — {error}") from None

    if "soapui-project" not in raw:
        raise PackError(
            f"{path}: not a SoapUI project. This reads the project XML the FAA "
            "ships with registration; for anything else use "
            "'aeropub credentials --set NAME'."
        )

    secrets: dict[str, str] = {}
    dropped: list[str] = []
    for tag, name in SOAPUI_FIELDS.items():
        value = _text(raw, tag)
        if value is None:
            continue
        if name is None:
            dropped.append(tag)
            continue
        secrets[name] = value

    wanted = {n for n in SOAPUI_FIELDS.values() if n is not None}
    absent = sorted(wanted - set(secrets))
    if absent:
        raise PackError(
            f"{path}: the OAuth2 profile does not carry {', '.join(absent)}. "
            "An FAA pack fills clientID and clientSecret on the profile named "
            "in selectedAuthProfile; one exported before the keys were issued "
            "carries neither."
        )

    return OnboardingPack(
        path=path,
        secrets=secrets,
        token_url=_text(raw, "accessTokenURI") or "",
        endpoint=_text(raw, "endpoint") or "",
        dropped=tuple(dropped),
    )


def read_spreadsheet_pack(
    path: Path | str, *, password: str | None = None
) -> OnboardingPack:
    """Read the FAA's onboarding spreadsheet — Key and Secret rows.

    The workbook is encrypted and its password arrives in a separate email, so
    both are needed. A wrong password is reported as a wrong password rather
    than as a malformed file, because those send a person to different places.

    Raises :class:`PackError` for anything it cannot read, including a
    workbook that opens but carries no such rows.
    """
    path = Path(path)
    if not path.exists():
        raise PackError(f"{path}: cannot be read — no such file")

    try:
        import openpyxl
    except ImportError:  # pragma: no cover - environment-dependent
        raise PackError(
            "reading a spreadsheet needs openpyxl (pip install openpyxl); "
            "or use 'aeropub credentials --set NAME' and paste the values."
        ) from None

    handle: object
    if password:
        try:
            import msoffcrypto
        except ImportError:  # pragma: no cover - environment-dependent
            raise PackError(
                "an encrypted workbook needs msoffcrypto-tool "
                "(pip install msoffcrypto-tool)."
            ) from None
        buffer = io.BytesIO()
        try:
            with open(path, "rb") as raw:
                office = msoffcrypto.OfficeFile(raw)
                office.load_key(password=password)
                office.decrypt(buffer)
        except Exception as error:  # msoffcrypto raises several types
            raise PackError(
                f"{path}: could not be decrypted — {type(error).__name__}. "
                "The FAA sends this workbook's password in a separate email "
                "from the workbook itself."
            ) from None
        buffer.seek(0)
        handle = buffer
    else:
        handle = path

    try:
        book = openpyxl.load_workbook(handle, data_only=True, read_only=True)
    except Exception as error:
        raise PackError(
            f"{path}: could not be opened — {type(error).__name__}. "
            "If it is password-protected, supply the password."
        ) from None

    secrets: dict[str, str] = {}
    try:
        for sheet in book.worksheets:
            for row in sheet.iter_rows():
                cells = [c.value for c in row if c.value is not None]
                if len(cells) < 2:
                    continue
                label = str(cells[0]).strip().casefold()
                name = SPREADSHEET_ROWS.get(label)
                if name is not None and name not in secrets:
                    value = str(cells[1]).strip()
                    if value:
                        secrets[name] = value
    finally:
        book.close()

    absent = sorted(set(SPREADSHEET_ROWS.values()) - set(secrets))
    if absent:
        raise PackError(
            f"{path}: no rows labelled "
            f"{' and '.join(sorted(SPREADSHEET_ROWS))} carrying a value. The "
            "FAA's onboarding spreadsheet puts the label in one column and "
            "the value beside it."
        )

    return OnboardingPack(path=path, secrets=secrets)


def read_pack(path: Path | str, *, password: str | None = None) -> OnboardingPack:
    """Read whichever onboarding artefact this is, by extension.

    An operator has whatever the FAA emailed them and should not have to know
    which reader it needs.
    """
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return read_spreadsheet_pack(path, password=password)
    return read_soapui_pack(path)
