"""Client for a running logosctl daemon.

Each method spawns a fresh `logosctl <subcommand> --json` subprocess and
parses its output. When obtained via `LogosctlDaemon.client()`, the client
is bound to a specific `config_dir` so it talks to that session's dial
spec, not the user's global `~/.logosctl/`.

Every command uses the grouped subcommand surface (`module ls`,
`module show`, `daemon stop`, …). The old hyphenated spellings are still
registered as hidden dispatch tokens, but the groups are what `--help`
documents, so that is what we type.
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
    """One well-known module's dial spec for a logosctl daemon.

    Serialized into a single entry of the `daemon` block of
    `<config_dir>/client/config.yaml` (schema version 2). A daemon serves
    each well-known module (`core_service`, `capability_module`) on its
    own listener, so a full connection needs one `DaemonEndpoint` per
    module.

    This is the CLIENT half of the wire description, and its key names
    are deliberately NOT the daemon's: a listener in the daemon document
    says `protocol:` / `cert:` / `key:` / `ca_file:`, while an entry here
    says `transport:` / `ca:`. Spelling one in the other's document fails
    the parse — the client side has no `cert`/`key` at all.

    `verify_peer` and `ca` are only emitted for `tcp_ssl` transports;
    leave them None to omit them (the typical case for plain `tcp`).
    Dropping `ca` while `verify_peer` stays true fails the handshake
    closed, so a dev setup wants `verify_peer=False` rather than a
    missing CA.
    """

    transport: str               # "tcp" | "tcp_ssl" | "local"
    host: str | None = None
    port: int | None = None
    codec: str = "json"
    verify_peer: bool | None = None
    ca: str | None = None        # path to the CA bundle, tcp_ssl only

    def _to_config_block(self) -> dict:
        # Built explicitly (not dataclasses.asdict) so key order and the
        # tcp_ssl-only `ca`/`verify_peer` match what the daemon writes
        # into its own session on boot — see write_config.
        block: dict = {"transport": self.transport}
        if self.host is not None:
            block["host"] = self.host
        if self.port is not None:
            block["port"] = self.port
        block["codec"] = self.codec
        if self.transport == "tcp_ssl":
            if self.ca is not None:
                block["ca"] = self.ca
            if self.verify_peer is not None:
                block["verify_peer"] = self.verify_peer
        return block


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
    passed as-is for the CLI's type coercion.

    Byte-identical to the logoscore encoding on purpose: both binaries
    compile the same `src/client/commands/call_command.cpp`, so the
    argument grammar is one contract, not two.
    """
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


class LogosctlClient:
    """Thin client around `logosctl` subcommands against a running daemon."""

    def __init__(
        self,
        binary: str = "logosctl",
        *,
        config_dir: Path | None = None,
        token: str | None = None,
        timeout: float | None = 30.0,
    ) -> None:
        self.binary = binary
        self.config_dir = Path(config_dir) if config_dir is not None else None
        self.token = token
        self.timeout = timeout

    # logoscore also took `transport` / `tcp_host` / `tcp_port` /
    # `no_verify_peer` / `codec` here, and turned them into
    # LOGOSCORE_CLIENT_* env vars the CLI merged over the on-disk spec per
    # call. logosctl honours exactly two variables — LOGOSCTL_CONFIG_DIR
    # and LOGOSCTL_TOKEN — and `RpcClient::connect()` reads
    # client/config.yaml verbatim with no merge layer at all. There is
    # therefore nothing a per-call kwarg could set, so the kwargs are
    # gone rather than silently ignored. Retargeting a client means
    # writing a different dial spec: `connect()` below, or an in-place
    # edit of the file (what `LogosctlDaemon` does). The per-module `port`
    # that `tcp_port` used to override — a docker `-p 8080:6000` mapping,
    # an SSH tunnel on another port — is now just `DaemonEndpoint(port=…)`,
    # which is strictly better: it can differ per module, and the single
    # uniform env port never could.

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
        """Write a `<config_dir>/client/config.yaml` dial spec (schema
        version 2) with one entry per well-known module, so the CLI can
        reach a daemon whose modules live on distinct listeners.

        This is the single source of truth for the on-disk client config —
        `LogosctlDaemon`, `LogosctlDockerDaemon`, and standalone callers
        (see `connect`) all funnel through here.

        The document is emitted as JSON text into a `.yaml` file. YAML is
        a superset of JSON, so the CLI's yaml-cpp parser reads it back
        exactly as written — and unlike hand-rolled block YAML it can't
        lose a quoted-looking scalar to YAML 1.1 typing (`1.10` → 1.1,
        `no` → False). It also keeps `merge=True` a plain load/patch/store.
        We deliberately do NOT shell out to `logosctl client config set`:
        that replaces the document wholesale (so it can't merge) and
        performs no schema re-validation anyway, so the only thing it
        would buy is the top-level key allowlist — and the four keys below
        are all this function ever writes.

        `token`, when given, is the RAW token string; it's wrapped as
        `{"token": token}` and written to the file named by `token_file`
        (default `auto.json`) under `<config_dir>/client/`, so the token
        always lands where config.yaml points. Omit it when the token
        file already exists (e.g. a daemon emitted it).

        `instance_id`, when not None (including ""), is recorded in
        config.yaml. It is mandatory for a `local` dial from a foreign
        config dir — the registry name is `local:logos_<module>_<id>` —
        and meaningless over tcp/tcp_ssl. `merge=True` preserves any
        pre-existing keys in config.yaml instead of rebuilding it from
        scratch — used by the local daemon, which patches the daemon's
        auto-emitted file.
        """
        client_dir = Path(config_dir) / "client"
        client_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = client_dir / "config.yaml"

        cfg: dict = {}
        if merge and cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text())
            except (json.JSONDecodeError, OSError):
                # A daemon-written config.yaml is canonical block YAML,
                # not JSON, so this is the normal path when merging onto
                # one the daemon emitted: start clean rather than fail.
                cfg = {}
        cfg["version"] = 2
        # `token_file` must be present and non-empty even when the caller
        # passes the token through $LOGOSCTL_TOKEN instead: the CLI's
        # fileOk check is `!daemon.empty() && !token_file.empty()`, so an
        # omitted one makes the whole spec read as "no client config".
        cfg.setdefault("token_file", "auto.json")
        if token is not None:
            # The token must land where config.yaml points. A merged
            # config may carry a custom `token_file`; honor it, but only
            # if it's a plain filename directly under client/ (no abs
            # path, no traversal) — otherwise fall back to auto.json.
            # The CLI applies the same rule and fails closed, so a name
            # we can't honor would leave an unreadable credential.
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
        binary: str = "logosctl",
        config_dir: str | Path | None = None,
        timeout: float | None = 30.0,
        instance_id: str | None = None,
    ) -> "LogosctlClient":
        """Build a client that dials a (possibly remote) daemon described
        by per-module `endpoints`.

        Materializes a `client/config.yaml` (via `write_config`) and
        returns a client bound to that config dir. The on-disk spec is the
        whole story — logosctl has no client-side flags or env vars to
        override it with, so this is the only way to reach a daemon that
        isn't the one owning this config dir.

        Point this at a config dir the daemon does NOT own. A daemon
        rewrites `client/config.yaml` in its own session on every boot
        when the file is missing or carries a stale instance id, which
        would silently replace a spec written into its dir.

        `token` is the raw token string the daemon issued for this client
        (see `issue_token`). Prefer a named token over a copy of the
        daemon's `client/auto.json` for tcp/tcp_ssl: auto.json is issued
        local-only, and the fact that it currently authenticates over the
        network is a runtime quirk, not a promise. When `config_dir` is
        None a private temp dir is created and removed when the returned
        client is garbage collected; pass a `config_dir` to keep the
        config around (it is never deleted).
        """
        owns_dir = config_dir is None
        cfg_dir = (
            Path(tempfile.mkdtemp(prefix="logosctl-client-"))
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

    # Every command below passes the session through LOGOSCTL_CONFIG_DIR
    # (`_proc` sets it) rather than `--config-dir`. The flag is app-level,
    # and client subcommands use allow_extras(): only -j/--json,
    # --no-json/--human and -q/--quiet are lifted back out of a
    # subcommand's leftovers, so a trailing `--config-dir DIR` would reach
    # the command as two positional arguments. The env var has no position
    # to get wrong.

    # ── Daemon-wide commands ────────────────────────────────────────────────

    def status(self) -> dict:
        return _proc.run_json(
            self.binary, ["status"],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    def stats(self) -> Any:
        return _proc.run_json(
            self.binary, ["module", "stats"],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    def stop(self) -> None:
        """Ask the daemon to shut down cleanly.

        This is an RPC like any other, so it needs a working dial spec in
        this client's config dir — it is not a signal. A caller holding
        the daemon process (`LogosctlDaemon`) keeps a SIGTERM/SIGKILL
        ladder behind it for the case where the spec is unusable.
        """
        _proc.run_json(
            self.binary, ["daemon", "stop"],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    # ── Module management ───────────────────────────────────────────────────

    def list_modules(self, *, loaded: bool = False) -> list[dict]:
        args: list[str] = ["module", "ls"]
        if loaded:
            args.append("--loaded")
        result = _proc.run_json(
            self.binary, args,
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )
        return result if isinstance(result, list) else []

    def module_info(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["module", "show", name],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    def load_module(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["module", "load", name],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    def unload_module(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["module", "unload", name],
            config_dir=self.config_dir, token=self.token, timeout=self.timeout,
        )

    def reload_module(self, name: str) -> dict:
        return _proc.run_json(
            self.binary, ["module", "reload", name],
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

        One argument value can't survive the trip: a bare `--json`, `-j`,
        `--no-json`, `--human`, `-q` or `--quiet` is lifted out of the
        subcommand's leftovers as a global flag before the call command
        sees it. Pass such a value as `"str:--json"`.
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
