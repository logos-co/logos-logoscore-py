from logoscore.errors import (
    DaemonNotRunningError,
    LogoscoreError,
    MethodError,
    ModuleError,
    from_exit_code,
)


def test_exit_code_mapping():
    assert isinstance(from_exit_code(2, "x"), DaemonNotRunningError)
    assert isinstance(from_exit_code(3, "x"), ModuleError)
    assert isinstance(from_exit_code(4, "x"), MethodError)
    # Unknown codes fall back to the base class
    assert isinstance(from_exit_code(1, "x"), LogoscoreError)
    assert isinstance(from_exit_code(99, "x"), LogoscoreError)


def test_error_attributes():
    exc = from_exit_code(3, "boom", stderr="std", error_code="MODULE_NOT_FOUND")
    assert exc.exit_code == 3
    assert exc.stderr == "std"
    assert exc.code == "MODULE_NOT_FOUND"
    assert "boom" in str(exc)


def test_error_hierarchy():
    assert issubclass(DaemonNotRunningError, LogoscoreError)
    assert issubclass(ModuleError, LogoscoreError)
    assert issubclass(MethodError, LogoscoreError)
