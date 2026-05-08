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
        transport: str | None = None,
        tcp_host: str | None = None,
        tcp_port: int | None = None,
        no_verify_peer: bool = False,
        codec: str | None = None,
    ) -> None:
        self.binary = binary
        self.config_dir = Path(config_dir) if config_dir is not None else None
        self.token = token
        self.timeout = timeout
        self.transport = transport
        self.tcp_host = tcp_host
        # `tcp_port` overrides the daemon-advertised port. Needed when
        # the reachable port differs from the one the daemon bound —
        # e.g. a docker `-p 8080:6000` maps host 8080 to container
        # 6000, or an SSH tunnel forwards through a different port.
        self.tcp_port = tcp_port
        self.no_verify_peer = no_verify_peer
        # Optional pin of the wire codec. When set, the client insists the
        # picked transport uses this codec — mismatch aborts connect. When
        # unset, the client accepts whatever the daemon advertised.
        self.codec = codec

    def _env_overrides(self) -> dict[str, str] | None:
        """Env vars the CLI reads to pick a client-side transport. Kept out
        of the public API — the user sees kwargs, the CLI sees env vars."""
        out: dict[str, str] = {}
        if self.transport:
            out["LOGOSCORE_CLIENT_TRANSPORT"] = self.transport
        if self.tcp_host:
            out["LOGOSCORE_CLIENT_TCP_HOST"] = self.tcp_host
        if self.tcp_port is not None:
            out["LOGOSCORE_CLIENT_TCP_PORT"] = str(self.tcp_port)
        if self.no_verify_peer:
            out["LOGOSCORE_CLIENT_NO_VERIFY_PEER"] = "1"
        if self.codec:
            out["LOGOSCORE_CLIENT_CODEC"] = self.codec
        return out or None

    # ── Daemon-wide commands ────────────────────────────────────────────────

    def status(self) -> dict:
        return _proc.run_json(
            self.binary, ["status"],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
            env=self._env_overrides(),
        )

    def stats(self) -> Any:
        return _proc.run_json(
            self.binary, ["stats"],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
            env=self._env_overrides(),
        )

    def stop(self) -> None:
        """Ask the daemon to shut down cleanly."""
        _proc.run_json(
            self.binary, ["stop"],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
            env=self._env_overrides(),
        )

    # ── Module management ───────────────────────────────────────────────────

    def list_modules(self, *, loaded: bool = False) -> list[dict]:
        args: list[str] = ["list-modules"]
        if loaded:
            args.append("--loaded")
        result = _proc.run_json(
            self.binary, args,
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
            env=self._env_overrides(),
        )
        return result if isinstance(result, list) else []

    def module_info(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["module-info", name],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
            env=self._env_overrides(),
        )

    def load_module(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["load-module", name],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
            env=self._env_overrides(),
        )

    def unload_module(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["unload-module", name],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
            env=self._env_overrides(),
        )

    def reload_module(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["reload-module", name],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
            env=self._env_overrides(),
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
            env=self._env_overrides(),
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
            extra_env=self._env_overrides(),
        )

    # ── Internal ────────────────────────────────────────────────────────────

    def _raw_args(self) -> Sequence[str]:
        """For debugging: common arg prefix for spawned subprocesses."""
        return [self.binary]
