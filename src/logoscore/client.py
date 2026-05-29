"""Client for a running logoscore daemon.

Each method spawns a fresh `logoscore <subcommand> --json` subprocess and
parses its output. When obtained via `LogoscoreDaemon.client()`, the client
is bound to a specific `config_dir` so it talks to that daemon's connection
file, not the user's global `~/.logoscore/`.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import _proc
from .errors import MethodError
from .events import Subscription


@dataclass(frozen=True)
class DaemonEndpoint:
    """One well-known module's dial spec for a logoscore daemon.

    Serialized into a single entry of the `daemon` block of
    `<config_dir>/client/config.json` (schema version 2). A daemon serves
    each well-known module (`core_service`, `capability_module`) on its
    own listener, so a full connection needs one `DaemonEndpoint` per
    module — which is exactly what the single-endpoint `LOGOSCORE_CLIENT_*`
    env overrides cannot express.

    `verify_peer` is only emitted for `tcp_ssl` transports; leave it None
    to omit it (the typical case for plain `tcp`).
    """

    transport: str               # "tcp" | "tcp_ssl" | "local"
    host: str | None = None
    port: int | None = None
    codec: str = "json"
    verify_peer: bool | None = None

    def _to_config_block(self) -> dict:
        # Built explicitly (not dataclasses.asdict) so key order and the
        # tcp_ssl-only `verify_peer` match what the daemon helpers wrote
        # by hand before this was centralized — see write_config.
        block: dict = {"transport": self.transport}
        if self.host is not None:
            block["host"] = self.host
        if self.port is not None:
            block["port"] = self.port
        block["codec"] = self.codec
        if self.transport == "tcp_ssl" and self.verify_peer is not None:
            block["verify_peer"] = self.verify_peer
        return block


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

    # ── Construction helpers ──────────────────────────────────────────────────

    @staticmethod
    def write_config(
        config_dir: str | Path,
        endpoints: Mapping[str, DaemonEndpoint],
        *,
        token: str | None = None,
        instance_id: str | None = None,
        merge: bool = False,
    ) -> None:
        """Write a `<config_dir>/client/config.json` dial spec (schema
        version 2) with one entry per well-known module, so the CLI can
        reach a daemon whose modules live on distinct listeners.

        This is the single source of truth for the on-disk client config —
        `LogoscoreDaemon`, `LogoscoreDockerDaemon`, and standalone callers
        (see `connect`) all funnel through here.

        `token`, when given, is the RAW token string; it's wrapped as
        `{"token": token}` and written to the file named by `token_file`
        (default `auto.json`) under `<config_dir>/client/`, so the token
        always lands where config.json points. Omit it when the token
        file already exists (e.g. a daemon emitted it).

        `instance_id`, when not None (including ""), is recorded in
        config.json. `merge=True` preserves any pre-existing keys in
        config.json instead of rebuilding it from scratch — used by the
        local daemon, which patches the daemon's auto-emitted file.
        """
        client_dir = Path(config_dir) / "client"
        client_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = client_dir / "config.json"

        cfg: dict = {}
        if merge and cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text())
            except (json.JSONDecodeError, OSError):
                cfg = {}
        cfg["version"] = 2
        cfg.setdefault("token_file", "auto.json")
        if token is not None:
            # The token must land where config.json points. A merged
            # config may carry a custom `token_file`; honor it, but only
            # if it's a plain filename directly under client/ (no abs
            # path, no traversal) — otherwise fall back to auto.json.
            token_file = cfg["token_file"]
            if Path(token_file).name != token_file or Path(token_file).is_absolute():
                token_file = "auto.json"
                cfg["token_file"] = token_file
        if instance_id is not None:
            cfg["instance_id"] = instance_id
        cfg["daemon"] = {
            name: ep._to_config_block() for name, ep in endpoints.items()
        }
        cfg_path.write_text(json.dumps(cfg, indent=4) + "\n")

        if token is not None:
            (client_dir / cfg["token_file"]).write_text(
                json.dumps({"token": token}, indent=4) + "\n")

    @classmethod
    def connect(
        cls,
        endpoints: Mapping[str, DaemonEndpoint],
        *,
        token: str | None = None,
        binary: str = "logoscore",
        config_dir: str | Path | None = None,
        timeout: float | None = 30.0,
        instance_id: str | None = None,
    ) -> "LogoscoreClient":
        """Build a client that dials a (possibly remote) daemon described
        by per-module `endpoints`.

        Materializes a `client/config.json` (via `write_config`) and
        returns a client bound to that config dir with NO env overrides —
        the on-disk spec is authoritative. This is the only way to reach a
        daemon whose `core_service` and `capability_module` listen on
        different ports, which the constructor's single-endpoint
        `transport=`/`tcp_*=` kwargs can't represent.

        `token` is the raw token string the daemon issued for this client
        (see `issue_token`). When `config_dir` is None a private temp dir
        is created and removed when the returned client is garbage
        collected; pass a `config_dir` to keep the config around (it is
        never deleted).
        """
        owns_dir = config_dir is None
        cfg_dir = (
            Path(tempfile.mkdtemp(prefix="logoscore-client-"))
            if owns_dir
            else Path(config_dir)
        )
        cls.write_config(
            cfg_dir, endpoints, token=token, instance_id=instance_id)
        client = cls(binary=binary, config_dir=cfg_dir, timeout=timeout)
        if owns_dir:
            # Clean up the temp dir when the client is collected. Stored on
            # the instance so the finalizer isn't itself collected early;
            # never registered for a caller-supplied dir.
            client._config_dir_finalizer = weakref.finalize(
                client, shutil.rmtree, str(cfg_dir), True)
        return client

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
