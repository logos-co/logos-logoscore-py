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
the daemon's setup is expressible on the command line: modules dirs, the
persistence path and everything else are a YAML document installed with
`logosctl daemon config set FILE`. `daemon start` acts on whatever is
already on disk, so `start()` has two phases — install the config, then
boot — and a document the CLI rejects has to be a hard error: the daemon
would otherwise come up silently missing every modules dir the caller
asked for.
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
from typing import Any, IO, Mapping

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
        # (after its listeners bind AND the bundled package modules load)
        # and removed at clean shutdown. Carries instance_id, pid,
        # started_at, and the resolved listeners. Operator preferences live
        # next to it in config.yaml; persistent state (tokens.json) in its
        # own file.
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
                        token=self._read_token(),
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

    def client(self, *, timeout: float | None = 30.0) -> LogosctlClient:
        """Build a client wired to this daemon's `client/config.yaml`, which
        the daemon writes into its own session at boot."""
        if self._process is None:
            raise LogosctlError(
                "daemon is not running — call start() or use the context manager"
            )
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
        timeout: float | None = 30.0,
        binary: str | None = None,
    ) -> LogosctlClient:
        """Build a client that drives this daemon from a SEPARATE config dir
        on this machine, over the daemon's local socket.

        A client living outside the daemon's session needs its own
        `<config_dir>/client/config.yaml` plus a copy of a token the daemon
        accepts; `LogosctlClient.connect` writes both. The socket's name
        embeds the daemon's instance id, carried through here, and
        QLocalServer resolves it against `$TMPDIR`, so both sides need the
        same one. A client on another machine uses Remote Runtime Control
        instead (`logosctl.remote.RuntimeControl`).

        `config_dir=None` uses a private temp dir that is removed when the
        returned client is garbage collected.
        """
        if self._process is None:
            raise LogosctlError(
                "daemon is not running — call start() or use the context manager"
            )
        state = self._read_state()
        endpoints = self._endpoints_from_state(state)
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

    def endpoints(self) -> dict[str, DaemonEndpoint]:
        """Per-module dial spec for this daemon, checked against `state.json`."""
        return self._endpoints_from_state(self._read_state())

    def peer(self, verb: str, *args: str, timeout: float | None = None) -> Any:
        """`logosctl peer <verb> [args…]` on this daemon, as its operator."""
        return self.client().peer(verb, *args, timeout=timeout)

    def set_remote_policy(self, policy: Mapping[str, Any]) -> Any:
        """Replace this daemon's remote policy (`logosctl peer policy set`):
        `{"<runtime id>/<consumer>": grants}`. Needs a `peering` section."""
        path = self._config_dir / "remote-policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        reply = self.peer("policy", "set", str(path))
        if not (isinstance(reply, dict) and reply.get("ok") is True):
            raise LogosctlError(f"the daemon did not take its remote policy: {reply!r}")
        return reply

    def logs(self) -> tuple[str, str]:
        """Return (stdout, stderr) captured from the daemon so far."""
        out = (self._config_dir / "daemon.stdout.log")
        err = (self._config_dir / "daemon.stderr.log")
        stdout = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
        stderr = err.read_text(encoding="utf-8", errors="replace") if err.exists() else ""
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
            return self.log_file.read_text(encoding="utf-8", errors="replace")
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
        # binary reads, and a daemon needs no token.
        env = os.environ.copy()
        env["LOGOSCTL_CONFIG_DIR"] = str(self._config_dir)
        env.update(self.extra_env)
        return env

    def _daemon_config_document(self) -> dict:
        """Build the daemon YAML document — the modules dirs and persistence
        path that used to be command-line flags, then `extra_config`."""
        doc: dict[str, Any] = {
            "modules_dirs": [str(_abs(d)) for d in self.modules_dirs],
        }
        if self.persistence_path is not None:
            doc["persistence_path"] = str(_abs(self.persistence_path))
        doc.update(self.extra_config)
        return doc

    def _install_daemon_config(self) -> None:
        """Write the daemon document and install it with `daemon config set`.

        Deliberately not routed through `_proc.run_json`: the CLI reports a
        rejected document as an error envelope whose useful half is the
        `message` (it names the offending key and lists the ones it knows),
        and run_json's exception carries only the exit code and the
        machine-readable `code`. A rejected key here means the daemon boots
        without the caller's modules dirs, so the message is the whole
        point."""
        doc = self._daemon_config_document()
        _check_config_types(doc)

        source = self._config_dir / "daemon.yaml"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(_yaml_document(doc), encoding="utf-8")

        cmd = [self.binary, "--config-dir", str(self._config_dir),
               "daemon", "config", "set", str(source)]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
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
        # The hashed-at-rest list is <configDir>/daemon/tokens.json — that
        # file is what the daemon validates against, but the raw token we
        # use for client RPC comes from client/auto.json.
        path = self.client_token_file
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("token")
        except (json.JSONDecodeError, OSError):
            return None

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
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

    def _endpoints_from_state(self, state: dict) -> dict[str, DaemonEndpoint]:
        """One local `DaemonEndpoint` per well-known module, once the
        daemon's resolved state says it listens there."""
        modules = state.get("resolved", {}).get("modules", {})
        endpoints: dict[str, DaemonEndpoint] = {}
        for module_name in ("core_service", "capability_module"):
            listeners = modules.get(module_name, {}).get("transports", [])
            if not any(t.get("protocol") == "local" for t in listeners):
                raise LogosctlError(
                    f"daemon state.json doesn't advertise a local listener "
                    f"for module '{module_name}'"
                )
            endpoints[module_name] = DaemonEndpoint()
        return endpoints
