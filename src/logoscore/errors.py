"""Exceptions mapped from `logoscore` CLI exit codes.

Exit code contract (from logos-logoscore-cli README):
    0 — success
    1 — general error
    2 — no daemon running
    3 — module error (not found, load/unload failed)
    4 — method error (not found, call failed, timeout)
"""
from __future__ import annotations


class LogoscoreError(Exception):
    """Base class for all logoscore CLI failures."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        stderr: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr
        self.code = code


class DaemonNotRunningError(LogoscoreError):
    """No daemon is reachable (exit code 2)."""


class ModuleError(LogoscoreError):
    """Module operation failed: not found, load/unload failed (exit code 3)."""


class MethodError(LogoscoreError):
    """Method call failed: not found, timeout, bad arguments (exit code 4)."""


_EXIT_CODE_TO_EXC: dict[int, type[LogoscoreError]] = {
    2: DaemonNotRunningError,
    3: ModuleError,
    4: MethodError,
}


def from_exit_code(
    code: int,
    message: str,
    *,
    stderr: str | None = None,
    error_code: str | None = None,
) -> LogoscoreError:
    cls = _EXIT_CODE_TO_EXC.get(code, LogoscoreError)
    return cls(message, exit_code=code, stderr=stderr, code=error_code)
