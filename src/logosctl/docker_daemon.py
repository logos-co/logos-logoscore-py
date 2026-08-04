"""Lifecycle manager for a `logosctl` daemon running inside docker.

`LogosctlDockerDaemon` is to `LogosctlDaemon` what its name suggests:
the same context-manager shape, but the daemon runs in a container and
speaks TCP to the host. Use it when your test setup deliberately
crosses a container boundary — e.g. you want to smoke-test a real
distribution of logosctl, or you need the daemon to be reachable from
multiple processes on the host.

Example:
    from logosctl import LogosctlDockerDaemon

    with LogosctlDockerDaemon(
        image="logosctl:smoke-portable",
        modules_dir="./my-module/result/modules",
    ) as daemon:
        client = daemon.client(binary="./logosctl")
        client.load_module("my_module")
        print(client.call("my_module", "do_something", 42))

Volume layout inside the container (all three dirs are on the host and
bind-mounted in — they survive the container):
    /config       — the session directory. Holds `daemon.yaml` (the
                    config document this wrapper writes) plus everything
                    logosctl puts under a session: `daemon/config.yaml`,
                    `daemon/state.json`, `daemon/tokens/<name>.json`,
                    `client/`, `logs/`, `modules/`, `plugins/`. The
                    host-side client config is built from the forwarded
                    ports plus the daemon's own auto token.
    /persistence  — the `persistence_path` config key; pre-seed to
                    restore a session, read back to inspect what modules
                    wrote
    /user-modules — compiled Qt plugins, mounted read-only; reached by
                    the daemon through `modules_dirs`

Configuration is a document, not flags:
    logosctl has no `-m`, `--persistence-path`, `--module-transport` or
    `--insecure-tcp` — every one of them was deleted, and passing one is
    a parse error that stops the daemon before it starts. All of it is
    now a YAML document installed into the session BEFORE the daemon
    boots. So `start()` runs two containers over the same `/config`
    bind-mount: a throwaway `daemon config set /config/daemon.yaml`,
    then the real `daemon start`. Going through the CLI rather than
    dropping the file straight into `/config/daemon/config.yaml` is what
    buys the top-level-key allowlist — a near-miss like `insecureTcp`
    comes back as an error naming the key instead of being silently
    dropped and leaving the daemon to boot without the operator's intent.

Port strategy (status-go `tests-functional` pattern):
    container-internal TCP ports are fixed: `core_service` on
    `CONTAINER_TCP_PORT` (6000), `capability_module` on
    `CONTAINER_CAP_TCP_PORT` (6001). The host maps an ephemeral port to
    each via `-p …:6000` / `-p …:6001`. The client dials those forwarded
    host ports from a per-module `client/config.yaml` written by
    `LogosctlClient.write_config` — one entry per module, each with its
    own port. There is no env-var shortcut to reach for here even if we
    wanted one: `LOGOSCTL_CONFIG_DIR` and `LOGOSCTL_TOKEN` are the only
    variables the binary reads, and the dial spec is a file.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .client import DaemonEndpoint, LogosctlClient
# The daemon document has one shape and one set of hazards whether the
# daemon runs here or in a container, so its emitter and its type checks
# live once, next to the flavor that came first.
from .daemon import _check_config_types, _yaml_document
from .errors import LogosctlError


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

# Paths *inside* the container. The first three are bind-mount targets we
# choose; the last three are properties of the image. The daemon config
# document is written in these terms, so every path in it has to be the
# container's, never the host's.
CONTAINER_CONFIG_DIR       = "/config"
CONTAINER_PERSISTENCE_DIR  = "/persistence"
CONTAINER_USER_MODULES_DIR = "/user-modules"
# The image's own modules dir — capability_module et al. The portable
# bundle also finds it on its own (the daemon adds `<bin>/../modules`
# unconditionally), but naming it keeps the two image flavors symmetric
# and the config document self-describing.
CONTAINER_BUNDLED_MODULES_DIR = "/opt/logosctl/modules"
CONTAINER_CERT_PATH = "/certs/cert.pem"
CONTAINER_KEY_PATH  = "/certs/key.pem"
# The document `daemon config set` reads. Written by the host into the
# root of the bind-mounted session dir, deliberately NOT at
# `daemon/config.yaml` — that path is the CLI's to write, and this is
# only the input it writes it from.
CONTAINER_CONFIG_DOC = f"{CONTAINER_CONFIG_DIR}/daemon.yaml"

# Every logosctl invocation in the container selects its session this way
# rather than with `--config-dir`. The flag is app-level, so it only
# parses before the subcommand, and `daemon config set FILE` reaches its
# command object through `remaining()` — where a trailing `--config-dir
# DIR` would arrive as two more positional arguments. The env var has no
# ordering rule to get wrong, and it's the same variable `_proc` uses for
# the host-side client.
_CONFIG_DIR_ENV = ["-e", f"LOGOSCTL_CONFIG_DIR={CONTAINER_CONFIG_DIR}"]


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
_BUILDER_IMAGE = os.environ.get("LOGOSCTL_BUILDER_IMAGE", "nixos/nix:2.24.9")


def build_modules_in_docker(
    builds: Sequence[tuple[str, str]],
    *,
    output_dir: str | Path,
    builder_image: str | None = None,
    timeout: float = 1800.0,
) -> Path:
    """Build one or more Logos module flakes inside docker and return the
    host-side modules dir, ready to pass as
    `LogosctlDockerDaemon(modules_dir=...)`.

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
    `LogosctlDockerDaemon(modules_dir=...)`.

    `attr` is the flake-output path that produces a derivation whose
    `$out/modules/<name>/...` matches what the daemon's `modules_dirs`
    config key expects. The standard logos-module-builder
    `.install-portable` output produces this layout. Examples:
      * `"modules.x86_64-linux.test_fullapi_cpp.install-portable"`
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
        with LogosctlDockerDaemon(
            image="logosctl:smoke-portable",
            modules_dir=modules_dir,
        ) as daemon:
            ...

    Raises LogosctlError on docker / nix build failure (the offending
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
        raise LogosctlError(
            f"Module build failed (exit {r.returncode}):\n"
            f"  builds: {builds}\n"
            f"  image:  {image}\n"
            f"  stderr: {r.stderr.strip()}"
        )
    return out


# ── The helper ────────────────────────────────────────────────────────────

class LogosctlDockerDaemon:
    """Spawn a logosctl daemon inside a docker container and drive it
    from the host over TCP.

    Construction stores config only. `start()` installs the session's
    daemon config, runs the container, and waits for `state.json`;
    `stop()` kills it. Use the context-manager form to get start/stop
    bracketing automatically.
    """

    def __init__(
        self,
        *,
        image: str,
        modules_dir: str | Path,
        # Optional bits — sane defaults mean you can do
        # `LogosctlDockerDaemon(image=..., modules_dir=...)`.
        config_dir: str | Path | None = None,
        persistence_dir: str | Path | None = None,
        host_port: int | None = None,
        codec: str = "json",
        transport: str = "tcp",
        ssl_cert: str | Path | None = None,
        ssl_key: str | Path | None = None,
        # HOST-side path to the CA that signs `ssl_cert`. Unlike the cert
        # and key — which are the daemon's and get bind-mounted into the
        # container — this one is read by the client, which runs on the
        # host, so it is never mounted anywhere. For the usual
        # self-signed smoke cert the cert IS its own CA, so passing
        # `ssl_ca=ssl_cert` is what makes `verify_peer=True` work at all;
        # without a CA a verifying handshake fails closed.
        ssl_ca: str | Path | None = None,
        # On-disk dial-spec value for `verify_peer` in the host client/
        # config.yaml. True = verify the daemon's cert against `ssl_ca`
        # (correct default; what a real deployment with a CA-issued
        # cert would use). False = skip verification (the smoke-test
        # default — the caller's `ssl_cert` is typically self-signed).
        # Independent of the per-call `no_verify_peer` knob in
        # `client()`, which rewrites this value on disk: logosctl has no
        # client-side flags or env vars, so the file is the only place
        # either of them can land.
        verify_peer: bool = False,
        container_name: str | None = None,
        # Name of an EXISTING docker network to attach the container to.
        # Caller-managed: the daemon never creates or removes networks.
        # Use to make multiple daemon containers discover each other by
        # container name via docker's embedded DNS.
        network: str | None = None,
        extra_module_dirs: Sequence[str] | None = None,
        # Extra top-level keys merged into the daemon config document —
        # `access_group`, `dirs`, `logging`, `access_policy`, … This is
        # where logoscore's free-form `extra_args` went: every daemon
        # knob that used to be a flag is a config key now, so an argv
        # escape hatch could no longer express any of them. Merged last,
        # so a caller can also override what this wrapper computes. Keys
        # are allowlisted by the CLI; an unknown one fails `config set`
        # with a message naming it.
        extra_config: Mapping[str, Any] | None = None,
        # Still argv, but only the app-level flags are left (`--verbose`,
        # `--quiet`) — everything that configured the daemon moved into
        # `extra_config`.
        extra_args: Sequence[str] | None = None,
        # 20s was already generous for logoscore; keep it, because a
        # logosctl daemon does strictly more before it writes state.json:
        # it creates the session's modules/plugins/keyring/cache dirs and
        # loads package_manager + package_downloader unconditionally.
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
        self.ssl_ca = Path(ssl_ca) if ssl_ca else None
        self.verify_peer = verify_peer
        self.startup_timeout = startup_timeout
        # Additional dirs *inside the container* to scan for modules, on
        # top of the image's own bundled modules and `/user-modules`
        # (the host `modules_dir` bind-mount). For most callers empty.
        self.extra_module_dirs = list(extra_module_dirs or [])
        self.extra_config = dict(extra_config or {})
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
                tempfile.mkdtemp(prefix="logosctl-docker-cfg-"))
        else:
            self._config_dir = Path(config_dir)
            self._config_dir.mkdir(parents=True, exist_ok=True)
        self._owns_persistence_dir = persistence_dir is None
        if persistence_dir is None:
            self._persistence_dir = Path(
                tempfile.mkdtemp(prefix="logosctl-docker-pers-"))
        else:
            self._persistence_dir = Path(persistence_dir)
            self._persistence_dir.mkdir(parents=True, exist_ok=True)

        # Host-only client config dir. The daemon's view of /config
        # (and the LogosctlClient's `config_dir` argument when this
        # daemon hands one out) are NOT the same on disk — the
        # container writes /config/{daemon,client}/* as root, and the
        # host process can't overwrite root-owned files in there. The
        # client side gets its own dir which the host populates with
        # client/config.yaml (host-correct ports) + a copy of the
        # daemon's raw auto-token. Keeping the two apart is also what
        # the CLI wants: a daemon rewrites `client/config.yaml` in its
        # OWN session on every boot, so a dial spec written into
        # /config would be refreshed out from under us. Cleaned up on
        # stop() alongside _config_dir.
        self._host_client_dir = Path(
            tempfile.mkdtemp(prefix="logosctl-docker-client-"))

        self._host_port = host_port  # may be None until start()
        # Capability_module's host-side port. Picked alongside
        # `_host_port` in start() so the container's stable
        # CONTAINER_CAP_TCP_PORT can be forwarded to a known host port.
        # Tracked separately so the post-startup client/config.yaml
        # build (see `_build_host_client_config`) knows what to point
        # the host client at for capability_module.
        self._host_cap_port: int | None = None
        self._container_name = (
            container_name
            or f"logosctl-{uuid.uuid4().hex[:12]}"
        )
        self.network = network
        self._container_id: str | None = None

    # ── Public properties ───────────────────────────────────────────────

    @property
    def host_port(self) -> int:
        """Dynamic host port mapped to the container's TCP listener.
        Only valid once `start()` has completed."""
        if self._host_port is None:
            raise LogosctlError("daemon hasn't started yet")
        return self._host_port

    @property
    def config_dir(self) -> Path:
        """Host path of the daemon's session directory. The container
        runs as root and writes `daemon/config.yaml`, `daemon/state.json`,
        `daemon/tokens.json`, `daemon/tokens/<name>.json`,
        `client/config.yaml`, `client/auto.json` and `logs/` here as
        root-owned, with the credential-adjacent ones at 0600 (and
        `daemon/` itself locked to 0700 when an access group is set).
        The host process generally can't read those directly even though
        it owns the surrounding dir. Use `read_container_file()` (which
        goes through `docker exec ... cat`) or the higher-level helpers
        (`state_json`, `instance_id`, `daemon_log`) to extract content;
        reaching into this path with `read_text()` will hit a
        PermissionError."""
        return self._config_dir

    # ── Container-side reads ─────────────────────────────────────────────
    #
    # Anything the daemon writes inside the bind-mounted /config tree
    # is owned by root with restrictive perms. Don't widen those on
    # disk (that would leave the whole daemon/tokens/ dir
    # world-readable on the host for the lifetime of the bind-mount).
    # Instead, pipe content out via `docker exec ... cat` — the
    # container is root inside, can read its own files, and we capture
    # bytes on stdout without touching on-disk permissions.

    def read_container_file(self, container_path: str) -> str | None:
        """Read a file from inside the running container as root.
        Returns the file's text content, or None if the file is
        missing / the container isn't running / the read fails.

        Use this for any host-side inspection of files the daemon
        writes under `/config/` — the host process can't read them
        directly. Examples: `state.json` (instance_id, resolved
        transport endpoints), `tokens.json` (hashed token list),
        `tokens/<name>.json` (raw tokens, when needed for testing).
        `cat` follows symlinks, so `logs/daemon.log` — which is a link
        to this boot's timestamped log — reads as the live file."""
        if self._container_id is None:
            return None
        r = subprocess.run(
            ["docker", "exec", self._container_id, "cat", container_path],
            capture_output=True, text=True,
        )
        return r.stdout if r.returncode == 0 else None

    def container_file_exists(self, container_path: str) -> bool:
        """True iff `container_path` exists inside the running
        container. Used to poll for files the daemon emits as root,
        since the host-side `Path.exists()` can race the perms model
        on some filesystems and is harder to reason about than a
        direct `test -f` inside the container."""
        if self._container_id is None:
            return False
        r = subprocess.run(
            ["docker", "exec", self._container_id, "test", "-f", container_path],
            capture_output=True, text=True,
        )
        return r.returncode == 0

    def state_json(self) -> dict | None:
        """Parsed contents of `/config/daemon/state.json` (the live
        runtime state file). Returns `None` if the daemon hasn't
        produced it yet, the container isn't running, or the JSON is
        malformed — every call site handles these the same way
        (treat the daemon as not-yet-ready). Source of truth for
        `instance_id` and the actually-bound transport ports."""
        text = self.read_container_file(
            f"{CONTAINER_CONFIG_DIR}/daemon/state.json")
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def daemon_log(self) -> str | None:
        """The daemon's own log file, `/config/logs/daemon.log`.

        Distinct from `docker logs`, which only sees what the daemon
        wrote to the container's stdout. The two normally agree —
        logosctl's LogSink mirrors every byte back to the original
        stdout unless `logging.console` is turned off — but the file is
        the durable record: it survives `logging.console: false`, and
        it's the only copy of anything a module logged after the sink
        took over the process's stdout."""
        return self.read_container_file(
            f"{CONTAINER_CONFIG_DIR}/logs/daemon.log")

    @property
    def instance_id(self) -> str | None:
        """The daemon's 12-char instance ID, or None if state.json
        isn't readable yet. Convenience wrapper over `state_json()`
        for the most common access pattern."""
        s = self.state_json()
        return s.get("instance_id") if isinstance(s, dict) else None

    @property
    def persistence_dir(self) -> Path:
        """Host path of the daemon's persistence directory (the
        `persistence_path` config key). Pre-seed before `start()` to
        restore a session; read back after `stop()` to inspect state."""
        return self._persistence_dir

    @property
    def container_id(self) -> str:
        if self._container_id is None:
            raise LogosctlError("daemon hasn't started yet")
        return self._container_id

    @property
    def container_name(self) -> str:
        return self._container_name

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> "LogosctlDockerDaemon":
        """Install the session's daemon config, `docker run` the daemon,
        and block until it writes state.json.

        Raises LogosctlError on docker failure / bad config / startup
        timeout. Does NOT check `docker_available()` or `image_present()`
        up front — callers that care about environmental skips should do
        so before calling start().
        """
        if self._container_id is not None:
            raise LogosctlError("daemon is already started")

        if self._host_port is None:
            self._host_port = pick_free_port()
        # Capability_module rides its own host:container port pair.
        # Pick eagerly here so the docker `-p` mapping and the
        # listener the config document declares for capability_module
        # line up.
        if self._host_cap_port is None:
            self._host_cap_port = pick_free_port()

        # Pre-flight: catch a missing network with a readable error
        # rather than the raw `Error response from daemon: network NAME
        # not found.` that `docker run` would emit. Same idea as the
        # modules_dir validation in __init__ — fail fast with a hint.
        # Done before the config-set container so a typo in the network
        # name doesn't leave a half-configured session behind.
        if self.network:
            inspect = subprocess.run(
                ["docker", "network", "inspect", self.network],
                capture_output=True, text=True,
            )
            if inspect.returncode != 0:
                raise LogosctlError(
                    f"docker network {self.network!r} does not exist; "
                    "create it before starting the daemon "
                    f"(stderr: {inspect.stderr.strip()})"
                )

        # Everything logoscore passed as daemon flags is a document now,
        # and `daemon start` acts on whatever is already on disk — so the
        # config has to be installed into the session first, in its own
        # container over the same bind-mount.
        self._install_daemon_config()

        # Note: deliberately no --rm. If the daemon exits during startup
        # (e.g. a listener fails to bind, or the plaintext-TCP guard
        # refuses the config), --rm would auto-remove the container
        # before _capture_logs gets a chance to read it — the on-fail
        # diagnostic would just say "No such container". stop() below
        # explicitly does `docker rm -f`, so we don't leak containers
        # either.
        cmd: list[str] = [
            "docker", "run", "-d",
            "--name", self._container_name,
            # Attach to a caller-managed docker network so multiple
            # daemon containers can discover each other by name via
            # docker's embedded DNS. No-op when network is None — the
            # splat injects nothing and `docker run` uses the default
            # bridge, byte-equivalent to the pre-feature command.
            *(["--network", self.network] if self.network else []),
            # Two host:container port mappings. core_service binds
            # CONTAINER_TCP_PORT inside the container; capability_module
            # binds CONTAINER_CAP_TCP_PORT. Each is forwarded to its
            # own dynamically-picked host port. See the module
            # docstring for the rationale.
            "-p", f"{self._host_port}:{CONTAINER_TCP_PORT}",
            "-p", f"{self._host_cap_port}:{CONTAINER_CAP_TCP_PORT}",
            *self._volume_args(),
            *_CONFIG_DIR_ENV,
            self.image,
            # Foreground, NOT `daemon start --detach`. Detaching forks a
            # setsid'd child and returns, which in a container means PID
            # 1 exits and takes the whole container (child included) with
            # it. `docker run -d` is the detaching layer here.
            "daemon", "start",
            *self.extra_args,
        ]

        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise LogosctlError(
                f"docker run failed (exit {r.returncode}):\n"
                f"  stderr: {r.stderr.strip()}\n"
                f"  cmd: {' '.join(cmd)}"
            )
        self._container_id = r.stdout.strip()

        if not self._wait_for_conn_file():
            # Capture the container's output before tearing down —
            # otherwise `docker rm -f` takes it with it and debugging is
            # blind. The daemon's own log file goes with the container
            # too, so grab it while it's still readable.
            logs = self._capture_logs()
            daemon_log = self.daemon_log()
            self.stop()
            sections = [
                f"daemon never wrote state.json within "
                f"{self.startup_timeout}s. Container logs:\n{logs}"
            ]
            if daemon_log and daemon_log.strip():
                sections.append(
                    f"--- {CONTAINER_CONFIG_DIR}/logs/daemon.log ---\n"
                    + daemon_log.strip())
            raise LogosctlError("\n".join(sections))

        # Build a host-side client config (separate from the
        # daemon's bind-mounted /config — that one's owned by root
        # because the container ran as root, and the daemon rewrites
        # its own client/config.yaml at every boot anyway). Writes
        # `<host_client_dir>/client/config.yaml` (host-correct ports)
        # and `<host_client_dir>/client/auto.json` (the raw token,
        # copied out of the daemon's session). The `client(...)` factory
        # below points the LogosctlClient at `host_client_dir` so it
        # reads from this host-owned tree instead of the container-owned
        # bind-mount. Tear the container down if seeding fails (e.g. the
        # auto token never showed up) so a failed start() doesn't leak a
        # running container.
        try:
            self._build_host_client_config()
        except Exception:
            self.stop()
            raise

        return self

    def _volume_args(self) -> list[str]:
        """Bind mounts common to both transports, plus the cert pair for
        tcp_ssl."""
        volumes = [
            "-v", f"{self._config_dir}:{CONTAINER_CONFIG_DIR}",
            "-v", f"{self._persistence_dir}:{CONTAINER_PERSISTENCE_DIR}",
            "-v", f"{self.modules_dir}:{CONTAINER_USER_MODULES_DIR}:ro",
        ]
        # For tcp_ssl, also bind-mount the cert+key into /certs:ro.
        # They're exposed read-only because the daemon only reads them.
        if self.transport == "tcp_ssl":
            # Mounting each cert file's parent as /certs would be wrong
            # if cert and key live in different dirs — mount them as
            # individual files to avoid that pitfall. Docker supports
            # file-level bind mounts natively.
            volumes += [
                "-v", f"{self.ssl_cert}:{CONTAINER_CERT_PATH}:ro",
                "-v", f"{self.ssl_key}:{CONTAINER_KEY_PATH}:ro",
            ]
        return volumes

    def _daemon_config_document(self) -> dict:
        """The daemon config document — the same one `LogosctlDaemon`
        builds, except every path in it is the container's.

        That is the one thing to be careful about here: `modules_dirs`,
        `persistence_path` and the listeners' `cert`/`key` are read
        inside the container, so a host path in any of them names a file
        that isn't there (or, worse, a directory the daemon will happily
        create and find empty).
        """
        doc: dict = {
            # Replaces every `-m` / `--modules-dir`. The image's own
            # modules first (capability_module and friends), then the
            # user's bind-mount, then anything the caller added.
            "modules_dirs": [
                CONTAINER_BUNDLED_MODULES_DIR,
                CONTAINER_USER_MODULES_DIR,
                *self.extra_module_dirs,
            ],
            # Replaces `--persistence-path`. `dirs: {data: …}` is the
            # newer spelling for the same thing and wins if both are
            # given; one is enough.
            "persistence_path": CONTAINER_PERSISTENCE_DIR,
            "modules": {
                "core_service": [self._listener(CONTAINER_TCP_PORT)],
                "capability_module": [
                    self._listener(CONTAINER_CAP_TCP_PORT)],
            },
        }
        if self.transport == "tcp":
            # The daemon refuses to bind plaintext tcp on a non-loopback
            # host without this. The whole docker setup *is* a
            # non-loopback bind by design (0.0.0.0 with port-forwarded
            # host:container access), so the guard legitimately needs the
            # override here. tcp_ssl doesn't trip it — SSL is exactly the
            # production-shaped alternative the guard recommends.
            doc["insecure_tcp"] = True
        doc.update(self.extra_config)
        return doc

    def _listener(self, port: int) -> dict:
        """One outward-facing listener entry for the `modules` block.

        These are the DAEMON's key names: `protocol`, and for tcp_ssl the
        server's own `cert`/`key`. The client half of the wire
        description spells the same ideas `transport` and `ca` — see
        `DaemonEndpoint`. Using one document's names in the other fails
        the parse, and on this side an unrecognised `protocol` sinks the
        whole config, not just the entry.

        The daemon prepends a `local` listener to every module
        unconditionally, so what we declare here is the *additional*
        outward-facing surface, and intra-daemon traffic keeps using the
        local socket.
        """
        listener: dict = {
            "protocol": self.transport,
            # Bind on all interfaces: docker's port-forwarding reaches
            # the container through its own veth address, so a loopback
            # bind would be unreachable from the host.
            "host": "0.0.0.0",
            "port": port,
            "codec": self.codec,
        }
        if self.transport == "tcp_ssl":
            listener["cert"] = CONTAINER_CERT_PATH
            listener["key"] = CONTAINER_KEY_PATH
        return listener

    def _install_daemon_config(self) -> None:
        """Write the config document to the host side of /config and
        install it into the session with `daemon config set`.

        A second, throwaway container rather than one shell invocation:
        the image's entrypoint IS the binary, so a `docker run` carries
        exactly one logosctl command. The /config bind-mount is what
        carries the result across to the daemon container.

        Deliberately not routed through `_proc.run_json` — that would run
        the host's `logosctl`, and this document describes the
        container's filesystem. It also keeps the CLI's own message,
        which names the offending key and is the whole point of installing
        through `config set` rather than dropping the file into
        `daemon/config.yaml` ourselves.
        """
        doc = self._daemon_config_document()
        _check_config_types(doc)

        doc_path = self._config_dir / "daemon.yaml"
        doc_path.write_text(_yaml_document(doc))

        cmd = [
            "docker", "run", "--rm",
            "-v", f"{self._config_dir}:{CONTAINER_CONFIG_DIR}",
            *_CONFIG_DIR_ENV,
            self.image,
            "daemon", "config", "set", CONTAINER_CONFIG_DOC,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            detail = "\n".join(
                s for s in ((r.stdout or "").strip(), (r.stderr or "").strip())
                if s
            )
            raise LogosctlError(
                f"daemon config was rejected (exit {r.returncode}): "
                f"{' '.join(cmd)}"
                + (f"\n{detail}" if detail else "")
                + f"\nThe document we submitted is at {doc_path}. A schema "
                  "error is reported AFTER the write, so treat this session's "
                  "daemon/config.yaml as unusable and rewrite it before "
                  "starting a daemon in it.",
                exit_code=r.returncode,
                stderr=r.stderr,
            )

    def _client_endpoints(
        self,
        tcp_host: str,
        wire_codec: str,
        verify: bool | None,
    ) -> dict[str, DaemonEndpoint]:
        """Per-module dial spec for the two well-known modules.

        Each module rides its OWN forwarded host port — `core_service` on
        `host_port`, `capability_module` on `host_cap_port` — so the two
        endpoints MUST carry distinct ports. Both entries are required:
        `core_service` is mandatory outright, and without
        `capability_module` the SDK falls back to a local socket for its
        first handshake, which a client outside the container doesn't
        have.

        `verify` and the CA are only serialized for `tcp_ssl`
        (DaemonEndpoint drops them for plain tcp). The CA is a host path:
        the client reads it, and the client runs on this side of the
        container boundary."""
        if self._host_port is None or self._host_cap_port is None:
            raise LogosctlError(
                "forwarded host ports not assigned — call start() first "
                "(both core_service and capability_module need a port)"
            )
        transport_kind = "tcp_ssl" if self.transport == "tcp_ssl" else "tcp"
        ca = str(self.ssl_ca) if self.ssl_ca else None
        return {
            "core_service": DaemonEndpoint(
                transport_kind, tcp_host, self._host_port, wire_codec,
                verify, ca),
            "capability_module": DaemonEndpoint(
                transport_kind, tcp_host, self._host_cap_port, wire_codec,
                verify, ca),
        }

    def _build_host_client_config(self) -> None:
        """Seed the host-only client config dir with the daemon's raw auto
        token (`client/auto.json`) plus a default `client/config.yaml`
        pointing at the forwarded host ports. Called once after the daemon
        comes up; `client()` rewrites config.yaml with the caller's dial
        params, but the token written here is what every client reuses.

        The raw auto token is pulled out of the container via `docker exec
        cat` rather than read off the host bind-mount — see
        `read_container_file` for the rationale (root-owned 0600 files
        don't widen on disk; we just pipe bytes out)."""
        if self._container_id is None:
            return

        # Default disk spec (localhost, constructor's verify_peer). The
        # token is the essential artifact — config.yaml is a sane default
        # that client() supersedes with the caller's tcp_host/codec/verify.
        endpoints = self._client_endpoints(
            "localhost", self.codec, self.verify_peer)

        raw_token = self._wait_for_auto_token()
        if not raw_token:
            raise LogosctlError(
                "daemon did not emit a readable auto token at "
                f"{CONTAINER_CONFIG_DIR}/client/auto.json within "
                f"{self.startup_timeout}s — cannot wire up an authenticated "
                "client"
            )

        LogosctlClient.write_config(
            self._host_client_dir, endpoints, token=raw_token)

    def _wait_for_auto_token(self) -> str | None:
        """Poll the container for `client/auto.json` and return the raw
        token string, or None if it never appears / can't be parsed
        within `startup_timeout`.

        This is the credential the daemon mints for itself at boot, and
        copying it into a foreign config dir is the documented way to
        authenticate a client that isn't co-resident with the daemon (the
        CLI's own transports doctest does exactly this over TCP and TLS).
        Worth knowing: it is issued `local_only`, and the only reason it
        works over TCP is that the daemon also registers the raw value
        with the in-process TokenManager, which is consulted ahead of the
        token store that would reject it. So don't build a test on the
        premise that a local-only token is refused over the network — and
        if that quirk is ever tightened, this is the line that has to
        become `token issue --name docker` (a named, non-local-only
        token, issued before boot).

        The file is `{version, name, token, issued_at}`; the daemon writes
        the same raw value to `daemon/tokens/auto.json`. We read the
        client-side copy because it's the one written last, after every
        transport is bound."""
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            text = self.read_container_file(
                f"{CONTAINER_CONFIG_DIR}/client/auto.json")
            if text is not None:
                try:
                    token = json.loads(text).get("token")
                except (json.JSONDecodeError, AttributeError):
                    token = None
                if token:
                    return token
            time.sleep(0.1)
        return None

    def stop(self) -> None:
        """Kill the container. Idempotent; safe to call even if start()
        never succeeded."""
        if self._container_id is not None:
            # Mirror the daemon's container logs to the parent's stderr
            # before tearing down — symmetric with _proc.py's CLI
            # forwarding, so a single env flag dumps both sides of the
            # CLI ↔ daemon conversation.
            if os.environ.get("LOGOSCTL_PY_FORWARD_OUTPUT", "").lower() in (
                "1", "true", "yes", "on",
            ):
                logs = self._capture_logs()
                header = f"[logosctl-py docker-daemon {self._container_name}]"
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
        binary: str = "logosctl",
        timeout: float | None = 30.0,
        tcp_host: str = "localhost",
        codec: str | None = None,
        no_verify_peer: bool | None = None,
    ) -> LogosctlClient:
        """Build a LogosctlClient wired to dial this daemon.

        `binary` is the host-side `logosctl` executable — the client
        shells out to it for every operation. Defaults to whatever
        `logosctl` resolves to on PATH.

        `tcp_host` defaults to localhost because the container's port
        is published there. Override for remote-docker setups — it is
        baked into BOTH modules' endpoints in the on-disk config.

        `no_verify_peer`: for `tcp_ssl` daemons this defaults to True so
        self-signed certs work out of the box (the common case for smoke
        tests) — it sets the on-disk `verify_peer` to False. Set it to
        False to exercise the verification path, in which case the
        constructor's `verify_peer` (controlled by
        `LogosctlDockerDaemon(verify_peer=...)`) takes effect and
        `ssl_ca` has to name the CA that signed the daemon's cert, or the
        handshake fails closed. Ignored when transport is plain `tcp`.

        Each call rewrites the on-disk dial spec, because on-disk is the
        only place a dial spec can live: `RpcClient::connect()` reads
        `client/config.yaml` verbatim, with no merge layer and no
        environment override — the whole `LOGOSCORE_CLIENT_*` family is
        gone. That also means the per-module ports, which a single
        uniform env override could never have expressed, are just two
        ordinary entries in the file.
        """
        if self._container_id is None:
            raise LogosctlError(
                "daemon is not running — call start() or use the context manager"
            )
        wire_codec = codec or self.codec
        # On-disk verify_peer (tcp_ssl only): skip by default so a
        # self-signed smoke cert connects; no_verify_peer=False exercises
        # the verify path against the constructor's verify_peer base.
        if self.transport == "tcp_ssl":
            if no_verify_peer is None:
                no_verify_peer = True
            verify: bool | None = False if no_verify_peer else self.verify_peer
        else:
            verify = None

        # Rewrite config.yaml in the host-only client dir (the daemon's
        # bind-mounted /config is root-owned, and the daemon rewrites its
        # own copy at every boot) with the caller's dial params + both
        # modules' distinct forwarded ports. The auto token written by
        # _build_host_client_config() at startup is left in place.
        endpoints = self._client_endpoints(tcp_host, wire_codec, verify)
        return LogosctlClient.connect(
            endpoints,
            binary=binary,
            config_dir=self._host_client_dir,
            timeout=timeout,
        )

    # ── Internals ───────────────────────────────────────────────────────

    def _wait_for_conn_file(self) -> bool:
        deadline = time.monotonic() + self.startup_timeout
        # state.json appears once every transport has bound AND the
        # bundled package modules are loaded — i.e. it means ready, with
        # no further sleep needed. Poll for it through `docker exec ...
        # test -f` rather than the host bind-mount: the daemon writes the
        # file as root with restrictive perms, so a host-side
        # `Path.exists()` is at best fragile and at worst depends on
        # filesystem-specific behavior. Asking the container directly is
        # unambiguous.
        while time.monotonic() < deadline:
            if self.container_file_exists(
                    f"{CONTAINER_CONFIG_DIR}/daemon/state.json"):
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

    def __enter__(self) -> "LogosctlDockerDaemon":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()
