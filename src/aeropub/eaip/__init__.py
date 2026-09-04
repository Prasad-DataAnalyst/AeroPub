"""Reading an eAIP — by profile, never by guesswork.

Why this is a package and not a parser
--------------------------------------
There are around 180 States and no universal eAIP reader. Many use the
EUROCONTROL eAIP toolchain and produce structurally similar HTML; many do not,
and several that do have customised it. A single hard-coded parser fits the
State it was written against and quietly mis-reads the next one, which is
worse than failing.

The earlier plan was: capture a page from one State, send it to whoever writes
parsers, get a parser back. That makes 180 States a queue with one person in
it, and it is the wrong shape. So the layout of each State's eAIP is
**configuration** — a :class:`~aeropub.eaip.profile.EaipProfile` in a JSON file
— and :mod:`aeropub.eaip.probe` reads a real page and writes a draft of that
configuration from what it actually finds. An AIS officer with the page in
front of them can onboard their own State without anybody writing code.

The same principle as the FAA connector, which the operator can re-point at a
moved endpoint from a JSON overlay while it runs. Where a source lives, and now
how it is laid out, is data.

What is never guessed
---------------------
A profile that does not match the page **fails, loudly, naming what it looked
for and what it found instead**. There is no fallback that scrapes something
plausible, no "best effort" extraction, and no silent partial parse. A value
this package emits was found where the profile said it would be, or it was not
emitted at all — and the section it came from is then reported as a coverage
gap, which is a true statement, rather than as a value somebody might act on.

The probe reports; it does not conclude. It says "this document has 25 elements
whose id matches AD-2.\\d+" — it does not decide that they are the AD 2 sections.
A person confirms the draft profile before it is used, and
:attr:`EaipProfile.verified_at` records that they did, exactly as the source
registry distinguishes a registered URL from a verified one.
"""

from aeropub.eaip.profile import (
    EaipProfile,
    FieldRule,
    ProfileError,
    SectionRule,
    load_layout,
)

__all__ = [
    "EaipProfile",
    "FieldRule",
    "ProfileError",
    "SectionRule",
    "load_layout",
]
