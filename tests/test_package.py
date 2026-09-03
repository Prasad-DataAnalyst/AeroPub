"""The public surface of ``import aeropub``.

None of this is clever, and that is why it is here. ``__init__.py`` grew by
prepending — a new module's names went on top of ``__all__``, a new import went
below the last one — and it accumulated a duplicated import and four duplicated
exports before anything noticed, because nothing was looking. A duplicate in
``__all__`` is harmless; a name in ``__all__`` that no longer resolves is an
``ImportError`` in somebody else's code, and the two arrive by the same route.
"""

from __future__ import annotations

import importlib
import pkgutil

import aeropub


class TestTheExportedSurface:
    def test_every_exported_name_resolves(self):
        missing = [name for name in aeropub.__all__ if not hasattr(aeropub, name)]
        assert not missing, (
            f"__all__ names something the package does not have: {missing}. "
            "A caller writing 'from aeropub import ...' gets an ImportError."
        )

    def test_no_name_is_exported_twice(self):
        seen: set[str] = set()
        duplicates = sorted({n for n in aeropub.__all__ if n in seen or seen.add(n)})
        assert not duplicates, f"__all__ lists these more than once: {duplicates}"

    def test_the_listing_is_sorted(self):
        # Sorted so the next addition has one obvious place to go, rather than
        # being prepended and hiding a name already further down.
        assert aeropub.__all__ == sorted(aeropub.__all__, key=str.lower)

    def test_star_import_matches_the_listing(self):
        namespace: dict[str, object] = {}
        exec("from aeropub import *", namespace)  # noqa: S102 - that is the test
        exported = {k for k in namespace if not k.startswith("__")}
        assert exported == set(aeropub.__all__)


class TestEveryModuleImports:
    """Import each module on its own, so a broken one cannot hide.

    ``import aeropub`` does not pull in the connectors — that is deliberate, so
    the import costs nothing a caller has not asked for. The cost of that
    choice is that a syntax error or a circular import inside a connector stays
    invisible until someone reaches for it. This walks the whole package.
    """

    def test_all_modules_import_cleanly(self):
        failures = []
        for info in pkgutil.walk_packages(aeropub.__path__, prefix="aeropub."):
            try:
                importlib.import_module(info.name)
            except Exception as error:  # noqa: BLE001 - reporting all of them
                failures.append(f"{info.name}: {type(error).__name__}: {error}")
        assert not failures, "modules that do not import:\n" + "\n".join(failures)

    def test_the_connectors_are_not_imported_by_the_package(self):
        # Stated in the package docstring as a promise to callers. Verified by
        # reading the source rather than sys.modules, which another test in the
        # same session may already have populated.
        source = (aeropub.__path__[0] + "/__init__.py")
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        assert "from aeropub.faa" not in text
        assert "import aeropub.faa" not in text
