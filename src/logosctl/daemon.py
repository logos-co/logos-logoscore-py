"""Lifecycle manager for a `logosctl` daemon process.

`LogosctlDaemon` is a context manager: on `__enter__` it installs a daemon
config document into an isolated `--config-dir`, spawns `logosctl daemon
start`, waits for the daemon's state file to appear, and verifies liveness
with `status`. On exit it runs `logosctl stop`, then terminates/kills the
child process as a fallback, and removes any temp state directory it
created.

The isolated config dir means multiple daemons can run concurrently in
the same test process without colliding on `~/.logosctl/daemon/`, and
nothing the wrapper does leaks into the developer's global state.

The one structural difference from the `logoscore` wrapper is that none of
the daemon's setup is expressible on the command line any more: `-m`,
`--persistence-path`, `--module-transport` and `--insecure-tcp` were all
deleted in favour of a YAML document installed with `logosctl daemon
config set FILE`. `daemon start` acts on whatever is already on disk, so
`start()` has two phases — install the config, then boot — and a document
the CLI rejects has to be a hard error: the daemon would otherwise come up
silently missing every modules dir and listener the caller asked for.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, IO

from . import _proc
from .client import DaemonEndpoint, LogosctlClient
from .errors import LogosctlError


# ── YAML emitting ────────────────────────────────────────────────────────
#
# The daemon config is YAML, and this package deliberately has no
# third-party dependencies — it is test support, so anything it imports
# propagates into every consumer's test environment. The documents emitted
# here are small and entirely under our control (nested mappings, one list
# of strings, one list of flat mappings), which is a few lines of
# block-style emitter. That's cheaper than a PyYAML dependency.

# Plain (unquoted) scalars are only safe for words that can't be mistaken
# for something else. yaml-cpp types scalars aggressively — an unquoted
# `1.10` decodes as the number 1.1, `no` may decode as false — so anything
# that has to stay a string and isn't identifier-shaped gets quoted.
# Leading `/` is allowed unquoted because most of what we emit is paths.
_PLAIN_SCALAR = re.compile(r"^[A-Za-z_/][A-Za-z0-9_./+@-]*$")

# YAML 1.1 boolean/null spellings. Bare, any of these decodes as a bool or
# a null rather than the string the caller wrote.
_RESERVED_WORDS = frozenset({
    "y", "n", "yes", "no", "true", "false", "on", "off", "null", "none", "~",
})


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value)
    if _PLAIN_SCALAR.match(text) and text.lower() not in _RESERVED_WORDS:
        return text
    # Double-quoted style, with the escapes YAML defines for it. Newlines
    # matter: left raw they'd fold into a space rather than survive, which
    # is reachable for a pretty-printed `access_policy` JSON string.
    escaped = (text.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{escaped}"'


def _yaml_lines(value: Any, indent: int = 0) -> list[str]:
    """Render `value` as block-style YAML lines, `indent` levels deep."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                if not item:
                    # An empty collection has no block form; flow style is
                    # the only way to say "present but empty".
                    lines.append(
                        f"{pad}{key}: " + ("{}" if isinstance(item, dict) else "[]"))
                else:
                    lines.append(f"{pad}{key}:")
                    lines.extend(_yaml_lines(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)) and item:
                nested = _yaml_lines(item, indent + 1)
                # The sequence marker eats the first nested line's indent;
                # every following line already lines up underneath it.
                lines.append(f"{pad}- {nested[0][len(pad) + 2:]}")
                lines.extend(nested[1:])
            elif isinstance(item, (dict, list)):
                lines.append(
                    f"{pad}- " + ("{}" if isinstance(item, dict) else "[]"))
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
    else:
        lines.append(f"{pad}{_yaml_scalar(value)}")
    return lines


def _yaml_document(doc: dict) -> str:
    return "\n".join(_yaml_lines(doc)) + "\n"


# Types the daemon's loader reads with nlohmann's `json::value(key,
# default)`, which THROWS on a type mismatch — and neither the loader nor
# `daemon config set` catches it, so the wrong type aborts the process
# instead of producing an INVALID_CONFIG. Everything this class builds is
# correct by construction; `extra_config` is the hole, so check it here
# where the diagnostic can still name the key.
_CONFIG_KEY_TYPES: dict[str, tuple[type, ...]] = {
    "modules_dirs": (list,),
    "persistence_path": (str,),
    "access_policy": (str,),
    "access_group": (str,),
    "insecure_tcp": (bool,),
}


def _check_config_types(doc: dict) -> None:
    for key, types in _CONFIG_KEY_TYPES.items():
        if key in doc and not isinstance(doc[key], types):
            names = "/".join(t.__name__ for t in types)
            raise LogosctlError(
                f"daemon config key {key!r} must be {names}, got "
                f"{type(doc[key]).__name__} — the daemon's loader would abort "
                "on it rather than report a config error. Note `access_policy` "
                "is a JSON *string*, not a mapping: json.dumps it first."
            )
    dirs = doc.get("modules_dirs")
    if isinstance(dirs, list) and not all(isinstance(d, str) for d in dirs):
        raise LogosctlError("daemon config key 'modules_dirs' must be a list of str")


def _abs(path: str | Path) -> Path:
    """Absolutise a path without requiring it to exist.

    Relative paths in the daemon config do NOT mean what a relative CLI
    flag meant: a `modules_dirs` entry resolves against the daemon
    process's cwd and `persistence_path` against the config dir, neither of
    which is the caller's cwd. Absolutising here preserves the old
    meaning of `-m ./modules`."""
    return Path(path).expanduser().absolute()


def _is_loopback(host: str) -> bool:
    # Same set the daemon's plaintext-tcp guard treats as loopback.
    return host in ("127.0.0.1", "::1", "localhost")


def _binds_public_plaintext_tcp(doc: dict) -> bool:
    """True when `doc` binds plaintext tcp on a host the daemon won't
    accept without `insecure_tcp`.

    Runs over a whole config document rather than the wrapper's own
    listeners, because the daemon's guard runs over its whole merged
    config too — a listener that arrived via `extra_config` gets exactly
    the same scrutiny there. An absent `host` counts as public, matching
    the daemon: its `isLoopback("")` is false, so a hostless tcp listener
    trips the guard.

    Defensive about shape: `modules` may be anything a caller put in
    `extra_config`, and a malformed document should be rejected by the
    CLI with its own message, not by a TypeError in here."""
    modules = doc.get("modules")
    if not isinstance(modules, dict):
        return False
    for entries in modules.values():
        if not isinstance(entries, list):
            continue
        for listener in entries:
            if not isinstance(listener, dict):
                continue
            if listener.get("protocol") != "tcp":
                continue
            if not _is_loopback(str(listener.get("host", ""))):
                return True
    return False


class LogosctlDaemon:
    """Context manager that spawns and tears down a logosctl daemon."""

    def __init__(
        self,
        modules_dir: str | Path | list[str | Path],
        *,
        binary: str = "logosctl",
        config_dir: str | Path | None = None,
        persistence_path: str | Path | None = None,
        extra_args: list[str] | None = None,
        # Extra top-level keys merged into the daemon config document,
        # applied last so they win. This is the replacement for the half
        # of `extra_args` that used to carry daemon *settings* — with the
        # flags gone, an escape hatch for `access_policy`, `access_group`,
        # `dirs`, `logging`, … has to be config-shaped. Keys are
        # allowlisted by the CLI; an unknown one fails `config set` by
        # name rather than being silently dropped.
        extra_config: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        # Higher than logoscore's 15s: boot now unconditionally loads
        # package_manager and package_downloader and creates the session's
        # modules/plugins/keyring/cache dirs before state.json appears.
        startup_timeout: float = 30.0,
        # Per-module transport list applied to BOTH `core_service` and
        # `capability_module` — i.e. every protocol named here becomes one
        # listener entry under `modules: { <module>: [ … ] }` in the daemon
        # config document. (logoscore spelled the same thing as repeated
        # `--module-transport <module>=<protocol>[,k=v...]` flags; that
        # flag no longer exists.)
        #
        # The daemon ALWAYS adds an implicit LocalSocket listener for
        # each module regardless of what's in this list — so
        # `transports=["tcp"]` actually binds `[local, tcp]` per
        # module, and a same-host client can still dial via
        # LocalSocket. The list controls what *additional*
        # outside-facing listeners get bound. Naming `local` here is
        # a no-op (idempotent with the implicit one). Omitting this
        # argument entirely emits no `modules` block and the daemon's
        # default (a single `local` listener per well-known module)
        # applies.
        transports: list[str] | None = None,
        tcp_host: str = "127.0.0.1",
        # `tcp_port` is core_service's port. `tcp_cap_port` is
        # capability_module's. They MUST be distinct: each module
        # opens its own listener, and two QTcpServers can't share an
        # address:port pair. Default 0 on both → daemon auto-allocates
        # ephemerals via PortAllocator (always distinct). Tests that
        # need the host to know the port up front pre-pick two free
        # ports and pass both.
        tcp_port: int = 0,
        tcp_cap_port: int = 0,
        tcp_codec: str = "json",        # "json" | "cbor"
        tcp_ssl_host: str = "127.0.0.1",
        tcp_ssl_port: int = 0,
        # Same dual-port story for tcp_ssl — capability_module's
        # tcp_ssl listener needs its own port when both are bound.
        tcp_ssl_cap_port: int = 0,
        tcp_ssl_codec: str = "json",    # "json" | "cbor"
        ssl_cert: str | Path | None = None,
        ssl_key: str | Path | None = None,
        ssl_ca: str | Path | None = None,
        # On-disk dial-spec value for `verify_peer` in the client
        # config.yaml the wrapper writes for tcp_ssl after startup. False
        # (default) suits the typical test setup where ssl_cert is
        # self-signed and wouldn't validate against any CA. Override to
        # True with a CA-issued cert if you want the full verification
        # path. Unlike logoscore there is no per-call escape from this:
        # the `LOGOSCORE_CLIENT_*` env family is gone, so the on-disk
        # value is the only thing the CLI reads (see `client()`).
        verify_peer: bool = False,
    ) -> None:
        if isinstance(modules_dir, (str, Path)):
            self.modules_dirs: list[Path] = [Path(modules_dir)]
        else:
            self.modules_dirs = [Path(p) for p in modules_dir]
        if not self.modules_dirs:
            raise ValueError("at least one modules_dir is required")

        self.binary = binary
        self.persistence_path = Path(persistence_path) if persistence_path else None
        self.extra_args = list(extra_args or [])
        self.extra_config = dict(extra_config or {})
        self.extra_env = dict(env or {})
        self.startup_timeout = startup_timeout
        self.transports = list(transports or [])
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.tcp_cap_port = tcp_cap_port
        self.tcp_codec = tcp_codec
        self.tcp_ssl_host = tcp_ssl_host
        self.tcp_ssl_port = tcp_ssl_port
        self.tcp_ssl_cap_port = tcp_ssl_cap_port
        self.tcp_ssl_codec = tcp_ssl_codec
        self.ssl_cert = Path(ssl_cert) if ssl_cert else None
        self.ssl_key = Path(ssl_key) if ssl_key else None
        self.ssl_ca = Path(ssl_ca) if ssl_ca else None
        self.verify_peer = verify_peer

        if config_dir is None:
            self._config_dir = Path(tempfile.mkdtemp(prefix="logosctl-"))
            self._owns_config_dir = True
        else:
            self._config_dir = Path(config_dir)
            self._config_dir.mkdir(parents=True, exist_ok=True)
            self._owns_config_dir = False

        self._process: subprocess.Popen[str] | None = None
        self._stdout_file: IO[str] | None = None
        self._stderr_file: IO[str] | None = None

    # ── Public API ──────────────────────────────────────────────────────────

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def state_file(self) -> Path:
        # Path to the daemon's live runtime-state file. Created at boot
        # (after transports actually bind AND the bundled package modules
        # load) and removed at clean shutdown. Carries instance_id, pid,
        # started_at, and the resolved transport endpoints (post-bind,
        # with real ports). Operator preferences live next to it in
        # config.yaml; persistent state (tokens.json) in its own file.
        return self._config_dir / "daemon" / "state.json"

    @property
    def connection_file(self) -> Path:
        # Backwards-compatible alias for state_file. Existing call sites
        # use this name (it predates the config/state split); kept so
        # downstream code doesn't have to migrate in lockstep.
        return self.state_file

    @property
    def daemon_config_file(self) -> Path:
        # Where `daemon config set` installs the document this wrapper
        # builds. Read it to see what the daemon will actually boot with —
        # the CLI re-emits it as canonical YAML, so it won't be byte-equal
        # to what we wrote.
        return self._config_dir / "daemon" / "config.yaml"

    @property
    def client_token_file(self) -> Path:
        # Path to the daemon-emitted local-client raw-token file. The
        # daemon writes this at boot from its in-memory raw value;
        # subsequent CLI invocations can reuse it without going through
        # env vars.
        return self._config_dir / "client" / "auto.json"

    @property
    def log_file(self) -> Path:
        # The daemon's own log — a symlink to this boot's
        # `logs/daemon_<stamp>.log`. logoscore had no file logging at
        # all; here LogSink dup2's the daemon's stdout+stderr into it.
        return self._config_dir / "logs" / "daemon.log"

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def start(self) -> None:
        if self._process is not None:
            raise LogosctlError("daemon already started")

        # Everything logoscore passed as daemon flags is configuration
        # now, and configuration is never passed alongside another
        # command: `daemon start` acts on whatever is already on disk. So
        # the document goes in first, in its own invocation.
        self._install_daemon_config()

        # `--config-dir` before the subcommand — it is an app-level
        # option, and a client subcommand would swallow it as a positional
        # argument. (`daemon` itself has fallthrough, so this is belt and
        # braces there, but the rule is uniform and worth keeping.)
        cmd: list[str] = [
            self.binary, "--config-dir", str(self._config_dir), "daemon", "start",
        ]
        cmd.extend(self.extra_args)

        # Foreground `daemon start` under our own Popen, rather than
        # `daemon start --detach`. --detach is nicer on paper — it blocks
        # until the daemon is ready, so no polling — but it hands back no
        # child handle, which would cost stop() its terminate/kill
        # fallback and force us to re-discover the pid from state.json.
        # The daemon's `logging.console` defaults to true, so LogSink
        # still mirrors everything into these pipes.
        self._stdout_file = open(self._config_dir / "daemon.stdout.log", "w")
        self._stderr_file = open(self._config_dir / "daemon.stderr.log", "w")

        self._process = subprocess.Popen(
            cmd,
            stdout=self._stdout_file,
            stderr=self._stderr_file,
            env=self._child_env(),
            start_new_session=True,
        )

        try:
            self._wait_for_ready()
        except Exception:
            self.stop()
            raise

    def stop(self, timeout: float = 10.0) -> None:
        """Shut the daemon down. Safe to call multiple times."""
        proc = self._process
        if proc is not None:
            # Ask the daemon to stop itself first — cleanest path. This is
            # an RPC, so it needs a usable client config in this dir; the
            # daemon writes one into its own session at boot, so it
            # normally works. When it doesn't (a startup that got as far
            # as state.json but no further), the escalation below is what
            # actually reaps the process.
            if proc.poll() is None:
                try:
                    _proc.run_json(
                        self.binary, ["daemon", "stop"],
                        config_dir=self._config_dir,
                        # Same env the daemon got: a caller who pinned
                        # TMPDIR (to keep the local socket path under
                        # sockaddr_un's 104-byte cap) has to reach the
                        # daemon through the same one.
                        env=self.extra_env or None,
                        timeout=timeout,
                    )
                except Exception:
                    pass  # fall through to terminate/kill
            # Wait for process exit; escalate if necessary.
            if proc.poll() is None:
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            self._process = None

        for f in (self._stdout_file, self._stderr_file):
            if f is not None and not f.closed:
                f.close()
        self._stdout_file = None
        self._stderr_file = None

        if self._owns_config_dir and self._config_dir.exists():
            shutil.rmtree(self._config_dir, ignore_errors=True)

    def client(
        self,
        *,
        timeout: float | None = 30.0,
        transport: str | None = None,
        tcp_host: str | None = None,
        no_verify_peer: bool = False,
        codec: str | None = None,
    ) -> LogosctlClient:
        """Build a client wired to this daemon's `client/config.yaml`.

        The daemon writes a per-module dial spec at startup (auto-emitted
        for `local`; rewritten from `state.json` for `tcp`/`tcp_ssl` — see
        `_write_own_client_config`), so the default `client()` needs no
        transport args: `core_service` and `capability_module` each dial
        their own bound port straight from disk.

        The optional `transport` / `tcp_host` / `codec` / `no_verify_peer`
        overrides rewrite that on-disk spec (uniformly across both
        modules, since they share host/transport/codec/verify). logoscore
        could also express these as `LOGOSCORE_CLIENT_*` env vars; that
        whole family was deleted, so the file is now the ONLY way to
        retarget a client — if the rewrite fails there is no fallback.
        There is deliberately no per-call port override: each module's
        port comes from the daemon's own state and is left untouched.
        """
        if self._process is None:
            raise LogosctlError(
                "daemon is not running — call start() or use the context manager"
            )
        if (transport is not None or tcp_host is not None
                or codec is not None or no_verify_peer):
            self._write_own_client_config(
                transport=transport, host=tcp_host, codec=codec,
                verify_peer=False if no_verify_peer else None)
        return LogosctlClient(
            binary=self.binary,
            config_dir=self._config_dir,
            token=self._read_token(),
            timeout=timeout,
        )

    def remote_client(
        self,
        config_dir: str | Path | None = None,
        *,
        transport: str | None = None,
        host: str | None = None,
        codec: str | None = None,
        verify_peer: bool | None = None,
        timeout: float | None = 30.0,
        binary: str | None = None,
    ) -> LogosctlClient:
        """Build a client that drives this daemon from a SEPARATE config dir.

        This is the shape logoscore expressed with `LOGOSCORE_CLIENT_*`
        env vars. They're gone: a dial spec is a file, so a client living
        outside the daemon's session needs its own
        `<config_dir>/client/config.yaml` plus a copy of a token the
        daemon accepts. `LogosctlClient.connect` writes both; what this
        adds is the endpoints — read back from `state.json`, since the
        ports are only known post-bind when the caller asked for
        ephemerals — and the token itself.

        `config_dir=None` uses a private temp dir that is removed when the
        returned client is garbage collected. A `local` transport
        additionally needs the daemon's instance id (the socket's registry
        name embeds it, and it's carried through here) and a shared
        `$TMPDIR` — QLocalServer resolves the bare socket name against it.
        """
        if self._process is None:
            raise LogosctlError(
                "daemon is not running — call start() or use the context manager"
            )
        state = self._read_state()
        endpoints = self._endpoints_from_state(
            state, transport=transport, host=host,
            codec=codec, verify_peer=verify_peer)
        # The daemon's own boot token. It is issued local-only, yet the
        # runtime accepts it over tcp/tcp_ssl (the raw value is registered
        # with the in-process TokenManager, ahead of the store validator
        # that would reject it) — which is what makes "copy auto.json" the
        # documented provisioning move. A client that would rather not
        # rest on that quirk should issue a named token instead (see
        # `tokens.issue_token`, without `local_only`).
        token = self._read_token()
        if token is None:
            raise LogosctlError(
                f"daemon has not emitted a client token at {self.client_token_file}"
                " — cannot wire up an authenticated client"
            )
        return LogosctlClient.connect(
            endpoints,
            token=token,
            binary=binary or self.binary,
            config_dir=config_dir,
            timeout=timeout,
            instance_id=state.get("instance_id"),
        )

    def endpoints(
        self,
        transport: str | None = None,
        *,
        host: str | None = None,
        codec: str | None = None,
        verify_peer: bool | None = None,
    ) -> dict[str, DaemonEndpoint]:
        """Per-module dial spec for this daemon, read back from `state.json`.

        Defaults to the first network protocol the wrapper asked for (else
        `local`). Handy on its own — a test that let the daemon allocate
        ephemeral ports has no other way to learn what they were."""
        return self._endpoints_from_state(
            self._read_state(), transport=transport, host=host,
            codec=codec, verify_peer=verify_peer)

    def logs(self) -> tuple[str, str]:
        """Return (stdout, stderr) captured from the daemon so far."""
        out = (self._config_dir / "daemon.stdout.log")
        err = (self._config_dir / "daemon.stderr.log")
        stdout = out.read_text() if out.exists() else ""
        stderr = err.read_text() if err.exists() else ""
        # Unlike logoscore, the daemon keeps its own log file too. With
        # `logging.console` on (the default) it and the pipes above carry
        # the same bytes; if a caller turned the console mirror off via
        # extra_config, the pipes are empty and the log file is the only
        # record — so fall back to it rather than reporting nothing.
        if not stdout.strip():
            stdout = self.daemon_log()
        return stdout, stderr

    def daemon_log(self) -> str:
        """Contents of `<config_dir>/logs/daemon.log` (a symlink to this
        boot's `daemon_<stamp>.log`). Empty when the daemon hasn't opened
        it yet or logging was disabled."""
        try:
            return self.log_file.read_text()
        except OSError:
            return ""

    # ── Context manager ─────────────────────────────────────────────────────

    def __enter__(self) -> "LogosctlDaemon":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ── Internal ────────────────────────────────────────────────────────────

    def _child_env(self) -> dict[str, str]:
        # LOGOSCTL_CONFIG_DIR and LOGOSCTL_TOKEN are the only env vars the
        # binary reads — the LOGOSCORE_CLIENT_* family has no counterpart
        # here, so there is nothing else to set.
        env = os.environ.copy()
        env["LOGOSCTL_CONFIG_DIR"] = str(self._config_dir)
        env.update(self.extra_env)
        return env

    def _daemon_config_document(self) -> dict:
        """Build the daemon YAML document — the modules dirs, persistence
        path and per-module listeners that used to be command-line flags."""
        doc: dict[str, Any] = {
            "modules_dirs": [str(_abs(d)) for d in self.modules_dirs],
        }
        if self.persistence_path is not None:
            doc["persistence_path"] = str(_abs(self.persistence_path))

        # Per-module listeners: one entry per (module, protocol), the
        # direct translation of logoscore's repeated `--module-transport`
        # flags.
        #
        # Each module gets its OWN port — `tcp_port` / `tcp_ssl_port` for
        # core_service, `tcp_cap_port` / `tcp_ssl_cap_port` for
        # capability_module. Reusing a single port across both fails the
        # second listener's bind because QTcpServer can't share an
        # address:port pair. Default 0 makes the daemon auto-allocate
        # distinct ephemerals.
        port_for = {
            ("tcp",     "core_service"):      self.tcp_port,
            ("tcp",     "capability_module"): self.tcp_cap_port,
            ("tcp_ssl", "core_service"):      self.tcp_ssl_port,
            ("tcp_ssl", "capability_module"): self.tcp_ssl_cap_port,
        }
        modules: dict[str, list[dict]] = {}
        for proto in self.transports:
            if proto == "local":
                # The daemon prepends a local listener to every module
                # unconditionally, so naming it is a no-op — and leaving
                # it out keeps a local-only document down to zero
                # `modules` keys.
                continue
            for module in ("core_service", "capability_module"):
                # `protocol:` — the daemon-side spelling. The client side
                # of the same idea says `transport:`, and mixing the two
                # up fails the whole config parse (the value lands empty
                # and misses the allowlist), so the asymmetry is worth
                # being loud about.
                listener: dict[str, Any] = {"protocol": proto}
                if proto == "tcp":
                    listener.update(
                        host=self.tcp_host,
                        port=port_for[(proto, module)],
                        codec=self.tcp_codec,
                    )
                elif proto == "tcp_ssl":
                    if not (self.ssl_cert and self.ssl_key):
                        raise LogosctlError(
                            "transports includes 'tcp_ssl' but "
                            "ssl_cert/ssl_key not set"
                        )
                    listener.update(
                        host=self.tcp_ssl_host,
                        port=port_for[(proto, module)],
                        codec=self.tcp_ssl_codec,
                        # Per-listener `cert`/`key`. NOT the top-level
                        # `ssl:` block — that one is parsed and then read
                        # by nobody, so a listener configured through it
                        # binds with no certificate and every handshake
                        # dies with "no shared cipher".
                        cert=str(_abs(self.ssl_cert)),
                        key=str(_abs(self.ssl_key)),
                    )
                    if self.ssl_ca:
                        # `ca_file` here; the client's spelling for the
                        # same file is a bare `ca`.
                        listener["ca_file"] = str(_abs(self.ssl_ca))
                modules.setdefault(module, []).append(listener)
        if modules:
            doc["modules"] = modules

        doc.update(self.extra_config)

        # The daemon refuses a plaintext tcp listener on a non-loopback
        # host unless the exposure is explicitly declared intentional.
        # logoscore spelled that `--insecure-tcp`; here it's a config key,
        # set for exactly the binds that would otherwise be refused.
        #
        # Derived from the FINAL document, after the extra_config merge:
        # `modules` is one of the keys a caller can legitimately supply
        # that way, and the daemon runs its own guard over the merged map
        # (main.cpp, "Plaintext-TCP guard, post-merge") — so deriving this
        # from the wrapper-built listeners alone would leave an
        # extra_config listener with insecure_tcp absent and a daemon that
        # refuses to boot, blaming the listener rather than the ordering.
        # An explicit `insecure_tcp` in extra_config still wins: it is a
        # deliberate declaration in either direction.
        if "insecure_tcp" not in doc and _binds_public_plaintext_tcp(doc):
            doc["insecure_tcp"] = True
        return doc

    def _install_daemon_config(self) -> None:
        """Write the daemon document and install it with `daemon config set`.

        Deliberately not routed through `_proc.run_json`: the CLI reports a
        rejected document as an error envelope whose useful half is the
        `message` (it names the offending key and lists the ones it knows),
        and run_json's exception carries only the exit code and the
        machine-readable `code`. A rejected key here means the daemon boots
        without the caller's modules dirs or listeners, so the message is
        the whole point."""
        doc = self._daemon_config_document()
        _check_config_types(doc)

        source = self._config_dir / "daemon.yaml"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(_yaml_document(doc))

        cmd = [self.binary, "--config-dir", str(self._config_dir),
               "daemon", "config", "set", str(source)]
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            env=self._child_env(), timeout=self.startup_timeout,
        )
        if proc.returncode != 0:
            detail = "\n".join(
                s for s in ((proc.stdout or "").strip(), (proc.stderr or "").strip())
                if s
            )
            raise LogosctlError(
                f"daemon config was rejected (exit {proc.returncode}): "
                f"{' '.join(cmd)}"
                + (f"\n{detail}" if detail else "")
                + f"\nThe document we submitted is at {source}. A schema error "
                  "is reported AFTER the write, so treat "
                  f"{self.daemon_config_file} as unusable and rewrite it "
                  "before starting a daemon in this session.",
                exit_code=proc.returncode,
                stderr=proc.stderr,
            )

    def _read_token(self) -> str | None:
        # Tokens live in <configDir>/client/auto.json (the daemon-emitted
        # local-client raw token). The hashed-at-rest token list is in
        # <configDir>/daemon/tokens.json — that file is what the daemon
        # validates against, but the raw token we use for client RPC comes
        # from client/auto.json.
        path = self.client_token_file
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text()).get("token")
        except (json.JSONDecodeError, OSError):
            return None

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            raise LogosctlError(f"daemon state.json unreadable: {e}") from e

    def _wait_for_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        conn = self.state_file
        proc = self._process
        assert proc is not None

        # Phase 1: wait for daemon/state.json to exist.
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                # BOTH streams, not just stderr. logosctl installs its LogSink
                # early in boot, and that sink dup2s stdout and stderr into one
                # pipe — so under this binary the reason a daemon died usually
                # arrives on *stdout*. Reading only stderr left every
                # post-LogSink startup failure as a bare exit code with nothing
                # attached, which is precisely the dead end the CLI's own
                # --detach path had.
                out, err = self.logs()
                detail = "\n".join(
                    part.strip() for part in (err, out) if part.strip()
                )
                raise LogosctlError(
                    f"daemon exited during startup (code {proc.returncode})"
                    + (f"\n{detail}" if detail else "")
                )
            if conn.exists():
                break
            time.sleep(0.05)
        else:
            # Process is still alive but state.json never appeared.
            # Surface whatever it logged so the diagnostic is more
            # useful than "did not write" — silent hangs almost
            # always have *something* on stdout (the daemon's normal
            # progress messages) or stderr (qDebug/qWarning) that
            # explains why bind / token / module load got stuck.
            out, err = self.logs()
            out_tail = out.strip().splitlines()[-50:] if out.strip() else []
            err_tail = err.strip().splitlines()[-50:] if err.strip() else []
            # Only point at the log file if it will still be there to read.
            # start() calls stop() when startup fails, and stop() removes the
            # config dir when it created it — so on the common path (no
            # explicit config_dir) this message named a file that was deleted
            # microseconds later. The tails below are attached precisely so the
            # reason travels with the exception instead of living in a file the
            # caller has to go find.
            where = "" if self._owns_config_dir else f" (full log: {self.log_file})"
            sections = [
                f"daemon did not write {conn} within {self.startup_timeout}s"
                f"{where}",
            ]
            if out_tail:
                sections.append(
                    f"--- daemon stdout (last {len(out_tail)} lines) ---\n"
                    + "\n".join(out_tail))
            if err_tail:
                sections.append(
                    f"--- daemon stderr (last {len(err_tail)} lines) ---\n"
                    + "\n".join(err_tail))
            raise LogosctlError("\n".join(sections))

        # Phase 1.5: rewrite client/config.yaml from state.json. The
        # daemon's auto-emitted config always advertises LocalSocket
        # for the same-host client; for `tcp` / `tcp_ssl` runs the
        # daemon binds different transports and the LocalSocket entry
        # is wrong. We patch in the actual resolved per-module
        # endpoints before the next phase tries to call `status`.
        if self._network_transport() is not None:
            self._write_own_client_config()

        # Phase 2: verify we can talk to it via `status`.
        remaining = max(1.0, deadline - time.monotonic())
        try:
            _proc.run_json(
                self.binary, ["status"],
                config_dir=self._config_dir,
                token=self._read_token(),
                env=self.extra_env or None,
                timeout=remaining,
            )
        except LogosctlError as e:
            raise LogosctlError(f"daemon status check failed: {e}") from e

    def _network_transport(self) -> str | None:
        """The first `tcp`/`tcp_ssl` protocol the wrapper asked the daemon
        to bind, or None for a local-only daemon. Tests pass exactly one
        of the two; a caller that passes `["local", "tcp"]` still gets the
        network listener surfaced in the client config, so the tcp path
        stays reachable."""
        for p in self.transports:
            if p in ("tcp", "tcp_ssl"):
                return p
        return None

    def _write_own_client_config(
        self,
        *,
        transport: str | None = None,
        host: str | None = None,
        codec: str | None = None,
        verify_peer: bool | None = None,
    ) -> None:
        """Rebuild `<config_dir>/client/config.yaml` from `state.json`.

        logoscore edited the daemon's auto-emitted config.json in place so
        it kept any field the high-level API didn't model. That isn't
        possible here: the daemon emits canonical block YAML and this
        package has no YAML reader. Rebuilding is equivalent in practice —
        state.json carries every resolved endpoint, `write_config`
        re-defaults `token_file` to the same `auto.json` the daemon wrote,
        and the instance id is carried across explicitly below because a
        `local` dial resolves its socket name through it."""
        state = self._read_state()
        endpoints = self._endpoints_from_state(
            state, transport=transport, host=host,
            codec=codec, verify_peer=verify_peer)
        LogosctlClient.write_config(
            self._config_dir, endpoints, instance_id=state.get("instance_id"))

    def _endpoints_from_state(
        self,
        state: dict,
        *,
        transport: str | None = None,
        host: str | None = None,
        codec: str | None = None,
        verify_peer: bool | None = None,
    ) -> dict[str, DaemonEndpoint]:
        """One `DaemonEndpoint` per well-known module, read out of the
        daemon's resolved state (post-bind, so ephemeral ports are real)."""
        proto = transport or self._network_transport() or "local"

        modules = state.get("resolved", {}).get("modules", {})
        endpoints: dict[str, DaemonEndpoint] = {}
        for module_name in ("core_service", "capability_module"):
            listeners = modules.get(module_name, {}).get("transports", [])
            match = next(
                (t for t in listeners if t.get("protocol") == proto),
                None,
            )
            if match is None:
                raise LogosctlError(
                    f"daemon state.json doesn't advertise '{proto}' "
                    f"for module '{module_name}' — wrapper transports "
                    f"setup is out of sync with the running daemon"
                )
            if proto == "local":
                # A local entry carries no network fields at all; host,
                # port and codec would be meaningless noise in the file.
                endpoints[module_name] = DaemonEndpoint(transport="local")
                continue

            bound_host = match.get("host", "127.0.0.1")
            # Daemons that bind 0.0.0.0 are reachable via 127.0.0.1
            # for same-host clients. Any host-side dial that goes
            # through the bind address would refuse on platforms
            # that don't auto-route 0.0.0.0 → loopback.
            # A CA is only meaningful for tcp_ssl, and it's spelled `ca`
            # on this side of the wire (`ca_file` is the daemon's). Passed
            # only when we have one so the endpoint's other fields stay
            # exactly what logoscore wrote.
            extra: dict[str, Any] = {}
            if proto == "tcp_ssl" and self.ssl_ca:
                extra["ca"] = str(_abs(self.ssl_ca))
            endpoints[module_name] = DaemonEndpoint(
                transport=proto,
                host=host or ("127.0.0.1" if bound_host == "0.0.0.0" else bound_host),
                port=match.get("port", 0),
                codec=codec or match.get("codec", "json"),
                verify_peer=(
                    (self.verify_peer if verify_peer is None else verify_peer)
                    if proto == "tcp_ssl" else None),
                **extra,
            )
        return endpoints
