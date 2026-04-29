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

# Fixed TCP ports the daemon binds *inside* the container. The host
# side always uses dynamically-picked ephemeral ports and port-forwards
# them in. See module docstring for the full rationale.
#
# core_service is on CONTAINER_TCP_PORT; capability_module on
# CONTAINER_CAP_TCP_PORT. The latter is needed because the SDK's
# auto-`requestModule` path inside LogosAPIClient dials capability_module
# transparently — without forwarding it through, every host-side RPC
# would either fail (post-config-split) or hit a 20s waitForSource
# timeout (pre-fix). Stable, distinct container ports lets us
# `docker run -p host_core:6000 -p host_cap:6001 ...` and keep the
# host-side mapping deterministic.
CONTAINER_TCP_PORT     = 6000
CONTAINER_CAP_TCP_PORT = 6001


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

    `flake_ref` is any non-local reference `nix build` accepts inside
    the container — e.g. github URIs:
      * `"github:logos-co/logos-test-modules"`
      * `"github:user/my-module/branch"`

    Local `path:` flake references are NOT supported by this helper —
    the build runs inside a one-shot `nixos/nix` container and the
    host filesystem isn't bind-mounted in. For local iteration on an
    unpushed branch, push to a fork and reference it via `github:...`,
    or build outside this helper (e.g. `nix build .#install-portable`)
    and pass the resulting `result/modules` directly to
    `LogoscoreDockerDaemon(modules_dir=...)`.

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
        # 3) Walk + `install -m 644` (NOT `tar` or `cp -rL`) so symlinks
        #    into /nix/store (which the host won't have) become regular
        #    files in /out, and every file is written with explicit
        #    rw-perms — Docker bind-mounts on macOS reject post-write
        #    chmod from the container, and `cp -rL` would inherit the
        #    nix-store's read-only perms which then fail tar/copy on
        #    the next iteration. See the in-script comment for detail.
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
        # Validate up front. `docker run -v <missing-host-path>:...`
        # silently auto-creates the host path with root ownership,
        # which both pollutes the caller's filesystem and produces a
        # confusing "modules dir is empty" failure later. Catch the
        # typo at construction.
        if not self.modules_dir.exists():
            raise FileNotFoundError(
                f"modules_dir does not exist: {self.modules_dir}. "
                "Build your module(s) first (e.g. `nix build .#install` "
                "or via build_modules_in_docker())."
            )
        if not self.modules_dir.is_dir():
            raise NotADirectoryError(
                f"modules_dir is not a directory: {self.modules_dir}"
            )
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

        # Host-only client config dir. The daemon's view of /config
        # (and the LogoscoreClient's `config_dir` argument when this
        # daemon hands one out) are NOT the same on disk — the
        # container writes /config/{daemon,client}/* as root, and the
        # host process can't overwrite root-owned files in there. The
        # client side gets its own dir which the host populates with
        # client.json (host-correct ports) + a copy of the daemon's
        # raw auto-token. Cleaned up on stop() alongside _config_dir.
        self._host_client_dir = Path(
            tempfile.mkdtemp(prefix="logoscore-docker-client-"))

        self._host_port = host_port  # may be None until start()
        # Capability_module's host-side port. Picked alongside
        # `_host_port` in start() so the container's stable
        # CONTAINER_CAP_TCP_PORT can be forwarded to a known host port.
        # Tracked separately so the post-startup client.json rewrite
        # (see _rewrite_client_json) knows what to point the host
        # client at for capability_module.
        self._host_cap_port: int | None = None
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
        # Capability_module rides its own host:container port pair.
        # Pick eagerly here so the docker `-p` mapping (and the
        # daemon's `--module-tcp-port` flag below) line up.
        if self._host_cap_port is None:
            self._host_cap_port = pick_free_port()

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

        # Per-module transport flags. The daemon takes
        # `--module-transport NAME=PROTOCOL[,k=v...]` and uses each
        # module's set independently — there's no implicit
        # core_service/capability_module inheritance any more. We
        # configure both modules explicitly with a `local` listener
        # plus one network listener, and pin distinct container ports
        # so the host-side `-p` mappings stay deterministic.
        #
        # core_service rides CONTAINER_TCP_PORT, capability_module
        # rides CONTAINER_CAP_TCP_PORT. The `local` listener satisfies
        # in-process module-to-module traffic (module-host spawns
        # inherit the daemon's transport config; if tcp_ssl were the
        # only option, they'd try to bind TLS with no cert and abort).
        if self.transport == "tcp":
            # `--insecure-tcp` is the daemon's explicit opt-in for
            # binding plaintext tcp on a non-loopback host. The whole
            # docker setup *is* a non-loopback bind by design (0.0.0.0
            # with port-forwarded host:container access), so the
            # daemon's safety net legitimately needs the override here.
            # tcp_ssl below doesn't trip the guard — SSL is exactly
            # the production-shaped alternative the guard recommends.
            transport_flags = [
                "--module-transport", "core_service=local",
                "--module-transport",
                f"core_service=tcp,host=0.0.0.0,port={CONTAINER_TCP_PORT},codec={self.codec}",
                "--module-transport", "capability_module=local",
                "--module-transport",
                f"capability_module=tcp,host=0.0.0.0,port={CONTAINER_CAP_TCP_PORT},codec={self.codec}",
                "--insecure-tcp",
            ]
        else:  # tcp_ssl
            transport_flags = [
                "--module-transport", "core_service=local",
                "--module-transport",
                f"core_service=tcp_ssl,host=0.0.0.0,port={CONTAINER_TCP_PORT}"
                f",codec={self.codec},cert=/certs/cert.pem,key=/certs/key.pem",
                "--module-transport", "capability_module=local",
                "--module-transport",
                f"capability_module=tcp_ssl,host=0.0.0.0,port={CONTAINER_CAP_TCP_PORT}"
                f",codec={self.codec},cert=/certs/cert.pem,key=/certs/key.pem",
            ]

        # Note: deliberately no --rm. If the daemon exits during startup
        # (e.g. CLI11 rejects an unknown flag, port bind fails), --rm
        # would auto-remove the container before _capture_logs gets a
        # chance to read it — the on-fail diagnostic would just say
        # "No such container". stop() below explicitly does
        # `docker rm -f`, so we don't leak containers either.
        cmd: list[str] = [
            "docker", "run", "-d",
            "--name", self._container_name,
            # Two host:container port mappings. core_service binds
            # CONTAINER_TCP_PORT inside the container; capability_module
            # binds CONTAINER_CAP_TCP_PORT. Each is forwarded to its
            # own dynamically-picked host port. See the module
            # docstring for the rationale.
            "-p", f"{self._host_port}:{CONTAINER_TCP_PORT}",
            "-p", f"{self._host_cap_port}:{CONTAINER_CAP_TCP_PORT}",
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

        # Build a host-side client config (separate from the
        # daemon's bind-mounted /config — that one's owned by root
        # because the container ran as root). Writes
        # `<host_client_dir>/client/client.json` (host-correct ports)
        # and `<host_client_dir>/client/auto.json` (the raw token,
        # copied out of the daemon's /config/daemon/tokens). The
        # `client(...)` factory below points the LogoscoreClient at
        # `host_client_dir` so it reads from this host-owned tree
        # instead of the container-owned bind-mount.
        self._build_host_client_config()

        return self

    def _build_host_client_config(self) -> None:
        """Populate the host-only client config dir with a client.json
        that points at the host-side forwarded ports + a copy of the
        daemon-emitted raw auto token. Called once after the daemon
        has come up and published daemon.json."""
        import json as _json

        # The daemon emitted /config/daemon/{daemon.json,tokens/auto.json}
        # as root inside the container, with restrictive perms
        # (daemon/tokens is 0700, the token file 0600). The host-side
        # process can't `os.stat()` into those directly. We need two
        # bytes of content from there: the daemon's instance_id (for
        # the LocalSocket dial path, even though we use TCP for the
        # network listener) and the raw auto-token.
        #
        # Don't widen `/config` permissions on disk: that would leave
        # the entire daemon/tokens/ directory (any operator-issued
        # tokens included) world-readable on the host filesystem for
        # the lifetime of the container's bind-mount, surviving the
        # daemon if it leaves anything behind. Instead, pipe the two
        # files we need through `docker exec ... cat` — the container
        # is root inside, can read its own files, and we capture the
        # content on stdout without touching disk permissions.
        if self._container_id is None:
            return

        def _container_read(path: str) -> str | None:
            r = subprocess.run(
                ["docker", "exec", self._container_id, "cat", path],
                capture_output=True, text=True,
            )
            return r.stdout if r.returncode == 0 else None

        daemon_state_text = _container_read("/config/daemon/daemon.json")
        if daemon_state_text is None:
            return
        try:
            daemon_state = _json.loads(daemon_state_text)
        except _json.JSONDecodeError:
            return

        instance_id = daemon_state.get("instance_id", "")

        # Each module dials localhost:<host_port> — the docker
        # forwarder routes those into the container's listeners.
        # Codec must match what the daemon actually advertised; we
        # trust self.codec because the docker run command pinned it.
        if self.transport == "tcp_ssl":
            transport_kind = "tcp_ssl"
            extra: dict = {"verify_peer": False}
        else:
            transport_kind = "tcp"
            extra = {}

        client_cfg = {
            "version": 1,
            "token_file": "auto.json",
            "instance_id": instance_id,
            "daemon": {
                "core_service": {
                    "transport": transport_kind,
                    "host":      "localhost",
                    "port":      self._host_port,
                    "codec":     self.codec,
                    **extra,
                },
                "capability_module": {
                    "transport": transport_kind,
                    "host":      "localhost",
                    "port":      self._host_cap_port,
                    "codec":     self.codec,
                    **extra,
                },
            },
        }
        client_dir = self._host_client_dir / "client"
        client_dir.mkdir(parents=True, exist_ok=True)
        (client_dir / "client.json").write_text(
            _json.dumps(client_cfg, indent=4) + "\n")

        # Pipe the auto token's content out of the container the same
        # way we did daemon.json above — the bind-mounted file on
        # disk stays mode 0600 root-owned, but we get its bytes.
        token_text = _container_read("/config/daemon/tokens/auto.json")
        if token_text is None:
            return
        (client_dir / "auto.json").write_text(token_text)

    def stop(self) -> None:
        """Kill the container. Idempotent; safe to call even if start()
        never succeeded."""
        if self._container_id is not None:
            # Mirror the daemon's container logs to the parent's stderr
            # before tearing down — symmetric with _proc.py's CLI
            # forwarding, so a single env flag dumps both sides of the
            # CLI ↔ daemon conversation.
            if os.environ.get("LOGOSCORE_PY_FORWARD_OUTPUT", "").lower() in (
                "1", "true", "yes", "on",
            ):
                logs = self._capture_logs()
                header = f"[logoscore-py docker-daemon {self._container_name}]"
                import sys
                print(header, file=sys.stderr, flush=True)
                for line in logs.splitlines():
                    print(f"{header} {line}", file=sys.stderr, flush=True)
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
        # The host-only client dir is always self-owned.
        if self._host_client_dir.exists():
            shutil.rmtree(self._host_client_dir, ignore_errors=True)

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
        # Point the client at the host-only client dir, NOT the
        # bind-mounted /config (which is owned by root because the
        # container ran as root). _build_host_client_config() above
        # populated the host dir with a client.json keyed at the
        # forwarded host ports + a copy of the auto token.
        return LogoscoreClient(
            binary=binary,
            config_dir=self._host_client_dir,
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
        conn_file = self._config_dir / "daemon" / "daemon.json"
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
