"""Lifecycle manager for a `logoscore` daemon process.

`LogoscoreDaemon` is a context manager: on `__enter__` it spawns
`logoscore -D` with an isolated `--config-dir`, waits for the daemon's
connection file to appear, and verifies liveness with `status`. On exit
it runs `logoscore stop`, then terminates/kills the child process as a
fallback, and removes any temp state directory it created.

The isolated config dir means multiple daemons can run concurrently in
the same test process without colliding on `~/.logoscore/daemon.json`,
and nothing the wrapper does leaks into the developer's global state.
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
    def connection_file(self) -> Path:
        return self._config_dir / "daemon.json"

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

    def client(self, *, timeout: float | None = 30.0) -> LogoscoreClient:
        if self._process is None:
            raise LogoscoreError(
                "daemon is not running — call start() or use the context manager"
            )
        return LogoscoreClient(
            binary=self.binary,
            config_dir=self._config_dir,
            token=self._read_token(),
            timeout=timeout,
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
        path = self.connection_file
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text()).get("token")
        except (json.JSONDecodeError, OSError):
            return None

    def _wait_for_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        conn = self.connection_file
        proc = self._process
        assert proc is not None

        # Phase 1: wait for daemon.json to exist.
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
