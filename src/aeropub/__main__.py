"""``python -m aeropub`` — see :mod:`aeropub.cli`.

Guarded, because this module is importable like any other and something that
walks the package to check every module imports would otherwise run the CLI
and exit the process. Under ``python -m aeropub`` this file *is* ``__main__``,
so the guard costs nothing there.
"""

from aeropub.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
