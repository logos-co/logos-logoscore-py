"""Exceptions mapped from `logoscore` CLI exit codes.

Exit code contract (from logos-logoscore-cli README):
    0 — success
    1 — general error
    2 — no daemon running
    3 — module error (not found, load/unload failed)
    4 — method error (not found, call failed, timeout)


`code` vs `detail_code`. The envelope carries TWO codes on a failed call and
they answer different questions:

    {"status":"error","code":"METHOD_FAILED",
     "error":{"code":"object_unavailable","message":...,"origin":...}}

`code` is the ENVELOPE verdict — one token for every way a call can fail, so
`METHOD_FAILED` is what a transport failure, a provider rejection and a bad
argument count all look like. `detail_code` is the FAILURE CLASS underneath it:
`object_unavailable` / `timeout` / `transport_error` / `call_failed` /
`unauthorized` from the transport, or `dispatch_failed` / `invalid_args` /
`unknown_method` folded in from a provider that ran and refused
(logos-logoscore-cli src/core_service/call_envelope.{h,cpp}).

Dropping it — which this client did — makes those classes indistinguishable to
every caller, and "the provider refused the argument VALUES" and "the module is
not there" are not the same event. `detail_code` is None when the envelope
carries no `error` object (METHOD_NOT_FOUND, RPC_FAILED, and every pre-#99
daemon), so a caller reads `detail_code or code`.
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
        detail_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr
        self.code = code
        self.detail_code = detail_code


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
    detail_error_code: str | None = None,
) -> LogoscoreError:
    cls = _EXIT_CODE_TO_EXC.get(code, LogoscoreError)
    return cls(message, exit_code=code, stderr=stderr, code=error_code,
               detail_code=detail_error_code)
