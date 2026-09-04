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
from pathlib import Path

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


class TestNoNameIsDefinedTwice:
    """A second definition silently replaces the first, and nothing complains.

    This was not hypothetical. ``airac.cycles_between`` enumerated the cycles
    covering a span of dates; a second function of the same name was appended
    to count the gap between two cycles, and it shadowed the original. The
    tests for the original caught it — but only because they existed, and the
    next such collision might land somewhere thinner.
    """

    def module_files(self):
        import aeropub

        root = Path(aeropub.__path__[0])
        return sorted(root.rglob("*.py"))

    def test_no_module_defines_the_same_top_level_name_twice(self):
        import ast

        collisions = []
        for path in self.module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            seen: dict[str, int] = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names = [node.name]
                elif isinstance(node, ast.Assign):
                    names = [
                        t.id for t in node.targets if isinstance(t, ast.Name)
                    ]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names = [node.target.id]
                else:
                    continue
                for name in names:
                    # A bare annotation re-stating a type is not a redefinition,
                    # and neither is the conventional `x = x` re-export.
                    if name in seen:
                        collisions.append(f"{path.name}:{node.lineno} {name} "
                                          f"(first at line {seen[name]})")
                    seen[name] = node.lineno
        assert collisions == [], (
            "these names are defined twice at module level; the second "
            "silently replaces the first:\n  " + "\n  ".join(collisions)
        )

    def test_no_exported_name_is_served_by_two_modules(self):
        # Two modules exporting the same name into the package means whichever
        # import runs last wins, and which one that is depends on line order.
        import importlib
        import pkgutil

        import aeropub

        providers: dict[str, list[str]] = {}
        for info in pkgutil.walk_packages(aeropub.__path__, prefix="aeropub."):
            module = importlib.import_module(info.name)
            for name in getattr(module, "__all__", ()):
                if name in aeropub.__all__:
                    providers.setdefault(name, []).append(info.name)
        shared = {
            name: sorted(where)
            for name, where in providers.items()
            if len(where) > 1
        }
        # A name re-exported by a module that imported it is fine; a name two
        # modules each *define* is not.
        genuine = {
            name: where
            for name, where in shared.items()
            if len({
                getattr(importlib.import_module(m), name).__module__
                for m in where
                if hasattr(getattr(importlib.import_module(m), name), "__module__")
            }) > 1
        }
        assert genuine == {}, f"defined in more than one module: {genuine}"
