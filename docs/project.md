# logos-logoscore-py — Project Description

## Overview

`logos-logoscore-py` is the **`logoscore` PyPI package** — a thin, dependency-free
Python wrapper around the headless [`logoscore`](https://github.com/logos-co/logos-logoscore-cli)
CLI. Every operation spawns a `logoscore <subcommand> --json` subprocess and parses
its JSON stdout. There are **no C++ bindings and no IPC code** in the package itself:
all of the wire work (the local socket; for `logosctl`, Remote Runtime Control and
peering over `tls_tcp`) lives in the CLI it drives. The same wheel ships the
`logosctl` client, which mirrors `logoscore`'s surface; this document describes
`logoscore` unless it names `logosctl`.

It exists so test suites and Python tooling can drive a real Logos daemon — load
compiled Qt-plugin modules, invoke their `Q_INVOKABLE` methods, watch their events —
without shelling out and parsing text by hand. It is primarily a **testing and
automation surface**: what module authors use to smoke-test their plugins against a
real distributed build of `logoscore`, and what the platform uses to exercise the
full wire stack end-to-end.

### Place in the Logos platform

The daemon this package drives is the headless CLI runtime over `logos-liblogos`,
which hosts compiled Logos modules (process-isolated Qt plugins, or pure-C++ universal
modules). This package sits at the **frontend edge**, one hop above the CLI:

```
  logos-logoscore-py   (this repo — Python wrapper, PyPI `logoscore`)
        │  spawns `logoscore <subcommand> --json` subprocesses
        ▼
  logos-logoscore-cli  (the `logoscore` daemon + client CLI)
        │  liblogos C API
        ▼
  logos-liblogos       (core runtime: logos_host, liblogos_core)
        │
        ▼
  logos-cpp-sdk        (LogosAPI, RPC, code generator — pins nixpkgs/Qt 6)
        │
        ▼
  modules              (logos-test-modules: test_fullapi_cpp [universal C++,
                        full type surface], capability_module, …)
```

Because the wrapper only ever speaks to the CLI, its sole external runtime requirement
is the `logoscore` binary on `PATH`. The Nix flake propagates that binary and pulls in
`logos-test-modules` so the test suite runs out of the box.

### Three lifecycle flavors

| Class | What it drives | When to use |
|---|---|---|
| `LogoscoreDaemon` | spawns a local `logoscore -D` subprocess with an isolated temp `--config-dir` | fast local iteration, in-process tests; multiple daemons coexist without colliding on `~/.logoscore` |
| `LogosctlDockerDaemon` (logosctl) | runs a logosctl daemon inside a docker container, operated from the host over Remote Runtime Control | smoke-test a real distribution of logosctl, or your module against one |
| `LogoscoreClient` | connects to an already-running daemon on this host | a daemon started elsewhere (shell, service manager), or from a config dir it doesn't own via `LogoscoreClient.connect()` |

A daemon on another machine is operated with `logosctl`'s Remote Runtime Control
(`RuntimeControl`, `LogosctlClient(remote=…)`), and a module calling a module on
another runtime is peering (`PeeredDaemons`); see the README and the Logos developer
guide, §9.6.

---

## Project Structure

```
logos-logoscore-py/
├── pyproject.toml                  # hatchling build; package `logoscore` v0.1.0; no runtime deps
├── flake.nix                       # wheel package + docker bundles + dev shell + unit/integration checks
├── flake.lock
├── README.md                       # user-facing quickstart + API overview
├── LICENSE-MIT / LICENSE-APACHE-v2 # dual-licensed MIT OR Apache-2.0
│
├── src/logoscore/                  # the package
│   ├── __init__.py                 # public API re-exports + __all__; __version__ = "0.1.0"
│   ├── client.py                   # LogoscoreClient, DaemonEndpoint, write_config/connect,
│   │                               #   arg coercion (_arg_to_str), tagged-bytes decode (_decode_bytes_tags)
│   ├── daemon.py                   # LogoscoreDaemon — local subprocess lifecycle
│   ├── events.py                   # Subscription — background-thread NDJSON pump over `logoscore watch`
│   ├── tokens.py                   # daemon-less issue_token / revoke_token / list_tokens
│   ├── errors.py                   # LogoscoreError hierarchy + from_exit_code() exit-code mapping
│   └── _proc.py                    # internal run_json(): builds argv, sets env, parses JSON, maps failures
│
├── src/logosctl/                   # the logosctl client, the same surface plus:
│   ├── remote.py                   # RuntimeControl — Remote Runtime Control pairing + grants
│   ├── docker_daemon.py            # LogosctlDockerDaemon + docker helpers + build_modules_in_docker
│   └── peering.py                  # PeeredDaemons — two daemons, one importing the other's modules
│
├── tests/
│   ├── conftest.py                 # fixtures: logoscore_bin, test_modules_dir; --docker-flavor
│   ├── _fullapi_module_cases.py    # FULLAPI_METHOD_CASES + FULLAPI_EVENT_CASES — shared matrices
│   ├── unit/                       # no logoscore needed (runs anywhere)
│   │   ├── test_client_config.py   #   write_config serialization + connect()/client() contract
│   │   ├── test_client_with_fake.py#   argv/env construction via monkeypatched subprocess.run
│   │   └── test_errors.py          #   exit-code → exception mapping
│   ├── integration/                # spawns real local daemons
│   │   ├── test_end_to_end.py      #   status / list / load+call+event round-trip
│   │   └── test_fullapi_module_cpp.py        #   test_fullapi_cpp — full param/return/event type surface
│   ├── logosctl/                   # the logosctl twins, + peering and Remote Runtime Control
│   └── docker_smoke/               # logosctl in docker (can't run inside the nix sandbox)
│       ├── Dockerfile              #   multi-stage; stage 1 runs `nix build` in nixos/nix
│       ├── build_smoke_image.sh    #   builds logosctl:smoke-{portable,dev} (FLAVOR=…)
│       ├── build_modules_in_docker.sh  # shell wrapper over build_modules_in_docker()
│       ├── conftest.py             #   docker-flavor fixtures + image/skip gating
│       ├── test_docker_smoke.py    #   method+event matrix, policy, two daemons — over RRC
│       └── README.md               #   image flavors, mount layout, host networking
│
├── docs/
│   ├── index.md
│   ├── spec.md                     # stack-agnostic spec (business logic, domain model, workflows)
│   └── project.md                  # this file
│
└── .github/workflows/
    ├── ci.yml                      # nix build + unit + integration-local; logosctl job + docker smoke
    └── publish.yml                 # PyPI trusted publishing on v* tags
```

---

## Technology Stack

| Component | Type | Purpose |
|---|---|---|
| Python ≥ 3.10 (3.10 / 3.11 / 3.12) | language | Wrapper implementation. Standard library only at runtime — `subprocess`, `json`, `threading`, `socket`, `tempfile`, `weakref`, `signal`, `base64`, `logging` |
| hatchling | build backend | PEP 517 build of the `logoscore` wheel (`tool.hatch.build.targets.wheel` → `src/logoscore`) |
| pytest ≥ 7 | test runner | Optional `[test]` extra; provided by the Nix dev shell and checks |
| Nix flakes | packaging | Reproducible wheel build, docker bundles, dev shell, and CI checks |
| Docker / buildx | tooling | Smoke tests; the logosctl daemon-in-a-container path and `build_modules_in_docker` |

### Runtime dependencies

The package declares **zero runtime Python dependencies** (`dependencies = []` in
`pyproject.toml`). Its one hard requirement is the `logoscore` CLI binary on `PATH`
(or supplied via the `binary=` kwarg / `LOGOSCORE_BIN` env in tests).

### Flake inputs

| Input | Purpose |
|---|---|
| `logos-nix` | Provides the shared nixpkgs pin (`nixpkgs.follows = "logos-nix/nixpkgs"`) |
| `logos-logoscore-cli` | The `logoscore` daemon/CLI binary the package wraps. Its `default` package is `propagatedBuildInputs` of the wheel; `ctl` and `ctl-bundle-dir` (logosctl) feed the docker bundles |
| `logos-test-modules` | `test_fullapi_cpp` (universal C++, full param/return/event type surface) — the single plugin the integration/smoke suites load (via `.install` / `.install-portable`) |
| `nixpkgs` | `python3`, `hatchling`, `qt6.qtbase` for builds and checks |

---

## Components

Everything below is re-exported from `logoscore/__init__.py`. The package version is
`__version__ = "0.1.0"`.

### `LogoscoreClient` (`client.py`)

A thin client around `logoscore` subcommands against a running daemon. Each method
spawns `logoscore <subcommand> --json` and parses the result.

```python
LogoscoreClient(
    binary="logoscore", *,
    config_dir=None, token=None, timeout=30.0,
)
```

Method → CLI subcommand map (every invocation gets a trailing `--json`):

| Method | CLI subcommand | Returns |
|---|---|---|
| `status()` | `status` | `dict` |
| `stats()` | `stats` | `Any` |
| `stop()` | `stop` | `None` |
| `list_modules(*, loaded=False)` | `list-modules [--loaded]` | `list[dict]` |
| `module_info(name)` | `module-info <name>` | `dict` |
| `load_module(name)` | `load-module <name>` | `dict` |
| `unload_module(name)` | `unload-module <name>` | `dict` |
| `reload_module(name)` | `reload-module <name>` | `dict` |
| `call(module, method, *args, timeout=None)` | `call <module> <method> …` | unwrapped `result` value |
| `on_event(module, event, callback, *, error_callback=None)` | `watch <module> [--event <event>]` | `Subscription` |

`call(...)` details:
- **Argument coercion** (`_arg_to_str`): a `pathlib.Path` becomes `@<path>` so the CLI
  loads the file's contents; `bool` becomes `"true"`/`"false"`; `bytes`/`bytearray` are
  passed as raw latin-1 characters; everything else is `str(arg)` for the CLI's own
  type coercion.
- **Tagged-bytes decode** (`_decode_bytes_tags`): the result is recursively scanned for
  the logos-protocol canonical byte form `{"_bytes": "<base64url, unpadded>"}` and
  decoded back to `bytes` — exactly once, at this boundary.
- Returns the `result` field of the JSON envelope; raises `MethodError` when the
  envelope reports `status == "error"`.

#### `LogoscoreClient.connect(...)` (classmethod)

```python
@classmethod
def connect(
    endpoints: Mapping[str, DaemonEndpoint], *,
    token=None, binary="logoscore", config_dir=None,
    timeout=30.0, instance_id=None,
) -> LogoscoreClient
```

Builds a client dialing a daemon on this host from a config dir the daemon does not
own, described by explicit per-module endpoints. Materializes `client/config.json` +
the token file via `write_config`; the on-disk spec is authoritative. `instance_id` is
the daemon's (its local socket is named after it). When `config_dir` is `None` a
private temp dir is created and removed via `weakref.finalize` when the client is
collected; pass `config_dir` to keep it.

#### `LogoscoreClient.write_config(...)` (staticmethod)

```python
@staticmethod
def write_config(
    config_dir, endpoints: Mapping[str, DaemonEndpoint], *,
    token=None, instance_id=None, merge=False,
) -> None
```

The single source of truth for the on-disk `<config_dir>/client/config.json` (schema
**version 2**): a `daemon` block with one entry per well-known module. Writes the raw
`token` (wrapped as `{"token": …}`) to the file named by `token_file` (default
`auto.json`), with a traversal-safe fallback to `auto.json` when a merged config carries
an unsafe `token_file`. `merge=True` preserves pre-existing top-level keys.

### `DaemonEndpoint` (`client.py`)

```python
@dataclass(frozen=True)
class DaemonEndpoint:
    transport: str = "local"
```

One well-known module's dial spec, serialized into a single `daemon`-block entry:
`{"transport": "local"}`, the only client transport.

### `LogoscoreDaemon` (`daemon.py`)

Context manager that spawns `logoscore -D` with an isolated `--config-dir`.

```python
LogoscoreDaemon(
    modules_dir,                      # str | Path | list — one or more -m dirs
    *, binary="logoscore",
    config_dir=None, persistence_path=None,
    extra_args=None, env=None, startup_timeout=15.0,
)
```

- **Startup** (`start()` / `__enter__`): builds the command
  `logoscore -D --config-dir <dir> -m <dir>… [--persistence-path …]`, waits for
  `daemon/state.json`, then verifies with `status`.
- **Shutdown** (`stop(timeout=10.0)` / `__exit__`): runs `logoscore stop`, then escalates
  `terminate()` → `kill()`, and removes the temp config dir it created. Safe to call
  repeatedly.
- **Properties**: `config_dir`, `state_file` (`<config_dir>/daemon/state.json`),
  `connection_file` (backward-compat alias for `state_file`), `client_token_file`
  (`<config_dir>/client/auto.json`), `pid`.
- **`client(*, timeout=30.0)`** — returns a `LogoscoreClient` reading the per-module
  `client/config.json` the daemon wrote into its own session.
- **`logs() -> (stdout, stderr)`** — the daemon's captured `daemon.stdout.log` /
  `daemon.stderr.log`.

### `LogosctlDockerDaemon` (`logosctl/docker_daemon.py`)

Context manager that `docker run`s a logosctl daemon and operates it from the host over
Remote Runtime Control.

```python
LogosctlDockerDaemon(
    *, image, modules_dir, binary="logosctl",
    config_dir=None, persistence_dir=None, control_port=None,
    name="node", grants=None, container_name=None,
    extra_module_dirs=None, extra_config=None, extra_args=None, startup_timeout=30.0,
)
```

- Installs the daemon config with a throwaway `daemon config set` container: the
  container paths below, plus a `peering` section with its control endpoint on
  `127.0.0.1:control_port` (a free port when `None`) and `runtime_control: true`.
- Runs the daemon with `--network host`: the host's loopback reaches the control
  endpoint and core_service's runtime-control listener, whose port the daemon picks.
- Bind-mounts three host dirs: `/config` (the session), `/persistence`
  (`persistence_path`), `/user-modules:ro` (your compiled plugins).
- Pairs the host-side `binary` with `RuntimeControl`: `peer invite --runtime-control`
  and `peer accept` inside the container (`docker exec`), `remote pair` on the host,
  then grants `grants` (`DEFAULT_GRANTS` when `None`) in the remote policy.
- Daemon files under `/config` are root-owned 0600; read them via `read_container_file`
  / `container_file_exists` / `state_json` (which go through `docker exec … cat`) rather
  than direct host reads.
- **Properties**: `control_port`, `runtime_control` (the host client's pairing),
  `config_dir`, `persistence_dir`, `container_id`, `container_name`, `instance_id`,
  `state_json()`.
- **`client(*, timeout=30.0)`** — a `LogosctlClient` that runs every command with
  `--remote`; **`peer(verb, *args)`** / **`set_remote_policy(policy)`** act as the
  daemon's local operator, inside the container.

Module-level helpers (also re-exported from `logosctl`): `docker_available() -> bool`,
`image_present(image) -> bool`, `pick_free_port() -> int`.

### `RuntimeControl` (`logosctl/remote.py`)

```python
RuntimeControl(daemon, *, binary="logosctl", config_dir=None, timeout=60.0)
rc.pair()                  # invite on the daemon, `remote pair` here, accept there
rc.grant(grants=None)      # this client's remote-policy entry; DEFAULT_GRANTS when None
rc.client(timeout=30.0)    # LogosctlClient(config_dir=rc.config_dir, remote=rc.daemon_id)
```

`daemon` is anything with `peer(verb, *args)` and `set_remote_policy(policy)`: a
`LogosctlDaemon` or a `LogosctlDockerDaemon`. `runtime_control_config(name, host=,
port=)` is the daemon's `peering` section. `DEFAULT_GRANTS` is every `LogosctlClient`
command but `stop`, plus every method of the daemon's user modules.

#### `build_modules_in_docker(...)`

```python
build_modules_in_docker(
    builds: Sequence[tuple[str, str]], *,
    output_dir, builder_image=None, timeout=1800.0,
) -> Path
```

Builds one or more Logos module flakes inside a `nixos/nix:2.24.9` container (shared nix
store, so common deps are fetched once) for ABI compatibility with the daemon image.
`builds` is a list of `(flake_ref, attr)` tuples — `attr` must point at a derivation
whose `$out/modules/<name>/…` matches the daemon's `modules_dirs` layout (e.g.
`packages.x86_64-linux.install-portable`). Returns the merged host modules dir. The
builder image is overridable via `LOGOSCTL_BUILDER_IMAGE`. **Local `path:` flake refs
are not supported** — the host filesystem isn't mounted into the one-shot container, so
push to github and reference `github:…`.

### `Subscription` (`events.py`)

A live event subscription backed by a `logoscore watch … --json` subprocess. A daemon
thread reads NDJSON from the watcher's stdout and dispatches each parsed event dict to
`callback`.

```python
Subscription.start(*, binary, args, config_dir, token, callback, error_callback, extra_env=None)
sub.alive        # False once the watcher exits
sub.cancel(timeout=5.0)   # SIGINT → SIGTERM → SIGKILL
# also usable as a context manager (__enter__/__exit__ → cancel)
```

The callback runs on a daemon thread; exceptions (and JSON-decode errors) are routed to
`error_callback`, or logged via `logging` when none is given.

### Tokens (`tokens.py`) — daemon-less

These read/write the config dir directly; no running daemon needed.

| Function | CLI subcommand | Returns |
|---|---|---|
| `issue_token(name, *, binary="logoscore", config_dir=None, replace=False, timeout=30.0)` | `issue-token --name <name> [--replace]` | `{"name", "token", "file", …}` |
| `revoke_token(name, *, binary="logoscore", config_dir=None, timeout=30.0)` | `revoke-token <name>` | `dict` (raises `ModuleError` on exit 3) |
| `list_tokens(*, binary="logoscore", config_dir=None, timeout=30.0)` | `list-tokens` | `[{"name", "issued_at"}, …]` |

The daemon stores only a hash; the raw token is visible only in the `issue_token`
return value and the per-client file it points at.

### Errors (`errors.py`)

`LogoscoreError(message, *, exit_code=None, stderr=None, code=None)` is the base class.
`from_exit_code(code, message, *, stderr=None, error_code=None)` dispatches CLI exit
codes to subclasses (unknown codes fall back to the base `LogoscoreError`):

| Exit code | Exception |
|---|---|
| 2 | `DaemonNotRunningError` |
| 3 | `ModuleError` |
| 4 | `MethodError` |

### Internal subprocess runner (`_proc.py`)

`run_json(binary, args, *, config_dir=None, token=None, env=None, timeout=30.0)` is the
single chokepoint: it builds `[binary, *args, "--json"]`, sets `LOGOSCORE_CONFIG_DIR` and
`LOGOSCORE_TOKEN` on the subprocess env (plus any `env` overrides), runs it, maps a
non-zero exit via `from_exit_code` (carrying the JSON `code` field from stdout when
present), and parses stdout as a single JSON value. Setting
`LOGOSCORE_PY_FORWARD_OUTPUT=1` (or `true`/`yes`/`on`) mirrors the CLI's **stderr**
(its qDebug/qWarning trail) to the parent's stderr and adds `--verbose` to the
invocation; stdout is deliberately **not** forwarded (it may carry raw tokens).

---

## Domain Concepts

| Term | Meaning |
|---|---|
| **logoscore daemon** | the `logoscore -D` runtime that hosts Logos modules and exposes them over RPC; this package launches and dials it |
| **well-known modules** | `core_service` and `capability_module` — always served by the daemon, each on its **own** listener |
| **`client/config.json` (v2)** | on-disk dial spec under `<config_dir>/client/`; a `daemon` block with one `DaemonEndpoint` per well-known module, all over the local socket. Authoritative |
| **Remote Runtime Control** | logosctl only: a client config dir paired with a daemon elsewhere runs its commands there (`--remote`) over `tls_tcp`; the daemon's remote policy grants each method |
| **`state.json`** | `<config_dir>/daemon/state.json`, written post-bind; carries `instance_id`, `pid`, `started_at`, and resolved per-module transports. Its appearance signals daemon readiness |
| **token / `auto.json`** | the daemon issues a signed token per client. The raw local-client token lands in `<config_dir>/client/auto.json`; the hashed-at-rest list is `<config_dir>/daemon/tokens.json` |
| **`Q_INVOKABLE`** | a C++/Qt module method exposed for RPC; `LogoscoreClient.call(module, method, *args)` invokes one |
| **tagged-bytes** | logos-protocol's NUL-safe form for byte arrays crossing JSON, `{"_bytes": "<base64url>"}`; decoded once at the `call()` boundary |
| **`LogosResult`** | a module return struct serialized as `{"success": bool, "value": any, "error": any}`; pinned across the basic-module matrix |
| **portable vs dev docker flavor** | `portable` = logosctl's self-contained `ctl-bundle-dir` (matches released binaries, default); `dev` = the nix-store-rpath-linked `ctl` package (~3 GB, needs `/nix/store` in image). `.install-portable` user modules load in either |

---

## Building and Testing

### Workspace forms (preferred)

```bash
export PATH="/workspace/scripts:$PATH"

ws build logos-logoscore-py                 # build the wheel package
ws build logos-logoscore-py --auto-local    # build with local dep overrides
ws test  logos-logoscore-py                 # run the repo's nix checks
ws test  logos-logoscore-py --auto-local    # with local overrides
```

### Raw Nix

```bash
nix build                          # default = the python wheel
nix build .#logoscore-py           # same wheel, explicit attr
nix build .#dockerBundlePortable   # self-contained logosctl bundle for the smoke image (Linux)
nix build .#dockerBundle           # dev (nix-store-linked) logosctl bundle (Linux)

nix develop                        # python + pytest + logoscore + logosctl on PATH
```

The dev shell exports `LOGOSCORE_BIN`, `LOGOSCORE_TEST_MODULES_DIR`, `LOGOSCTL_BIN`,
`LOGOSCTL_TEST_MODULES_DIR`, `LOGOSCTL_PLAIN_MODULES_DIR` and, on Linux,
`LOGOSCTL_DOCKER_MODULES_DIR` (the `.install-portable` module the docker smoke mounts),
and prepends `src/` to `PYTHONPATH`, so `pytest` works without extra setup.

### Nix checks

```bash
nix flake check                                   # every check, conformance included
nix build '.#checks.x86_64-linux.unit'            # unit only (no daemon)
nix build '.#checks.x86_64-linux.integration-local'
nix build '.#checks.x86_64-linux.integration'     # back-compat alias = integration-local
nix build '.#checks.x86_64-linux.integration-logosctl-local'   # + peering, Remote Runtime Control
```

### pytest directly

```bash
pytest                                  # testpaths = tests (unit + integration; docker skipped)
pytest tests/unit -v                    # no logoscore binary required
pytest tests/integration -v
```

Integration tests **skip** unless `LOGOSCORE_BIN` and `LOGOSCORE_TEST_MODULES_DIR` are
set (the dev shell / nix checks set both).

### Docker smoke tests

These need a docker socket and so cannot run inside the nix sandbox — they live in
`tests/docker_smoke/` and run only in the dedicated CI step.

```bash
./tests/docker_smoke/build_smoke_image.sh        # FLAVOR=portable (default)
FLAVOR=dev  ./tests/docker_smoke/build_smoke_image.sh
FLAVOR=both ./tests/docker_smoke/build_smoke_image.sh

nix develop --command pytest tests/docker_smoke -v       # as CI runs it
nix develop --command pytest tests/docker_smoke --docker-flavor=both
```

### Distribution

```bash
python -m build         # sdist + wheel (publish.yml does this on v* tags → PyPI trusted publishing)
pip install logoscore   # the `logoscore` CLI must already be on PATH
```

### CI (`.github/workflows`)

`ci.yml` runs on `x86_64-linux` and `aarch64-linux`: `nix build`, the `unit` and
`integration-local` checks, and in a separate job `unit-logosctl` and
`integration-logosctl-local`, then builds `logosctl:smoke-portable` and runs the docker
smoke suite via `nix develop`.
`publish.yml` builds the sdist+wheel and publishes to PyPI via trusted publishing on
`v*` tags.

---

## Examples

### Local daemon

```python
from logoscore import LogoscoreDaemon

with LogoscoreDaemon(modules_dir="./modules") as daemon:
    client = daemon.client()
    client.load_module("chat")

    info = client.module_info("chat")
    print([m["name"] for m in info["methods"]])

    result = client.call("chat", "send_message", "hello world")
# Daemon stopped + temp config dir cleaned up on __exit__.
```

### Connect to an already-running daemon

```python
from logoscore import LogoscoreClient

client = LogoscoreClient()                       # default ~/.logoscore
print(client.status())
client.load_module("chat")

client = LogoscoreClient(config_dir="/custom/path")   # daemon started with --config-dir
```

### Event subscription round-trip

```python
def on_msg(event: dict) -> None:
    print(f"{event['event']}: {event['data']}")

sub = client.on_event("chat", "chat-message", on_msg)
try:
    ...
finally:
    sub.cancel()        # SIGINT → SIGTERM → SIGKILL, then joins the thread
```

### Operate a daemon on another machine (logosctl)

```python
from logosctl import LogosctlDaemon, RuntimeControl, runtime_control_config

with LogosctlDaemon("./modules", extra_config={"peering": runtime_control_config()}) as daemon, \
        RuntimeControl(daemon) as rc:
    rc.pair()
    rc.grant()                                   # DEFAULT_GRANTS
    print(rc.client().status())                  # runs with --remote
```

### Daemon in docker (logosctl)

```python
from logosctl import LogosctlDockerDaemon

with LogosctlDockerDaemon(
    image="logosctl:smoke-portable",
    modules_dir="./my-module/result/modules",   # host dir with your Qt plugins
    binary="logosctl",                          # the host-side client
) as daemon:
    client = daemon.client()
    client.load_module("my_module")
    print(client.call("my_module", "do_something", 42))
```

### Token provisioning (daemon-less)

```python
from logoscore import issue_token, revoke_token, list_tokens

token = issue_token("alice", config_dir="/path/to/daemon-cfg")
print(list_tokens(config_dir="/path/to/daemon-cfg"))   # [{"name": "alice", "issued_at": …}, …]
revoke_token("alice", config_dir="/path/to/daemon-cfg")
```

### ABI-safe module builds for the container

```bash
./tests/docker_smoke/build_modules_in_docker.sh ./build/modules \
    'github:user/my-module#packages.x86_64-linux.install-portable'
```

```python
from logosctl import build_modules_in_docker, LogosctlDockerDaemon

modules_dir = build_modules_in_docker(
    builds=[("github:user/my-module", "packages.x86_64-linux.install-portable")],
    output_dir="./build/modules",
)
with LogosctlDockerDaemon(image="logosctl:smoke-portable", modules_dir=modules_dir) as d:
    ...
```

### LogosResult / method matrix

`tests/_fullapi_module_cases.py::FULLAPI_METHOD_CASES` is the shared `(method, args,
expected)` matrix exercised against `test_fullapi_cpp` locally, through a peering
import, and in docker over Remote Runtime Control (its sibling `FULLAPI_EVENT_CASES`
does the same for one typed event per type). It pins, among others, that a
`LogosResult` round-trips as `{"success", "value", "error"}` (absent side is `null`):

```python
("makeResult", (True,),  {"success": True,  "value": {"ok": True, "provider": "test_fullapi_cpp"}, "error": None})
("makeResult", (False,), {"success": False, "value": None, "error": "deliberate error for testing"})
```

---

## Known Limitations

- **CLI must be on PATH.** The `logoscore` binary must be on `PATH` (or passed via
  `binary=` / `LOGOSCORE_BIN`). The package has no fallback and no C++ bindings.
- **Subprocess per call.** Every operation spawns a fresh `logoscore` subprocess and
  pays its startup cost — fine for testing/automation, not designed for high-throughput
  RPC.
- **Local clients only for `logoscore`.** A client reaches a daemon on its own host;
  operating one elsewhere is `logosctl`'s Remote Runtime Control.
- **`build_modules_in_docker` rejects local `path:` flake refs** — the host filesystem
  isn't mounted into the one-shot builder container; push to github and reference
  `github:…`, or build outside and pass `result/modules` directly.
- **Environment-gated tests.** Integration tests skip unless `LOGOSCORE_BIN` and
  `LOGOSCORE_TEST_MODULES_DIR` are set. Docker smoke tests skip without docker or the
  image, and cannot run inside the nix sandbox (no docker socket).
- **Host networking.** The docker smoke's host client reaches the daemon over the
  host's network, which is complete on Linux; Docker Desktop has it as an opt-in.
- **Container files are root-owned 0600.** Files the daemon writes under the container's
  `/config` must be read via `docker exec cat` (`read_container_file` / `state_json`),
  not direct host filesystem reads — a host-side `read_text()` hits `PermissionError`.
- **`pick_free_port()` is TOCTOU-racy** in theory (another process could grab the port
  before the caller rebinds) — fine at typical test concurrency.
- **ABI / flavor matching.** Modules compiled on macOS (`.dylib`) or with a mismatched
  glibc won't load in the Linux daemon container, and the flavor (`portable` / `dev`) of
  user modules must match the image flavor (`.install-portable` ↔ `portable`,
  `.install` ↔ `dev`).
