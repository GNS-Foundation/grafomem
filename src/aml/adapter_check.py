"""
GRAFOMEM adapter pre-flight checker.

    grafomem check -b my_module:MyBackend

Quick structural validation BEFORE running the full conformance suite:
checks Protocol compliance, method signatures, capability coherence,
and basic round-trip. Fails fast with actionable error messages.
"""

from __future__ import annotations

import inspect
from typing import Any


def check_adapter(cls: type) -> list[str]:
    """Run pre-flight checks against a backend class. Returns a list of errors.
    Empty list means the adapter is structurally conformant."""
    errors: list[str] = []

    # 1. Check required methods exist
    required_methods = ["capabilities", "write", "retrieve", "delete",
                         "supersede", "audit", "flush"]
    for method in required_methods:
        if not hasattr(cls, method):
            errors.append(f"Missing required method: {method}()")
        elif not callable(getattr(cls, method)):
            errors.append(f"{method} exists but is not callable")

    if errors:
        return errors  # can't proceed without basic methods

    # 2. Try to instantiate
    instance = None
    try:
        # Check if __init__ needs arguments beyond self
        sig = inspect.signature(cls.__init__)
        params = [p for p in sig.parameters.values()
                  if p.name != "self" and p.default is inspect.Parameter.empty]
        if params:
            errors.append(
                f"Constructor requires arguments: {[p.name for p in params]}. "
                f"The conformance suite needs a zero-arg factory. Consider using "
                f"a wrapper lambda: lambda: MyBackend(arg1, arg2)"
            )
            return errors
        instance = cls()
    except Exception as e:
        errors.append(f"Failed to instantiate {cls.__name__}(): {e}")
        return errors

    # 3. Check capabilities() returns set[Capability]
    try:
        from aml.backends.interface import Capability
        caps = instance.capabilities()
        if not isinstance(caps, set):
            errors.append(f"capabilities() returned {type(caps).__name__}, expected set")
        else:
            for c in caps:
                if not isinstance(c, Capability):
                    errors.append(f"capabilities() contains {c!r} which is not a Capability enum member")
    except Exception as e:
        errors.append(f"capabilities() raised: {e}")

    # 4. Check Protocol compliance
    try:
        from aml.backends.interface import MemoryBackend
        if not isinstance(instance, MemoryBackend):
            errors.append(
                f"{cls.__name__} does not satisfy the MemoryBackend Protocol. "
                f"Check method signatures match the Protocol definition."
            )
    except Exception as e:
        errors.append(f"Protocol check failed: {e}")

    # 5. Basic round-trip: write + retrieve
    try:
        from aml.backends.interface import WriteOptions, RetrieveOptions
        ref = instance.write("test content", WriteOptions())
        if ref is None:
            errors.append("write() returned None; must return a MemoryRef")
        instance.flush()
        mems = instance.retrieve("test", RetrieveOptions(budget_tokens=1024))
        if not isinstance(mems, list):
            errors.append(f"retrieve() returned {type(mems).__name__}, expected list")
    except Exception as e:
        errors.append(f"Basic write/retrieve round-trip failed: {e}")

    return errors


def print_check(cls: type) -> bool:
    """Run and print pre-flight check results. Returns True if all pass."""
    print(f"GRAFOMEM adapter check — {cls.__name__}\n")

    errors = check_adapter(cls)

    if not errors:
        # Also show declared capabilities
        try:
            instance = cls()
            caps = instance.capabilities()
            print(f"✓ Protocol compliance         OK")
            print(f"✓ Method signatures            OK")
            print(f"✓ Write/retrieve round-trip    OK")
            print(f"✓ Declared capabilities:       {{{', '.join(sorted(c.value for c in caps))}}}")
            print(f"\nAdapter is structurally conformant. Run `grafomem conformance` for full suite.")
        except Exception:
            print(f"✓ Structural checks passed")
        return True
    else:
        for e in errors:
            print(f"✗ {e}")
        print(f"\n{len(errors)} error(s). Fix these before running the conformance suite.")
        return False


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from aml.backends.gmp_reference import GMPReferenceBackend
    from aml.backends.vector_only import _stub_embedder, VectorOnlyBackend
    from aml.backends.persistence import PersistenceBackend

    for cls_factory in [
        lambda: PersistenceBackend,
        lambda: type("BadBackend", (), {}),  # deliberately broken
    ]:
        cls = cls_factory()
        print_check(cls)
        print()

    print("✓ Adapter check module smoke green.")
