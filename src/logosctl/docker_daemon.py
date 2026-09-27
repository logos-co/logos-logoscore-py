"""Lifecycle manager for a `logosctl` daemon running inside docker.

`LogosctlDockerDaemon` is to `LogosctlDaemon` what its name suggests: the
same context-manager shape, but the daemon runs in a container and the host
operates it with Remote Runtime Control. Use it when your test setup
deliberately crosses a container boundary — e.g. to smoke-test a real
distribution of logosctl, or your own module against one.

Example:
    from logosctl import LogosctlDockerDaemon

    with LogosctlDockerDaemon(
        image="logosctl:smoke-portable",
        modules_dir="./my-module/result/modules",
        binary="logosctl",                  # the host-side client
    ) as daemon:
        client = daemon.client()
        client.load_module("my_module")
        print(client.call("my_module", "do_something", 42))

Remote Runtime Control:
    The container shares the host's network (`--network host`), and the
    daemon's `peering` section puts its control endpoint on a fixed loopback
    port with `runtime_control: true`, so core_service also listens on
    `tls_tcp`. `start()` mints a runtime-control invite inside the container,
    pairs a host-side logosctl (a config dir of its own, no daemon) with
    `logosctl remote pair`, accepts it inside the container, and grants it
    methods in the daemon's remote policy (`grants`, `DEFAULT_GRANTS` by
    default). `client()` runs every command with `--remote`. Host networking
    is what lets the client reach the runtime-control listener, whose port
    the daemon picks; Docker Desktop has it only as an opt-in setting.

Volume layout inside the container (all three dirs are on the host and
bind-mounted in — they survive the container):
    /config       — the session directory. Holds `daemon.yaml` (the
                    config document this wrapper writes) and
                    `remote-policy.json` (the last policy it set) plus
                    everything logosctl puts under a session:
                    `daemon/config.yaml`, `daemon/state.json`, `peering/`,
                    `client/`, `logs/`, `modules/`, `plugins/`.
    /persistence  — the `persistence_path` config key; pre-seed to
                    restore a session, read back to inspect what modules
                    wrote
    /user-modules — compiled Qt plugins, mounted read-only; reached by
                    the daemon through `modules_dirs`

Configuration is a document, not flags:
    logosctl has no `-m` or `--persistence-path`; all of it is a YAML
    document installed into the session BEFORE the daemon boots. So
    `start()` runs two containers over the same `/config` bind-mount: a
    throwaway `daemon config set /config/daemon.yaml`, then the real
    `daemon start`. Going through the CLI rather than dropping the file
    straight into `/config/daemon/config.yaml` is what buys the
    top-level-key allowlist — a near-miss like `persistencePath` comes back
    as an error naming the key instead of being silently dropped.
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

from ._proc import _error_codes_from_stdout
from .client import LogosctlClient
# The daemon document has one shape and one set of hazards whether the
# daemon runs here or in a container, so its emitter and its type checks
# live once, next to the flavor that came first.
from .daemon import _check_config_types, _yaml_document
from .errors import LogosctlError, from_exit_code
from .remote import RuntimeControl, runtime_control_config


# ── Module-level helpers (also re-exported from the package) ──────────────

# Paths *inside* the container: bind-mount targets we choose. The daemon
# config document is written in these terms, so every path in it has to be
# the container's, never the host's. The image's own modules sit beside its
# binary, where the daemon finds them unasked.
CONTAINER_CONFIG_DIR       = "/config"
CONTAINER_PERSISTENCE_DIR  = "/persistence"
CONTAINER_USER_MODULES_DIR = "/user-modules"
# The document `daemon config set` reads. Written by the host into the
# root of the bind-mounted session dir, deliberately NOT at
# `daemon/config.yaml` — that path is the CLI's to write, and this is
# only the input it writes it from.
CONTAINER_CONFIG_DOC = f"{CONTAINER_CONFIG_DIR}/daemon.yaml"
CONTAINER_POLICY_FILE = f"{CONTAINER_CONFIG_DIR}/remote-policy.json"

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
    """Spawn a logosctl daemon inside a docker container and operate it
    from the host with Remote Runtime Control.

    Construction stores config only. `start()` installs the session's
    daemon config, runs the container, waits for `state.json`, and pairs
    the host-side client; `stop()` kills it. Use the context-manager form
    to get start/stop bracketing automatically.
    """

    def __init__(
        self,
        *,
        image: str,
        modules_dir: str | Path,
        # The HOST-side logosctl that pairs with the daemon and runs every
        # client command; the container's own binary never leaves it.
        binary: str = "logosctl",
        config_dir: str | Path | None = None,
        persistence_dir: str | Path | None = None,
        # The daemon's control endpoint, on the loopback the container
        # shares with the host. None: a free port picked at start().
        control_port: int | None = None,
        # The daemon's peering name: the alias the host client lists it as.
        name: str = "node",
        # What the host client may call, as its remote-policy entry
        # (`DEFAULT_GRANTS` when None). `runtime_control.grant()` changes it.
        grants: Mapping[str, Any] | None = None,
        container_name: str | None = None,
        extra_module_dirs: Sequence[str] | None = None,
        # Extra top-level keys merged into the daemon config document —
        # `access_group`, `dirs`, `logging`, `access_policy`, … Merged
        # last, so a caller can also override what this wrapper computes
        # (the `peering` section included). Keys are allowlisted by the
        # CLI; an unknown one fails `config set` with a message naming it.
        extra_config: Mapping[str, Any] | None = None,
        # Still argv, but only the app-level flags are left (`--verbose`,
        # `--quiet`) — everything that configures the daemon is config.
        extra_args: Sequence[str] | None = None,
        # A logosctl daemon creates the session's modules/plugins/keyring/
        # cache dirs and loads package_manager + package_downloader and
        # peering before it writes state.json.
        startup_timeout: float = 30.0,
    ) -> None:
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
                "Build your module(s) first (e.g. `nix build .#install-portable` "
                "or via build_modules_in_docker())."
            )
        if not self.modules_dir.is_dir():
            raise NotADirectoryError(
                f"modules_dir is not a directory: {self.modules_dir}"
            )
        self.binary = binary
        self.name = name
        self.grants = dict(grants) if grants is not None else None
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

        self._control_port = control_port  # may be None until start()
        self._container_name = (
            container_name
            or f"logosctl-{uuid.uuid4().hex[:12]}"
        )
        self._container_id: str | None = None
        # The host-side client's pairing, from start() to stop(). Its config
        # dir is the host's own: the container writes /config as root.
        self._runtime_control: RuntimeControl | None = None

    # ── Public properties ───────────────────────────────────────────────

    @property
    def control_port(self) -> int:
        """The daemon's control endpoint port, on 127.0.0.1. Only valid once
        `start()` has picked it."""
        if self._control_port is None:
            raise LogosctlError("daemon hasn't started yet")
        return self._control_port

    @property
    def runtime_control(self) -> RuntimeControl:
        """The host-side client's pairing: `grant()` changes what it may
        call, `runtime_id` is its runtime ID."""
        if self._runtime_control is None:
            raise LogosctlError(
                "daemon is not running — call start() or use the context manager"
            )
        return self._runtime_control

    @property
    def config_dir(self) -> Path:
        """Host path of the daemon's session directory. The container
        runs as root and writes `daemon/config.yaml`, `daemon/state.json`,
        `daemon/tokens.json`, `peering/`, `client/config.yaml`,
        `client/auto.json` and `logs/` here as root-owned, with the
        credential-adjacent ones at 0600 (and `daemon/` itself locked to
        0700 when an access group is set). The host process generally
        can't read those directly even though it owns the surrounding dir.
        Use `read_container_file()` (which goes through `docker exec ...
        cat`) or the higher-level helpers (`state_json`, `instance_id`,
        `daemon_log`) to extract content; reaching into this path with
        `read_text()` will hit a PermissionError."""
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
        listeners), `tokens.json` (hashed token list),
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
        `instance_id` and the listeners the daemon bound."""
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

    # ── Operating it from inside ─────────────────────────────────────────

    def peer(self, verb: str, *args: str) -> Any:
        """`logosctl peer <verb> [args…]` inside the container, as the
        daemon's local operator; the reply parsed like `LogosctlClient`'s."""
        # `/proc/1/exe` is the daemon's own binary, whatever the image.
        cmd = ["docker", "exec", self.container_id, "/proc/1/exe",
               "--config-dir", CONTAINER_CONFIG_DIR, "--json", "peer", verb, *args]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           timeout=self.startup_timeout)
        if r.returncode != 0:
            code, detail = _error_codes_from_stdout(r.stdout or "")
            raise from_exit_code(
                r.returncode,
                f"logosctl command failed in the container (exit {r.returncode}): "
                f"{' '.join(cmd)}\n{(r.stdout or r.stderr or '').strip()}",
                stderr=r.stderr, error_code=code, detail_error_code=detail)
        return json.loads(r.stdout) if (r.stdout or "").strip() else None

    def set_remote_policy(self, policy: Mapping[str, Any]) -> Any:
        """Replace the daemon's remote policy (`logosctl peer policy set`),
        through a file in the host side of `/config`."""
        (self._config_dir / "remote-policy.json").write_text(
            json.dumps(policy), encoding="utf-8")
        reply = self.peer("policy", "set", CONTAINER_POLICY_FILE)
        if not (isinstance(reply, dict) and reply.get("ok") is True):
            raise LogosctlError(f"the daemon did not take its remote policy: {reply!r}")
        return reply

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> "LogosctlDockerDaemon":
        """Install the session's daemon config, `docker run` the daemon,
        block until it writes state.json, and pair the host-side client.

        Raises LogosctlError on docker failure / bad config / startup
        timeout / a refused pairing. Does NOT check `docker_available()` or
        `image_present()` up front — callers that care about environmental
        skips should do so before calling start().
        """
        if self._container_id is not None:
            raise LogosctlError("daemon is already started")

        if self._control_port is None:
            self._control_port = pick_free_port()

        # Everything logoscore passed as daemon flags is a document now,
        # and `daemon start` acts on whatever is already on disk — so the
        # config has to be installed into the session first, in its own
        # container over the same bind-mount.
        self._install_daemon_config()

        # Deliberately no --rm. If the daemon exits during startup, --rm
        # would auto-remove the container before _capture_logs gets a
        # chance to read it — the on-fail diagnostic would just say "No
        # such container". stop() below explicitly does `docker rm -f`,
        # so we don't leak containers either.
        cmd: list[str] = [
            "docker", "run", "-d",
            "--name", self._container_name,
            # The host's network: its loopback reaches the control endpoint
            # and the runtime-control listener, on a port the daemon picks.
            "--network", "host",
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

        # Tear the container down if pairing fails, so a failed start()
        # doesn't leak a running container.
        try:
            self._runtime_control = RuntimeControl(self, binary=self.binary)
            self._runtime_control.pair()
            self._runtime_control.grant(self.grants)
        except Exception:
            self.stop()
            raise
        return self

    def _volume_args(self) -> list[str]:
        return [
            "-v", f"{self._config_dir}:{CONTAINER_CONFIG_DIR}",
            "-v", f"{self._persistence_dir}:{CONTAINER_PERSISTENCE_DIR}",
            "-v", f"{self.modules_dir}:{CONTAINER_USER_MODULES_DIR}:ro",
        ]

    def _daemon_config_document(self) -> dict:
        """The daemon config document — the same one `LogosctlDaemon`
        builds, except every path in it is the container's, plus the
        `peering` section that turns Remote Runtime Control on.

        `modules_dirs` and `persistence_path` are read inside the
        container, so a host path in either names a directory that isn't
        there (or, worse, one the daemon will happily create and find
        empty).
        """
        doc: dict = {
            # Replaces every `-m` / `--modules-dir`: the user's bind-mount,
            # then anything the caller added.
            "modules_dirs": [CONTAINER_USER_MODULES_DIR, *self.extra_module_dirs],
            # Replaces `--persistence-path`. `dirs: {data: …}` is the
            # newer spelling for the same thing and wins if both are
            # given; one is enough.
            "persistence_path": CONTAINER_PERSISTENCE_DIR,
            "peering": runtime_control_config(
                self.name, host="127.0.0.1", port=self.control_port),
        }
        doc.update(self.extra_config)
        return doc

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

        if self._runtime_control is not None:
            self._runtime_control.close()
            self._runtime_control = None
        # Only clean up dirs we created ourselves. Anything the caller
        # passed in (e.g. a pre-seeded persistence dir they want to
        # inspect after the test) stays on disk.
        if self._owns_config_dir and self._config_dir.exists():
            shutil.rmtree(self._config_dir, ignore_errors=True)
        if self._owns_persistence_dir and self._persistence_dir.exists():
            shutil.rmtree(self._persistence_dir, ignore_errors=True)

    # ── Client factory ──────────────────────────────────────────────────

    def client(self, *, timeout: float | None = 30.0) -> LogosctlClient:
        """A LogosctlClient that runs every command on this daemon over
        Remote Runtime Control, as the host-side logosctl `start()` paired.
        What it may call is `grants` (see `runtime_control.grant()`)."""
        return self.runtime_control.client(timeout=timeout)

    # ── Internals ───────────────────────────────────────────────────────

    def _wait_for_conn_file(self) -> bool:
        deadline = time.monotonic() + self.startup_timeout
        # state.json appears once every listener has bound AND the
        # bundled modules are loaded — i.e. it means ready, with
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
