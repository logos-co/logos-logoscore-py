"""Client for a running logoscore daemon.

Each method spawns a fresh `logoscore <subcommand> --json` subprocess and
parses its output. When obtained via `LogoscoreDaemon.client()`, the client
is bound to a specific `config_dir` so it talks to that daemon's connection
file, not the user's global `~/.logoscore/`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

from . import _proc
from .errors import MethodError
from .events import Subscription


def _arg_to_str(arg: Any) -> str:
    """Convert a Python arg to the string form the CLI expects.

    `pathlib.Path` values are read via `@file` so the CLI loads the file
    content. Strings/numbers/bools are passed as-is for the CLI's own
    type coercion (see logos-logoscore-cli/src/client/commands/call_command.cpp).
    """
    if isinstance(arg, Path):
        return f"@{arg}"
    if isinstance(arg, bool):
        return "true" if arg else "false"
    return str(arg)


class LogoscoreClient:
    """Thin client around `logoscore` subcommands against a running daemon."""

    def __init__(
        self,
        binary: str = "logoscore",
        *,
        config_dir: Path | None = None,
        token: str | None = None,
        timeout: float | None = 30.0,
    ) -> None:
        self.binary = binary
        self.config_dir = Path(config_dir) if config_dir is not None else None
        self.token = token
        self.timeout = timeout

    # ── Daemon-wide commands ────────────────────────────────────────────────

    def status(self) -> dict:
        return _proc.run_json(
            self.binary, ["status"],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    def stats(self) -> Any:
        return _proc.run_json(
            self.binary, ["stats"],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    def stop(self) -> None:
        """Ask the daemon to shut down cleanly."""
        _proc.run_json(
            self.binary, ["stop"],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    # ── Module management ───────────────────────────────────────────────────

    def list_modules(self, *, loaded: bool = False) -> list[dict]:
        args: list[str] = ["list-modules"]
        if loaded:
            args.append("--loaded")
        result = _proc.run_json(
            self.binary, args,
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )
        return result if isinstance(result, list) else []

    def module_info(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["module-info", name],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    def load_module(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["load-module", name],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    def unload_module(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["unload-module", name],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    def reload_module(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["reload-module", name],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    # ── Method calls ────────────────────────────────────────────────────────

    def call(
        self,
        module: str,
        method: str,
        *args: Any,
        timeout: float | None = None,
    ) -> Any:
        """Call a `Q_INVOKABLE` method on a loaded module.

        Returns the method's result value (the `result` field of the JSON
        envelope). Raises `MethodError` on status == "error".
        """
        cli_args = ["call", module, method, *(_arg_to_str(a) for a in args)]
        envelope = _proc.run_json(
            self.binary, cli_args,
            config_dir=self.config_dir, token=self.token,
            timeout=timeout if timeout is not None else self.timeout,
        )
        # On success, the CLI prints {"status":"success", "result": ...} — but
        # non-success paths are already raised by run_json (exit code 3 or 4).
        if isinstance(envelope, dict) and envelope.get("status") == "error":
            raise MethodError(
                envelope.get("message", "method call failed"),
                code=envelope.get("code"),
            )
        if isinstance(envelope, dict) and "result" in envelope:
            return envelope["result"]
        return envelope

    # ── Event subscription ──────────────────────────────────────────────────

    def on_event(
        self,
        module: str,
        event: str | None,
        callback: Callable[[dict], None],
        *,
        error_callback: Callable[[BaseException], None] | None = None,
    ) -> Subscription:
        """Subscribe to events from a module. Returns a cancellable subscription.

        `callback` is invoked on a background thread for each event dict.
        If `event` is None, all events from the module are received.
        """
        watch_args: list[str] = ["watch", module]
        if event is not None:
            watch_args.extend(["--event", event])
        return Subscription.start(
            binary=self.binary,
            args=watch_args,
            config_dir=self.config_dir,
            token=self.token,
            callback=callback,
            error_callback=error_callback,
        )

    # ── Internal ────────────────────────────────────────────────────────────

    def _raw_args(self) -> Sequence[str]:
        """For debugging: common arg prefix for spawned subprocesses."""
        return [self.binary]
