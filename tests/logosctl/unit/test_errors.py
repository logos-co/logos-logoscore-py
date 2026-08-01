"""Exit-code → exception mapping for the `logosctl` client.

DELIBERATE DUPLICATE of tests/unit/test_errors.py — do not refactor the two
into one parametrized suite. The `logoscore` and `logosctl` packages ship
side by side until logoscore is retired, and retiring it should be a delete
of src/logoscore/ + tests/unit/, not an unpick.

This file is the one place in the port where the duplication buys nothing
new: the two binaries compile the same `src/client/commands/*.cpp`, so the
exit codes and the JSON error envelope are byte-identical. That is exactly
what makes it worth keeping — if a future logosctl-only change moves a code,
this suite fails while the logoscore one still passes, and the divergence is
visible instead of averaged away.
"""
from logosctl.errors import (
    DaemonNotRunningError,
    LogosctlError,
    MethodError,
    ModuleError,
    from_exit_code,
)


def test_exit_code_mapping():
    assert isinstance(from_exit_code(2, "x"), DaemonNotRunningError)
    assert isinstance(from_exit_code(3, "x"), ModuleError)
    assert isinstance(from_exit_code(4, "x"), MethodError)
    # Unknown codes fall back to the base class
    assert isinstance(from_exit_code(1, "x"), LogosctlError)
    assert isinstance(from_exit_code(99, "x"), LogosctlError)


def test_error_attributes():
    exc = from_exit_code(3, "boom", stderr="std", error_code="MODULE_NOT_FOUND")
    assert exc.exit_code == 3
    assert exc.stderr == "std"
    assert exc.code == "MODULE_NOT_FOUND"
    assert "boom" in str(exc)


def test_error_hierarchy():
    assert issubclass(DaemonNotRunningError, LogosctlError)
    assert issubclass(ModuleError, LogosctlError)
    assert issubclass(MethodError, LogosctlError)
