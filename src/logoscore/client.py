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
    """One well-known module's entry in the `daemon` block of
    `<config_dir>/client/config.json` (schema version 2): the local socket,
    the one transport a client dials.
    """

    transport: str = "local"

    def _to_config_block(self) -> dict:
        # The shape the daemon writes into its own session on boot.
        return {"transport": self.transport}


def _json_default(obj: Any) -> Any:
    """`json.dumps` fallback for values that aren't natively serialisable.

    `bytes`/`bytearray` (top-level or nested in a container arg) are
    wrapped in the protocol's canonical tagged form so they survive the
    round-trip losslessly (symmetric with `_proc.decode_bytes_tags` on
    the way back).
    """
    if isinstance(obj, (bytes, bytearray)):
        return _proc.encode_bytes_tag(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serialisable")


def _arg_to_str(arg: Any) -> str:
    """Convert a Python arg to the string form the CLI expects.

    `pathlib.Path` values are read via `@file` so the CLI loads the file
    content. `bytes`/`bytearray`, `list`/`tuple`/`dict` are JSON-encoded
    behind the CLI's `json:` prefix so the daemon reconstructs them
    losslessly: byte arrays go through the canonical `{"_bytes": …}` tag
    (NUL- and high-byte-safe — a raw latin-1 string would UTF-8-mangle any
    byte ≥ 0x80 crossing the argv boundary), and container params
    (`[tstr]`, `[int]`, `[any]`, `{tstr:any}`) plus non-scalar `any`
    values pass as natural Python objects. Strings/numbers/bools are
    passed as-is for the CLI's type coercion (see
    logos-logoscore-cli/src/client/commands/call_command.cpp).

    `None` is the empty inhabitant of an optional slot, and a POSITIONAL
    slot has no key to omit — the contract spells empty there as null, and
    the arity must not change — so it takes the same `json:` route rather
    than falling through to `str(None)`. Without this it crossed argv as
    the literal string "None", which a `?tstr` provider echoed back as a
    present four-character string. Nulls NESTED in a container were always
    fine (they ride the branch below intact); only the top-level one was
    lost, which is why `Optional/record/explicit-null` was expressible and
    `Optional/scalar/empty-is-null` was not.
    """
    if arg is None:
        return "json:null"
    if isinstance(arg, Path):
        return f"@{arg}"
    if isinstance(arg, bool):
        return "true" if arg else "false"
    if isinstance(arg, (bytes, bytearray, list, tuple, dict)):
        return "json:" + json.dumps(arg, default=_json_default)
    return str(arg)


# Decoding tagged-bytes is shared with the event path; keep one impl in
# `_proc` (importable by both `client` and `events` without a cycle).
_decode_bytes_tags = _proc.decode_bytes_tags


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
        version 2) with one entry per well-known module — the single source
        of truth for the on-disk client config (see `connect`).

        `token`, when given, is the RAW token string; it's wrapped as
        `{"token": token}` and written to the file named by `token_file`
        (default `auto.json`) under `<config_dir>/client/`, so the token
        always lands where config.json points. Omit it when the token
        file already exists (e.g. a daemon emitted it).

        `instance_id`, when not None (including ""), is recorded in
        config.json. `merge=True` preserves any pre-existing keys in
        config.json instead of rebuilding it from scratch.
        """
        client_dir = Path(config_dir) / "client"
        client_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = client_dir / "config.json"

        cfg: dict = {}
        if merge and cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
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
        cfg_path.write_text(json.dumps(cfg, indent=4) + "\n", encoding="utf-8")

        if token is not None:
            (client_dir / cfg["token_file"]).write_text(
                json.dumps({"token": token}, indent=4) + "\n", encoding="utf-8")

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
        """Build a client that dials a daemon on this machine from a config
        dir the daemon does not own, described by per-module `endpoints`.

        Materializes a `client/config.json` (via `write_config`) and
        returns a client bound to that config dir. `token` is the raw token
        string the daemon issued for this client (see `issue_token`), and
        `instance_id` the daemon's (its local socket is named after it).
        When `config_dir` is None a private temp dir is created and removed
        when the returned client is garbage collected; pass a `config_dir`
        to keep the config around (it is never deleted).
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
        decode_bytes: bool = True,
    ) -> Any:
        """Call a `Q_INVOKABLE` method on a loaded module.

        Returns the method's result value (the `result` field of the JSON
        envelope). Raises `MethodError` on status == "error".

        `decode_bytes=False` returns the envelope's result verbatim, with any
        canonical `{"_bytes": "..."}` object left as an object rather than
        materialized into `bytes`.

        That distinction is not cosmetic. The tagged form is structurally
        indistinguishable from a one-key user map, and decoding here — inside the
        client, after the value has crossed every boundary intact — makes the
        collision look like a platform behaviour when it is this function's. A
        test that wants to observe what the SYSTEM did with such a value has to
        turn the decode off, or it is measuring the client.
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
            # Both codes, for the reason `_proc._error_codes_from_stdout`
            # spells out: `code` is the envelope verdict (METHOD_FAILED for
            # every failure) and `error.code` is the failure CLASS under it.
            err = envelope.get("error")
            raise MethodError(
                envelope.get("message", "method call failed"),
                code=envelope.get("code"),
                detail_code=(err.get("code") if isinstance(err, dict) else None),
            )
        if isinstance(envelope, dict) and "result" in envelope:
            result = envelope["result"]
            return _decode_bytes_tags(result) if decode_bytes else result
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
