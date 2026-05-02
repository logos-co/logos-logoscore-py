"""Lifecycle manager for a `logoscore` daemon process.

`LogoscoreDaemon` is a context manager: on `__enter__` it spawns
`logoscore -D` with an isolated `--config-dir`, waits for the daemon's
connection file to appear, and verifies liveness with `status`. On exit
it runs `logoscore stop`, then terminates/kills the child process as a
fallback, and removes any temp state directory it created.

The isolated config dir means multiple daemons can run concurrently in
the same test process without colliding on `~/.logoscore/daemon/`, and
nothing the wrapper does leaks into the developer's global state.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import IO

from . import _proc
from .client import LogoscoreClient
from .errors import LogoscoreError


class LogoscoreDaemon:
    """Context manager that spawns and tears down a logoscore daemon."""

    def __init__(
        self,
        modules_dir: str | Path | list[str | Path],
        *,
        binary: str = "logoscore",
        config_dir: str | Path | None = None,
        persistence_path: str | Path | None = None,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        startup_timeout: float = 15.0,
        # Per-module transport list applied to BOTH `core_service` and
        # `capability_module` — i.e. every protocol named here becomes
        # one `--module-transport <module>=<protocol>[,k=v...]` flag
        # per module on the daemon command line. `local` is always on
        # for the well-known modules even if omitted here (the daemon
        # defaults missing modules to local-only). Add `tcp` /
        # `tcp_ssl` to expose TCP listeners for remote clients.
        transports: list[str] | None = None,
        tcp_host: str = "127.0.0.1",
        tcp_port: int = 0,
        tcp_codec: str = "json",        # "json" | "cbor"
        tcp_ssl_host: str = "127.0.0.1",
        tcp_ssl_port: int = 0,
        tcp_ssl_codec: str = "json",    # "json" | "cbor"
        ssl_cert: str | Path | None = None,
        ssl_key: str | Path | None = None,
        ssl_ca: str | Path | None = None,
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
        self.extra_env = dict(env or {})
        self.startup_timeout = startup_timeout
        self.transports = list(transports or [])
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.tcp_codec = tcp_codec
        self.tcp_ssl_host = tcp_ssl_host
        self.tcp_ssl_port = tcp_ssl_port
        self.tcp_ssl_codec = tcp_ssl_codec
        self.ssl_cert = Path(ssl_cert) if ssl_cert else None
        self.ssl_key = Path(ssl_key) if ssl_key else None
        self.ssl_ca = Path(ssl_ca) if ssl_ca else None

        if config_dir is None:
            self._config_dir = Path(tempfile.mkdtemp(prefix="logoscore-"))
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
        # (after transports actually bind) and removed at clean
        # shutdown. Carries instance_id, pid, started_at, and the
        # resolved transport endpoints (post-bind, with real ports).
        # Persistent state (tokens.json) and operator preferences
        # (config.json, written only on --persist-config) live in
        # their own files.
        return self._config_dir / "daemon" / "state.json"

    @property
    def connection_file(self) -> Path:
        # Backwards-compatible alias for state_file. Existing call sites
        # use this name (it predates the config/state split); kept so
        # downstream code doesn't have to migrate in lockstep.
        return self.state_file

    @property
    def client_token_file(self) -> Path:
        # Path to the daemon-emitted local-client raw-token file. The
        # daemon writes this at boot from its in-memory raw value;
        # subsequent CLI invocations can reuse it without going
        # through env vars.
        return self._config_dir / "client" / "auto.json"

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def start(self) -> None:
        if self._process is not None:
            raise LogoscoreError("daemon already started")

        cmd: list[str] = [self.binary, "-D", "--config-dir", str(self._config_dir)]
        for d in self.modules_dirs:
            cmd.extend(["-m", str(d)])
        if self.persistence_path is not None:
            cmd.extend(["--persistence-path", str(self.persistence_path)])
        # Per-module transport flags. The daemon expects
        # `--module-transport NAME=PROTOCOL[,k=v...]` (repeatable). We
        # emit one entry per requested protocol for both well-known
        # modules (`core_service` and `capability_module`); they share
        # the same listener spec by default. Operators with
        # finer-grained needs (different ports per module, asymmetric
        # protocols) can drop down to extra_args + omit this fixture.
        for proto in self.transports:
            for module in ("core_service", "capability_module"):
                spec = f"{module}={proto}"
                if proto == "tcp":
                    spec += (f",host={self.tcp_host}"
                             f",port={self.tcp_port}"
                             f",codec={self.tcp_codec}")
                elif proto == "tcp_ssl":
                    if not (self.ssl_cert and self.ssl_key):
                        raise LogoscoreError(
                            "transports includes 'tcp_ssl' but "
                            "ssl_cert/ssl_key not set"
                        )
                    spec += (f",host={self.tcp_ssl_host}"
                             f",port={self.tcp_ssl_port}"
                             f",codec={self.tcp_ssl_codec}"
                             f",cert={self.ssl_cert}"
                             f",key={self.ssl_key}")
                    if self.ssl_ca:
                        spec += f",ca={self.ssl_ca}"
                cmd.extend(["--module-transport", spec])
        cmd.extend(self.extra_args)

        env = os.environ.copy()
        env["LOGOSCORE_CONFIG_DIR"] = str(self._config_dir)
        env.update(self.extra_env)

        self._stdout_file = open(self._config_dir / "daemon.stdout.log", "w")
        self._stderr_file = open(self._config_dir / "daemon.stderr.log", "w")

        self._process = subprocess.Popen(
            cmd,
            stdout=self._stdout_file,
            stderr=self._stderr_file,
            env=env,
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
            # Ask the daemon to stop itself first — cleanest path.
            if proc.poll() is None:
                try:
                    _proc.run_json(
                        self.binary, ["stop"],
                        config_dir=self._config_dir,
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
        tcp_port: int | None = None,
        no_verify_peer: bool = False,
        codec: str | None = None,
    ) -> LogoscoreClient:
        if self._process is None:
            raise LogoscoreError(
                "daemon is not running — call start() or use the context manager"
            )
        return LogoscoreClient(
            binary=self.binary,
            config_dir=self._config_dir,
            token=self._read_token(),
            timeout=timeout,
            transport=transport,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            no_verify_peer=no_verify_peer,
            codec=codec,
        )

    def logs(self) -> tuple[str, str]:
        """Return (stdout, stderr) captured from the daemon so far."""
        out = (self._config_dir / "daemon.stdout.log")
        err = (self._config_dir / "daemon.stderr.log")
        return (
            out.read_text() if out.exists() else "",
            err.read_text() if err.exists() else "",
        )

    # ── Context manager ─────────────────────────────────────────────────────

    def __enter__(self) -> "LogoscoreDaemon":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ── Internal ────────────────────────────────────────────────────────────

    def _read_token(self) -> str | None:
        # Tokens live in <configDir>/client/auto.json (the
        # daemon-emitted local-client raw token). The hashed-at-rest
        # token list is in <configDir>/daemon/tokens.json — that file
        # is what the daemon validates against, but the raw token
        # we use for client RPC comes from client/auto.json.
        path = self.client_token_file
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text()).get("token")
        except (json.JSONDecodeError, OSError):
            return None

    def _wait_for_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        conn = self.state_file
        proc = self._process
        assert proc is not None

        # Phase 1: wait for daemon/state.json to exist.
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                _, err = self.logs()
                raise LogoscoreError(
                    f"daemon exited during startup (code {proc.returncode})"
                    + (f"\n{err.strip()}" if err.strip() else "")
                )
            if conn.exists():
                break
            time.sleep(0.05)
        else:
            raise LogoscoreError(
                f"daemon did not write {conn} within {self.startup_timeout}s"
            )

        # Phase 2: verify we can talk to it via `status`.
        remaining = max(1.0, deadline - time.monotonic())
        try:
            _proc.run_json(
                self.binary, ["status"],
                config_dir=self._config_dir,
                token=self._read_token(),
                timeout=remaining,
            )
        except LogoscoreError as e:
            raise LogoscoreError(f"daemon status check failed: {e}") from e
