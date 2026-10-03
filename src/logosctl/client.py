"""Client for a running logosctl daemon.

Each method spawns a fresh `logosctl <subcommand> --json` subprocess and
parses its output. When obtained via `LogosctlDaemon.client()`, the client
is bound to a specific `config_dir` so it talks to that session's dial
spec, not the user's global `~/.logosctl/`. With `remote=PEER` every
command runs on a daemon this client is paired with instead (Remote
Runtime Control, `logosctl --remote PEER`; see `logosctl.remote`).

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
    """One well-known module's entry in the `daemon` block of
    `<config_dir>/client/config.yaml` (schema version 2).

    A client dials a daemon on this machine over its local socket; a daemon
    elsewhere is operated with Remote Runtime Control
    (`LogosctlClient(remote=…)`), which needs no dial spec at all.
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
    passed as-is for the CLI's type coercion.

    Byte-identical to the logoscore encoding on purpose: both binaries
    compile the same `src/client/commands/call_command.cpp`, so the
    argument grammar is one contract, not two. That includes a top-level
    `None`, the empty inhabitant of an optional slot: `json:null`, never
    the four-character string "None" (see logoscore's `_arg_to_str`).
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


class LogosctlClient:
    """Thin client around `logosctl` subcommands against a running daemon."""

    def __init__(
        self,
        binary: str = "logosctl",
        *,
        config_dir: Path | None = None,
        token: str | None = None,
        timeout: float | None = 30.0,
        remote: str | None = None,
    ) -> None:
        self.binary = binary
        self.config_dir = Path(config_dir) if config_dir is not None else None
        self.token = token
        self.timeout = timeout
        # A paired daemon's alias or runtime ID: every command then runs
        # there, over the pairing this config dir holds, and needs no token.
        self.remote = remote

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
        version 2) with one entry per well-known module.

        This is the single source of truth for the on-disk client config —
        `LogosctlDaemon.remote_client` and standalone callers (see
        `connect`) funnel through here.

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
        config dir — the registry name is `local:logos_<module>_<id>`.
        `merge=True` preserves any
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
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
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
            # `setdefault` above only fills the key in when it is ABSENT, so a
            # merged config can still carry a non-string here — a hand-edited
            # file, a corrupt one, an older format. `Path(42)` raises
            # TypeError, which would abort the write over a value we were
            # always going to reject anyway. Treat "not a plain string" as one
            # more thing we cannot honor, same as a path or a traversal.
            token_file = cfg["token_file"]
            if (not isinstance(token_file, str)
                    or Path(token_file).name != token_file
                    or Path(token_file).is_absolute()):
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
        binary: str = "logosctl",
        config_dir: str | Path | None = None,
        timeout: float | None = 30.0,
        instance_id: str | None = None,
    ) -> "LogosctlClient":
        """Build a client that dials a daemon on this machine from a config
        dir the daemon does not own, described by per-module `endpoints`.

        Materializes a `client/config.yaml` (via `write_config`) and
        returns a client bound to that config dir. The on-disk spec is the
        whole story — logosctl has no client-side flags or env vars to
        override it with. A daemon on another machine is operated with
        Remote Runtime Control instead (`logosctl.remote`).

        Point this at a config dir the daemon does NOT own. A daemon
        rewrites `client/config.yaml` in its own session on every boot
        when the file is missing or carries a stale instance id, which
        would silently replace a spec written into its dir.

        `token` is the raw token string the daemon issued for this client
        (its `client/auto.json`, or a named one from `issue_token`), and
        `instance_id` the daemon's (its local socket is named after it).
        When `config_dir` is None a private temp dir is created and removed
        when the returned client is garbage collected; pass a `config_dir`
        to keep the config around (it is never deleted).
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
    # --no-json/--human, -q/--quiet and --remote are lifted back out of a
    # subcommand's leftovers, so a trailing `--config-dir DIR` would reach
    # the command as two positional arguments. The env var has no position
    # to get wrong.

    # ── Daemon-wide commands ────────────────────────────────────────────────

    def status(self) -> dict:
        return self._run(["status"])

    def stats(self) -> Any:
        return self._run(["module", "stats"])

    def stop(self) -> None:
        """Ask the daemon to shut down cleanly.

        This is an RPC like any other, so it needs a working dial spec in
        this client's config dir — it is not a signal. A caller holding
        the daemon process (`LogosctlDaemon`) keeps a SIGTERM/SIGKILL
        ladder behind it for the case where the spec is unusable.
        """
        self._run(["daemon", "stop"])

    # ── Module management ───────────────────────────────────────────────────

    def list_modules(self, *, loaded: bool = False) -> list[dict]:
        args: list[str] = ["module", "ls"]
        if loaded:
            args.append("--loaded")
        result = self._run(args)
        return result if isinstance(result, list) else []

    def module_info(self, name: str) -> dict:
        return self._run(["module", "show", name])

    def load_module(self, name: str) -> dict:
        return self._run(["module", "load", name])

    def unload_module(self, name: str) -> dict:
        return self._run(["module", "unload", name])

    def reload_module(self, name: str) -> dict:
        return self._run(["module", "reload", name])

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
        `--no-json`, `--human`, `-q`, `--quiet` or `--remote` is lifted out
        of the subcommand's leftovers as a global flag before the call
        command sees it. Pass such a value as `"str:--json"`.
        """
        envelope = self._run(
            ["call", module, method, *(_arg_to_str(a) for a in args)], timeout)
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

    # ── Peering ─────────────────────────────────────────────────────────────

    def peer(self, verb: str, *args: str, timeout: float | None = None) -> Any:
        """Run `logosctl peer <verb> [args…]` and return peering_module's reply.

        `peer status`, `ls`, `routes`, `import NAME --from PEER …`, `policy set
        FILE` and the rest; see `logosctl peer` for the verbs.
        """
        return self._run(["peer", verb, *args], timeout)

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
        watch_args: list[str] = [*self._remote_args(), "watch", module]
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

    def _remote_args(self) -> list[str]:
        # App-level, so ahead of the subcommand: a trailing one is lifted out
        # of the leftovers too, which would eat a call argument `--remote`.
        return ["--remote", self.remote] if self.remote else []

    def _run(self, args: Sequence[str], timeout: float | None = None) -> Any:
        return _proc.run_json(
            self.binary, [*self._remote_args(), *args],
            config_dir=self.config_dir, token=self.token,
            timeout=timeout if timeout is not None else self.timeout,
        )

    def _raw_args(self) -> Sequence[str]:
        """For debugging: common arg prefix for spawned subprocesses."""
        return [self.binary, *self._remote_args()]
