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


# Pinned to the same nixos/nix base the smoke image's stage-1 builder uses
# so the build closure (glibc, Qt, openssl, boost) lines up with what the
# daemon image was compiled against. Override via the env var if you've
# bumped the daemon image's builder base.
_BUILDER_IMAGE = os.environ.get("LOGOSCORE_BUILDER_IMAGE", "nixos/nix:2.24.9")


def build_modules_in_docker(
    builds: Sequence[tuple[str, str]],
    *,
    output_dir: str | Path,
    builder_image: str | None = None,
    timeout: float = 1800.0,
) -> Path:
    """Build one or more Logos module flakes inside docker and return the
    host-side modules dir, ready to pass as
    `LogoscoreDockerDaemon(modules_dir=...)`.

    Why this exists: a module compiled on your host (macOS dylib,
    Linux-with-different-glibc, etc.) often won't load inside the
    daemon container. Building inside docker via the same base image
    guarantees ABI compatibility — same glibc, same Qt, same OpenSSL.
    Same approach the smoke image's stage-1 already uses for the
    daemon binary itself.

    `builds` is a list of `(flake_ref, attr)` tuples. **All builds
    share the same nix store inside one container run**, so common
    dependencies (logos-cpp-sdk, Qt, boost, openssl) get fetched once.
    Time saved is roughly proportional to N (number of modules) for
    typical Logos modules. For a single module, pass a one-item list.

    `flake_ref` is anything `nix build` accepts:
      * `"github:logos-co/logos-test-modules"`
      * `"github:user/my-module/branch"`
      * `"path:./my-module"` (mounted into the container)

    `attr` is the flake-output path that produces a derivation whose
    `$out/modules/<name>/...` matches what the daemon's `-m` flag
    expects. The standard logos-module-builder `.install-portable`
    output produces this layout. Examples:
      * `"modules.x86_64-linux.test_basic_module.install-portable"`
      * `"packages.aarch64-linux.install-portable"`

    `output_dir` is a host directory that'll receive the merged
    `modules/<name>/<plugin>.so + manifest.json` trees from every
    build. Created if missing. The returned `Path` is `output_dir`.

    Typical use:

        modules_dir = build_modules_in_docker(
            builds=[
                ("github:user/my-module",  "packages.x86_64-linux.install-portable"),
                ("github:user/my-module2", "packages.x86_64-linux.install-portable"),
            ],
            output_dir="./build/modules",
        )
        with LogoscoreDockerDaemon(
            image="logoscore:smoke-portable",
            modules_dir=modules_dir,
        ) as daemon:
            ...

    Raises LogoscoreError on docker / nix build failure (the offending
    flake_ref#attr is included in the message).
    """
    if not builds:
        raise ValueError("build_modules_in_docker requires at least one build")

    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    image = builder_image or _BUILDER_IMAGE

    # Pass build pairs through an env var, one per line: "<flake>\t<attr>".
    # Tab is safe — neither flake refs nor attr paths contain it. Newline
    # separator avoids quoting issues that would arise from passing as
    # positional args through `sh -c`.
    builds_env = "\n".join(f"{flake}\t{attr}" for (flake, attr) in builds)

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{out}:/out",
        "-e", f"BUILDS={builds_env}",
        image,
        "sh", "-c",
        # In-container build script. Notes:
        # 1) `sandbox = false` + `filter-syscalls = false` because Docker
        #    Desktop's seccomp + Rosetta layer (on Apple Silicon) blocks
        #    the BPF filters nix's sandbox installs. The outer docker
        #    layer already isolates the build.
        # 2) Each build gets its own /tmp/result-N out-link to avoid
        #    nix complaining about an existing link, then they're merged
        #    into /out together at the end.
        # 3) `tar c | tar x` so symlinks into /nix/store (which the host
        #    won't have) become regular files in /out, and we DON'T try
        #    to preserve the read-only nix-store permissions on the
        #    target — Docker bind-mounts on macOS reject chmod on
        #    host-owned paths and `cp -rL` would otherwise abort.
        'set -e; mkdir -p /etc/nix; '
        '{ echo "experimental-features = nix-command flakes"; '
        '  echo "sandbox = false"; '
        '  echo "filter-syscalls = false"; } > /etc/nix/nix.conf; '
        'i=0; '
        # Read the BUILDS env line-by-line. printf instead of echo so
        # we don\'t depend on echo -e behaviour.
        'printf "%s\\n" "$BUILDS" | while IFS="\t" read -r flake attr; do '
        '  [ -n "$flake" ] || continue; '
        '  echo "[$i] building $flake#$attr"; '
        '  nix build -L "$flake#$attr" --out-link "/tmp/result-$i" --refresh; '
        '  if [ ! -d "/tmp/result-$i/modules" ]; then '
        '    echo "ERROR: $flake#$attr has no modules/ subdir" >&2; '
        '    ls -la "/tmp/result-$i/" >&2; exit 1; fi; '
        # Plain `cp` from /nix/store inherits the source\'s read-only
        # permissions. tar would then fail to overwrite on the next
        # iteration, and Docker Desktop bind mounts on macOS reject
        # `chmod` from the container (the dest is host-owned) so we
        # can\'t un-readonly after the fact. Workaround: use `find +
        # cat + install -m` which writes EVERY file with explicit perms,
        # bypassing tar/cp\'s preserve-perms logic entirely.
        '  cd "/tmp/result-$i/modules" && '
        '    find . -type d | while read -r d; do mkdir -p "/out/$d"; done && '
        '    find . -type f | while read -r f; do '
        '      install -m 644 "$f" "/out/$f"; done && '
        '  cd -; '
        '  i=$((i+1)); '
        'done',
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise LogoscoreError(
            f"Module build failed (exit {r.returncode}):\n"
            f"  builds: {builds}\n"
            f"  image:  {image}\n"
            f"  stderr: {r.stderr.strip()}"
        )
    return out


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
        transport: str = "tcp",
        ssl_cert: str | Path | None = None,
        ssl_key: str | Path | None = None,
        container_name: str | None = None,
        extra_module_dirs: Sequence[str] | None = None,
        extra_args: Sequence[str] | None = None,
        startup_timeout: float = 20.0,
    ) -> None:
        if transport not in ("tcp", "tcp_ssl"):
            raise ValueError(
                f"transport must be 'tcp' or 'tcp_ssl' (got {transport!r})"
            )
        if transport == "tcp_ssl" and not (ssl_cert and ssl_key):
            raise ValueError(
                "transport='tcp_ssl' requires ssl_cert and ssl_key"
            )

        self.image = image
        self.modules_dir = Path(modules_dir)
        self.codec = codec
        self.transport = transport
        self.ssl_cert = Path(ssl_cert) if ssl_cert else None
        self.ssl_key = Path(ssl_key) if ssl_key else None
        self.startup_timeout = startup_timeout
        # Additional dirs *inside the container* to scan for modules, on
        # top of `/opt/logoscore/modules` (CLI's built-in) and
        # `/user-modules` (the host `modules_dir` bind-mount). For most
        # callers empty.
        self.extra_module_dirs = list(extra_module_dirs or [])
        self.extra_args = list(extra_args or [])

        # Host-side dirs: either caller-supplied (persistent across
        # runs — useful for session restore) or freshly-minted tmpdirs
        # we own and clean up on stop(). For caller-supplied dirs we
        # mkdir(parents=True, exist_ok=True) before docker can bind-mount
        # them: a missing host path under `docker -v host:/container`
        # gets auto-created by the daemon with root ownership, which
        # then breaks reads/cleanup from the unprivileged caller.
        self._owns_config_dir = config_dir is None
        if config_dir is None:
            self._config_dir = Path(
                tempfile.mkdtemp(prefix="logoscore-docker-cfg-"))
        else:
            self._config_dir = Path(config_dir)
            self._config_dir.mkdir(parents=True, exist_ok=True)
        self._owns_persistence_dir = persistence_dir is None
        if persistence_dir is None:
            self._persistence_dir = Path(
                tempfile.mkdtemp(prefix="logoscore-docker-pers-"))
        else:
            self._persistence_dir = Path(persistence_dir)
            self._persistence_dir.mkdir(parents=True, exist_ok=True)

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

        # Volume mounts common to both transports.
        volumes = [
            "-v", f"{self._config_dir}:/config",
            "-v", f"{self._persistence_dir}:/persistence",
            "-v", f"{self.modules_dir}:/user-modules:ro",
        ]
        # For tcp_ssl, also bind-mount the cert+key into /certs:ro.
        # They're exposed read-only because the daemon only reads them.
        if self.transport == "tcp_ssl":
            # Mount each cert file's parent as /certs would be wrong if
            # cert and key live in different dirs — mount them as
            # individual files to avoid that pitfall. Docker supports
            # file-level bind mounts natively.
            volumes += [
                "-v", f"{self.ssl_cert}:/certs/cert.pem:ro",
                "-v", f"{self.ssl_key}:/certs/key.pem:ro",
            ]

        # Transport-specific daemon flags. We always *also* advertise
        # `local` because module-to-module traffic inside the daemon's
        # process group still uses it (the module-host spawns inherit
        # the daemon's transport config; if tcp_ssl were the only
        # option, they'd try to bind TLS with no cert and abort).
        # `local` satisfies that; the network listener is separate.
        transport_flags: list[str] = ["--transport", "local"]
        if self.transport == "tcp":
            transport_flags += [
                "--transport", "tcp",
                "--tcp-host", "0.0.0.0",
                "--tcp-port", str(CONTAINER_TCP_PORT),
                "--tcp-codec", self.codec,
            ]
        else:  # tcp_ssl
            transport_flags += [
                "--transport", "tcp_ssl",
                "--tcp-ssl-host", "0.0.0.0",
                "--tcp-ssl-port", str(CONTAINER_TCP_PORT),
                "--tcp-ssl-codec", self.codec,
                "--ssl-cert", "/certs/cert.pem",
                "--ssl-key", "/certs/key.pem",
            ]

        cmd: list[str] = [
            "docker", "run", "--rm", "-d",
            "--name", self._container_name,
            # host_port (dynamic) → CONTAINER_TCP_PORT (fixed). See the
            # module docstring for why we don't bind the same port on
            # both sides.
            "-p", f"{self._host_port}:{CONTAINER_TCP_PORT}",
            *volumes,
            self.image,
            "daemon",
            "--config-dir", "/config",
            "--persistence-path", "/persistence",
            *transport_flags,
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
        no_verify_peer: bool | None = None,
    ) -> LogoscoreClient:
        """Build a LogoscoreClient wired to dial this daemon.

        `binary` is the host-side `logoscore` executable — the client
        shells out to it for some operations (e.g. `watch`). Defaults
        to whatever `logoscore` resolves to on PATH.

        `tcp_host` defaults to localhost because the container's port
        is published there. Override for remote-docker setups.

        `no_verify_peer`: for `tcp_ssl` daemons this defaults to True so
        self-signed certs work out of the box (the common case for smoke
        tests). Set to False to exercise the verification path once
        you're feeding the client a real CA. Ignored when transport is
        plain `tcp`.
        """
        if self._container_id is None:
            raise LogoscoreError(
                "daemon is not running — call start() or use the context manager"
            )
        if no_verify_peer is None:
            no_verify_peer = (self.transport == "tcp_ssl")
        return LogoscoreClient(
            binary=binary,
            config_dir=self._config_dir,
            timeout=timeout,
            transport=self.transport,
            tcp_host=tcp_host,
            tcp_port=self.host_port,   # <-- the critical override
            codec=codec or self.codec,
            no_verify_peer=no_verify_peer,
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
