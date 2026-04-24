"""Lifecycle manager for a `logoscore` daemon running inside docker.

`LogoscoreDockerDaemon` is to `LogoscoreDaemon` what its name suggests:
the same context-manager shape, but the daemon runs in a container and
speaks TCP to the host. Use it when your test setup deliberately
crosses a container boundary — e.g. you want to smoke-test a real
distribution of logoscore, or you need the daemon to be reachable from
multiple processes on the host.

Example:
    from logoscore import LogoscoreDockerDaemon

    with LogoscoreDockerDaemon(
        image="logoscore:smoke-portable",
        modules_dir="./my-module/result/modules",
    ) as daemon:
        client = daemon.client(binary="./logoscore")
        client.load_module("my_module")
        print(client.call("my_module", "do_something", 42))

Volume layout inside the container (all three dirs are on the host and
bind-mounted in — they survive the container):
    /config       — daemon.json lives here (read by the host-side client)
    /persistence  — `--persistence-path`; pre-seed to restore a session,
                    read back to inspect what modules wrote
    /user-modules — compiled Qt plugins, mounted read-only; loaded by the
                    daemon via `-m /user-modules`

Port strategy (status-go `tests-functional` pattern):
    container-internal TCP port is fixed at `CONTAINER_TCP_PORT` (6000).
    The host maps an ephemeral `host_port` to it via `-p …:6000`. The
    client is told to dial `host_port` via `LOGOSCORE_CLIENT_TCP_PORT`
    so the daemon-written `daemon.json` (which still advertises 6000)
    doesn't mislead it.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Iterable, Sequence

from .client import LogoscoreClient
from .errors import LogoscoreError


# ── Module-level helpers (also re-exported from the package) ──────────────

# Fixed TCP port the daemon binds *inside* the container. The host side
# always uses a dynamically-picked ephemeral port and port-forwards it
# in. See module docstring for the full rationale.
CONTAINER_TCP_PORT = 6000


def docker_available() -> bool:
    """True iff `docker` is on PATH and responsive to `docker info`."""
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "info"], capture_output=True, text=True)
    return r.returncode == 0


def image_present(image: str) -> bool:
    """True iff `docker image inspect <image>` returns successfully."""
    r = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def pick_free_port() -> int:
    """Pick an ephemeral TCP port by binding + closing. TOCTOU-racy in
    theory — another process could grab it before the caller rebinds —
    fine at typical test concurrency levels."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── The helper ────────────────────────────────────────────────────────────

class LogoscoreDockerDaemon:
    """Spawn a logoscore daemon inside a docker container and drive it
    from the host over TCP.

    Construction stores config only. `start()` actually runs the
    container (and waits for `daemon.json`); `stop()` kills it. Use the
    context-manager form to get start/stop bracketing automatically.
    """

    def __init__(
        self,
        *,
        image: str,
        modules_dir: str | Path,
        # Optional bits — sane defaults mean you can do
        # `LogoscoreDockerDaemon(image=..., modules_dir=...)`.
        config_dir: str | Path | None = None,
        persistence_dir: str | Path | None = None,
        host_port: int | None = None,
        codec: str = "json",
        container_name: str | None = None,
        extra_module_dirs: Sequence[str] | None = None,
        extra_args: Sequence[str] | None = None,
        startup_timeout: float = 20.0,
    ) -> None:
        self.image = image
        self.modules_dir = Path(modules_dir)
        self.codec = codec
        self.startup_timeout = startup_timeout
        # Additional dirs *inside the container* to scan for modules, on
        # top of `/opt/logoscore/modules` (CLI's built-in) and
        # `/user-modules` (the host `modules_dir` bind-mount). For most
        # callers empty.
        self.extra_module_dirs = list(extra_module_dirs or [])
        self.extra_args = list(extra_args or [])

        # Host-side dirs: either caller-supplied (persistent across
        # runs — useful for session restore) or freshly-minted tmpdirs
        # we own and clean up on stop().
        self._owns_config_dir = config_dir is None
        self._config_dir = (
            Path(tempfile.mkdtemp(prefix="logoscore-docker-cfg-"))
            if config_dir is None else Path(config_dir)
        )
        self._owns_persistence_dir = persistence_dir is None
        self._persistence_dir = (
            Path(tempfile.mkdtemp(prefix="logoscore-docker-pers-"))
            if persistence_dir is None else Path(persistence_dir)
        )

        self._host_port = host_port  # may be None until start()
        self._container_name = (
            container_name
            or f"logoscore-{uuid.uuid4().hex[:12]}"
        )
        self._container_id: str | None = None

    # ── Public properties ───────────────────────────────────────────────

    @property
    def host_port(self) -> int:
        """Dynamic host port mapped to the container's TCP listener.
        Only valid once `start()` has completed."""
        if self._host_port is None:
            raise LogoscoreError("daemon hasn't started yet")
        return self._host_port

    @property
    def config_dir(self) -> Path:
        """Host path of the daemon's config directory (contains
        `daemon.json` after startup)."""
        return self._config_dir

    @property
    def persistence_dir(self) -> Path:
        """Host path of the daemon's persistence directory
        (`--persistence-path`). Pre-seed before `start()` to restore a
        session; read back after `stop()` to inspect state."""
        return self._persistence_dir

    @property
    def container_id(self) -> str:
        if self._container_id is None:
            raise LogoscoreError("daemon hasn't started yet")
        return self._container_id

    @property
    def container_name(self) -> str:
        return self._container_name

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> "LogoscoreDockerDaemon":
        """`docker run` the daemon and block until it writes daemon.json.

        Raises LogoscoreError on docker failure / startup timeout. Does
        NOT check `docker_available()` or `image_present()` up front —
        callers that care about environmental skips should do so before
        calling start().
        """
        if self._container_id is not None:
            raise LogoscoreError("daemon is already started")

        if self._host_port is None:
            self._host_port = pick_free_port()

        cmd: list[str] = [
            "docker", "run", "--rm", "-d",
            "--name", self._container_name,
            # host_port (dynamic) → CONTAINER_TCP_PORT (fixed). See the
            # module docstring for why we don't bind the same port on
            # both sides.
            "-p", f"{self._host_port}:{CONTAINER_TCP_PORT}",
            "-v", f"{self._config_dir}:/config",
            "-v", f"{self._persistence_dir}:/persistence",
            "-v", f"{self.modules_dir}:/user-modules:ro",
            self.image,
            "daemon",
            "--config-dir", "/config",
            "--persistence-path", "/persistence",
            "--transport", "tcp",
            "--tcp-host", "0.0.0.0",
            "--tcp-port", str(CONTAINER_TCP_PORT),
            "--tcp-codec", self.codec,
            # -m is repeatable. /opt/logoscore/modules is the CLI's own
            # built-in modules (capability_module et al., populated in
            # the portable flavor; empty-but-harmless in dev). The
            # user's plugins come in via /user-modules. Extra dirs are
            # appended last.
            "-m", "/opt/logoscore/modules",
            "-m", "/user-modules",
        ]
        for d in self.extra_module_dirs:
            cmd += ["-m", d]
        cmd += list(self.extra_args)

        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise LogoscoreError(
                f"docker run failed (exit {r.returncode}):\n"
                f"  stderr: {r.stderr.strip()}\n"
                f"  cmd: {' '.join(cmd)}"
            )
        self._container_id = r.stdout.strip()

        if not self._wait_for_conn_file():
            # Capture container logs before tearing down — otherwise the
            # `--rm` container takes them with it and debugging is blind.
            logs = self._capture_logs()
            self.stop()
            raise LogoscoreError(
                f"daemon never wrote daemon.json within "
                f"{self.startup_timeout}s. Container logs:\n{logs}"
            )
        return self

    def stop(self) -> None:
        """Kill the container. Idempotent; safe to call even if start()
        never succeeded."""
        if self._container_id is not None:
            subprocess.run(
                ["docker", "rm", "-f", self._container_id],
                capture_output=True, text=True,
            )
            self._container_id = None

        # Only clean up dirs we created ourselves. Anything the caller
        # passed in (e.g. a pre-seeded persistence dir they want to
        # inspect after the test) stays on disk.
        if self._owns_config_dir and self._config_dir.exists():
            shutil.rmtree(self._config_dir, ignore_errors=True)
        if self._owns_persistence_dir and self._persistence_dir.exists():
            shutil.rmtree(self._persistence_dir, ignore_errors=True)

    # ── Client factory ──────────────────────────────────────────────────

    def client(
        self,
        *,
        binary: str = "logoscore",
        timeout: float | None = 30.0,
        tcp_host: str = "localhost",
        codec: str | None = None,
    ) -> LogoscoreClient:
        """Build a LogoscoreClient wired to dial this daemon.

        `binary` is the host-side `logoscore` executable — the client
        shells out to it for some operations (e.g. `watch`). Defaults
        to whatever `logoscore` resolves to on PATH.

        `tcp_host` defaults to localhost because the container's port
        is published there. Override for remote-docker setups.
        """
        if self._container_id is None:
            raise LogoscoreError(
                "daemon is not running — call start() or use the context manager"
            )
        return LogoscoreClient(
            binary=binary,
            config_dir=self._config_dir,
            timeout=timeout,
            transport="tcp",
            tcp_host=tcp_host,
            tcp_port=self.host_port,   # <-- the critical override
            codec=codec or self.codec,
        )

    # ── Internals ───────────────────────────────────────────────────────

    def _wait_for_conn_file(self) -> bool:
        deadline = time.monotonic() + self.startup_timeout
        conn_file = self._config_dir / "daemon.json"
        while time.monotonic() < deadline:
            if conn_file.exists():
                return True
            time.sleep(0.1)
        return False

    def _capture_logs(self) -> str:
        if self._container_id is None:
            return "<no container>"
        r = subprocess.run(
            ["docker", "logs", self._container_id],
            capture_output=True, text=True,
        )
        out = (r.stdout or "") + (r.stderr or "")
        return out.strip() or "<empty>"

    # ── Context manager ─────────────────────────────────────────────────

    def __enter__(self) -> "LogoscoreDockerDaemon":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()
