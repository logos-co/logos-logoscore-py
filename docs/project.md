# logos-logoscore-py — Project Description

## Overview

`logos-logoscore-py` is the **`logoscore` PyPI package** — a thin, dependency-free
Python wrapper around the headless [`logoscore`](https://github.com/logos-co/logos-logoscore-cli)
CLI. Every operation spawns a `logoscore <subcommand> --json` subprocess and parses
its JSON stdout. There are **no C++ bindings and no IPC code** in the package itself:
all of the wire work (local Unix socket, TCP, TCP+TLS; JSON / CBOR codecs; Qt Remote
Objects under the hood) lives in the CLI it drives.

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
  modules              (logos-test-modules: test_basic_module [Qt],
                        test_basic_module_cpp [pure-C++], capability_module, …)
```

Because the wrapper only ever speaks to the CLI, its sole external runtime requirement
is the `logoscore` binary on `PATH`. The Nix flake propagates that binary and pulls in
`logos-test-modules` so the test suite runs out of the box.

### Three lifecycle flavors

| Class | What it drives | When to use |
|---|---|---|
| `LogoscoreDaemon` | spawns a local `logoscore -D` subprocess with an isolated temp `--config-dir` | fast local iteration, in-process tests; multiple daemons coexist without colliding on `~/.logoscore` |
| `LogoscoreDockerDaemon` | runs the daemon inside a docker container, dials it over forwarded TCP ports | smoke-test a real distribution of `logoscore`, or anything needing the daemon reachable from multiple processes |
| `LogoscoreClient` | connects to an already-running / remote daemon | a daemon started elsewhere (shell, service manager, container), or a multi-port/remote daemon via `LogoscoreClient.connect()` |

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
│   ├── daemon.py                   # LogoscoreDaemon — local subprocess lifecycle, transport-flag
│   │                               #   construction, client/config.json rewrite from state.json
│   ├── docker_daemon.py            # LogoscoreDockerDaemon + docker helpers + build_modules_in_docker
│   ├── events.py                   # Subscription — background-thread NDJSON pump over `logoscore watch`
│   ├── tokens.py                   # daemon-less issue_token / revoke_token / list_tokens
│   ├── errors.py                   # LogoscoreError hierarchy + from_exit_code() exit-code mapping
│   └── _proc.py                    # internal run_json(): builds argv, sets env, parses JSON, maps failures
│
├── tests/
│   ├── conftest.py                 # fixtures: logoscore_bin, test_modules_dir, transport,
│   │                               #   self_signed_cert, tcp_port/tcp_ssl_port; --transport / --docker-flavor
│   ├── _basic_module_cases.py      # BASIC_MODULE_CASES — shared (method, args, expected) matrix
│   ├── unit/                       # no logoscore needed (runs anywhere)
│   │   ├── test_client_config.py   #   write_config serialization + connect()/client() env contract
│   │   ├── test_client_with_fake.py#   argv/env construction via monkeypatched subprocess.run
│   │   └── test_errors.py          #   exit-code → exception mapping
│   ├── integration/                # spawns real local daemons; parametrised over --transport
│   │   ├── test_end_to_end.py      #   status / list / load+call+event round-trip
│   │   ├── test_basic_module_methods.py     #   test_basic_module (Qt) method matrix
│   │   └── test_basic_module_cpp_methods.py #   test_basic_module_cpp (pure-C++) method matrix
│   └── docker_smoke/               # docker-required (can't run inside the nix sandbox)
│       ├── Dockerfile              #   multi-stage; stage 1 runs `nix build` in nixos/nix
│       ├── build_smoke_image.sh    #   builds logoscore:smoke-{portable,dev} (FLAVOR=…)
│       ├── build_modules_in_docker.sh  # shell wrapper over build_modules_in_docker()
│       ├── conftest.py             #   docker-flavor fixtures + image/skip gating
│       ├── test_docker_smoke.py    #   method+event matrix over TCP in json & cbor; two-daemon test
│       ├── test_docker_ssl_smoke.py#   TLS smoke (self-signed cert, --no-verify-peer)
│       └── README.md               #   image flavors, mount layout, port strategy
│
├── docs/
│   ├── index.md
│   ├── spec.md                     # stack-agnostic spec (business logic, domain model, workflows)
│   └── project.md                  # this file
│
└── .github/workflows/
    ├── ci.yml                      # nix build + unit + integration-{local,tcp,tcp_ssl} + docker smoke
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
| Docker / buildx | tooling | Smoke tests; the daemon-in-a-container path and `build_modules_in_docker` |
| OpenSSL | tooling | `self_signed_cert` fixture for `tcp_ssl` integration / smoke tests |

### Runtime dependencies

The package declares **zero runtime Python dependencies** (`dependencies = []` in
`pyproject.toml`). Its one hard requirement is the `logoscore` CLI binary on `PATH`
(or supplied via the `binary=` kwarg / `LOGOSCORE_BIN` env in tests).

### Flake inputs

| Input | Purpose |
|---|---|
| `logos-nix` | Provides the shared nixpkgs pin (`nixpkgs.follows = "logos-nix/nixpkgs"`) |
| `logos-logoscore-cli` | The `logoscore` daemon/CLI binary the package wraps. Its `default` package is `propagatedBuildInputs` of the wheel, and `cli-bundle-dir` feeds the portable docker bundle |
| `logos-test-modules` | `test_basic_module` (Qt) and `test_basic_module_cpp` (pure-C++) plugins used by the integration/smoke suites (via `.install` / `.install-portable`) |
| `nixpkgs` | `python3`, `hatchling`, `openssl`, `qt6.qtbase` for builds and checks |

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
    transport=None, tcp_host=None, tcp_port=None,
    no_verify_peer=False, codec=None,
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

Transport kwargs (`transport`, `tcp_host`, `tcp_port`, `no_verify_peer`, `codec`) are
turned into `LOGOSCORE_CLIENT_*` env vars on the subprocess (`_env_overrides()`):
`LOGOSCORE_CLIENT_TRANSPORT`, `LOGOSCORE_CLIENT_TCP_HOST`, `LOGOSCORE_CLIENT_TCP_PORT`,
`LOGOSCORE_CLIENT_NO_VERIFY_PEER`, `LOGOSCORE_CLIENT_CODEC`.

#### `LogoscoreClient.connect(...)` (classmethod)

```python
@classmethod
def connect(
    endpoints: Mapping[str, DaemonEndpoint], *,
    token=None, binary="logoscore", config_dir=None,
    timeout=30.0, instance_id=None,
) -> LogoscoreClient
```

Builds a client dialing a (possibly remote, possibly multi-port) daemon from explicit
per-module endpoints. Materializes `client/config.json` + the token file via
`write_config`, and sets **no** `LOGOSCORE_CLIENT_*` env overrides — the on-disk spec
is authoritative. This is the only way to reach a daemon whose `core_service` and
`capability_module` listen on different ports. When `config_dir` is `None` a private
temp dir is created and removed via `weakref.finalize` when the client is collected;
pass `config_dir` to keep it.

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
an unsafe `token_file`. `merge=True` preserves pre-existing top-level keys (the daemon
helpers use it to patch the daemon's auto-emitted file).

### `DaemonEndpoint` (`client.py`)

```python
@dataclass(frozen=True)
class DaemonEndpoint:
    transport: str            # "tcp" | "tcp_ssl" | "local"
    host: str | None = None
    port: int | None = None
    codec: str = "json"
    verify_peer: bool | None = None
```

One well-known module's dial spec, serialized into a single `daemon`-block entry.
`verify_peer` is only emitted for `tcp_ssl`.

### `LogoscoreDaemon` (`daemon.py`)

Context manager that spawns `logoscore -D` with an isolated `--config-dir`.

```python
LogoscoreDaemon(
    modules_dir,                      # str | Path | list — one or more -m dirs
    *, binary="logoscore",
    config_dir=None, persistence_path=None,
    extra_args=None, env=None, startup_timeout=15.0,
    transports=None,                  # ["tcp"] | ["tcp_ssl"] | ["local", "tcp"] | …
    tcp_host="127.0.0.1", tcp_port=0, tcp_cap_port=0, tcp_codec="json",
    tcp_ssl_host="127.0.0.1", tcp_ssl_port=0, tcp_ssl_cap_port=0, tcp_ssl_codec="json",
    ssl_cert=None, ssl_key=None, ssl_ca=None, verify_peer=False,
)
```

- **Startup** (`start()` / `__enter__`): builds the command
  `logoscore -D --config-dir <dir> -m <dir>… [--persistence-path …] [--module-transport …]`,
  emitting one `--module-transport NAME=PROTOCOL[,k=v…]` per (protocol, well-known
  module) pair. Each well-known module rides its **own** port (`tcp_port`/`tcp_ssl_port`
  for `core_service`, `tcp_cap_port`/`tcp_ssl_cap_port` for `capability_module`) because
  two `QTcpServer`s can't share an address:port. Waits for `daemon/state.json`, rewrites
  `client/config.json` from the resolved transports (for `tcp`/`tcp_ssl`), then verifies
  with `status`.
- **Shutdown** (`stop(timeout=10.0)` / `__exit__`): runs `logoscore stop`, then escalates
  `terminate()` → `kill()`, and removes the temp config dir it created. Safe to call
  repeatedly.
- **Properties**: `config_dir`, `state_file` (`<config_dir>/daemon/state.json`),
  `connection_file` (backward-compat alias for `state_file`), `client_token_file`
  (`<config_dir>/client/auto.json`), `pid`.
- **`client(*, timeout=30.0, transport=None, tcp_host=None, no_verify_peer=False, codec=None)`** —
  returns a `LogoscoreClient` reading the daemon's per-module `client/config.json`. With
  no transport kwargs it sets **no** `LOGOSCORE_CLIENT_*` env overrides. The optional
  overrides are merged **into** the on-disk spec in place (uniformly across both modules,
  each module's own port left intact). There is deliberately **no per-call port
  override** — a single `LOGOSCORE_CLIENT_TCP_PORT` would collapse `capability_module`
  onto `core_service`'s port.
- **`logs() -> (stdout, stderr)`** — the daemon's captured `daemon.stdout.log` /
  `daemon.stderr.log`.

### `LogoscoreDockerDaemon` (`docker_daemon.py`)

Context manager that `docker run`s the daemon and dials it over forwarded TCP ports.

```python
LogoscoreDockerDaemon(
    *, image, modules_dir,
    config_dir=None, persistence_dir=None, host_port=None,
    codec="json", transport="tcp",
    ssl_cert=None, ssl_key=None, verify_peer=False,
    container_name=None, network=None,
    extra_module_dirs=None, extra_args=None, startup_timeout=20.0,
)
```

- Bind-mounts three host dirs: `/config` (daemon writes `state.json` etc.),
  `/persistence` (`--persistence-path`), `/user-modules:ro` (your compiled plugins).
- Forwards each well-known module to its own dynamically-picked host port: container
  `core_service` on `CONTAINER_TCP_PORT` (6000) and `capability_module` on
  `CONTAINER_CAP_TCP_PORT` (6001), via `-p $host_core:6000 -p $host_cap:6001`.
- Daemon files under `/config` are root-owned 0600; read them via `read_container_file`
  / `container_file_exists` / `state_json` (which go through `docker exec … cat`) rather
  than direct host reads.
- **Properties**: `host_port`, `config_dir`, `persistence_dir`, `container_id`,
  `container_name`, `instance_id`, `state_json()`.
- **`client(*, binary="logoscore", timeout=30.0, tcp_host="localhost", codec=None, no_verify_peer=None)`** —
  returns a `LogoscoreClient` via `LogoscoreClient.connect(...)` wired to the forwarded
  host ports. For `tcp_ssl`, `no_verify_peer` defaults to `True` (so a self-signed smoke
  cert connects); `no_verify_peer=False` exercises the verify path against the
  constructor's `verify_peer`.

Module-level helpers (also re-exported): `docker_available() -> bool`,
`image_present(image) -> bool`, `pick_free_port() -> int`, and the constant
`CONTAINER_TCP_PORT = 6000`.

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
whose `$out/modules/<name>/…` matches the daemon's `-m` layout (e.g.
`packages.x86_64-linux.install-portable`). Returns the merged host modules dir. The
builder image is overridable via `LOGOSCORE_BUILDER_IMAGE`. **Local `path:` flake refs
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
| **well-known modules** | `core_service` and `capability_module` — always served by the daemon, each on its **own** listener/port. In docker: `core_service` → 6000, `capability_module` → 6001 |
| **`client/config.json` (v2)** | on-disk dial spec under `<config_dir>/client/`; a `daemon` block with one `DaemonEndpoint` per well-known module. Authoritative, and preferred over `LOGOSCORE_CLIENT_*` env overrides (which apply uniformly to all modules) |
| **`state.json`** | `<config_dir>/daemon/state.json`, written post-bind; carries `instance_id`, `pid`, `started_at`, and resolved per-module transports. Its appearance signals daemon readiness |
| **token / `auto.json`** | the daemon issues a signed token per client. The raw local-client token lands in `<config_dir>/client/auto.json`; the hashed-at-rest list is `<config_dir>/daemon/tokens.json` |
| **`Q_INVOKABLE`** | a C++/Qt module method exposed for RPC; `LogoscoreClient.call(module, method, *args)` invokes one |
| **tagged-bytes** | logos-protocol's NUL-safe form for byte arrays crossing JSON, `{"_bytes": "<base64url>"}`; decoded once at the `call()` boundary |
| **`LogosResult`** | a module return struct serialized as `{"success": bool, "value": any, "error": any}`; pinned across the basic-module matrix |
| **portable vs dev docker flavor** | `portable` = self-contained `cli-bundle-dir` (~600 MB, matches released binaries, default); `dev` = nix-store-rpath-linked (~3 GB, needs `/nix/store` in image). User modules must match: `.install-portable` vs `.install` |

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
nix build .#dockerBundlePortable   # self-contained CLI bundle for the smoke image (Linux)
nix build .#dockerBundle           # dev (nix-store-linked) bundle (Linux)

nix develop                        # python + pytest + logoscore on PATH;
                                   # LOGOSCORE_BIN + LOGOSCORE_TEST_MODULES_DIR[_PORTABLE] preset
```

The dev shell exports `LOGOSCORE_BIN`, `LOGOSCORE_TEST_MODULES_DIR` (dev `.install`
modules) and `LOGOSCORE_TEST_MODULES_DIR_PORTABLE` (`.install-portable`), and prepends
`src/` to `PYTHONPATH`, so `pytest` works without extra setup.

### Nix checks

```bash
nix flake check                                   # unit + all three integration transports
nix build '.#checks.x86_64-linux.unit'            # unit only (no daemon)
nix build '.#checks.x86_64-linux.integration-local'
nix build '.#checks.x86_64-linux.integration-tcp'
nix build '.#checks.x86_64-linux.integration-tcp_ssl'
nix build '.#checks.x86_64-linux.integration'     # back-compat alias = integration-local
```

The integration suite is replicated as three separate checks (one per transport) so CI
can fan them out and a `tcp_ssl` failure doesn't mask the `local`/`tcp` signal.

### pytest directly

```bash
pytest                                  # testpaths = tests (unit + integration; docker skipped)
pytest tests/unit -v                    # no logoscore binary required
pytest tests/integration -v --transport=tcp     # also: --transport={local,tcp,tcp_ssl}
```

Integration tests **skip** unless `LOGOSCORE_BIN` and `LOGOSCORE_TEST_MODULES_DIR` are
set (the dev shell / nix checks set both); `tcp_ssl` additionally needs `openssl` on
`PATH` for the `self_signed_cert` fixture.

### Docker smoke tests

These need a docker socket and so cannot run inside the nix sandbox — they live in
`tests/docker_smoke/` and run only in the dedicated CI step.

```bash
./tests/docker_smoke/build_smoke_image.sh        # FLAVOR=portable (default)
FLAVOR=dev  ./tests/docker_smoke/build_smoke_image.sh
FLAVOR=both ./tests/docker_smoke/build_smoke_image.sh

pytest tests/docker_smoke -v --docker-flavor=portable   # also: dev | both
nix develop --command pytest tests/docker_smoke -v       # as CI runs it
```

### Distribution

```bash
python -m build         # sdist + wheel (publish.yml does this on v* tags → PyPI trusted publishing)
pip install logoscore   # the `logoscore` CLI must already be on PATH
```

### CI (`.github/workflows`)

`ci.yml` runs on `x86_64-linux` and `aarch64-linux`: `nix build`, then the `unit` and
`integration-{local,tcp,tcp_ssl}` checks sequentially, then builds
`logoscore:smoke-portable` and runs the docker smoke suite via `nix develop`.
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

### Remote / multi-port daemon

```python
from logoscore import LogoscoreClient, DaemonEndpoint

client = LogoscoreClient.connect(
    {
        "core_service":      DaemonEndpoint("tcp_ssl", "daemon.example.com", 6000, verify_peer=True),
        "capability_module": DaemonEndpoint("tcp_ssl", "daemon.example.com", 6001, verify_peer=True),
    },
    token="<raw-token-issued-for-this-client>",
)
print(client.status())
```

### Daemon in docker

```python
from logoscore import LogoscoreDockerDaemon

with LogoscoreDockerDaemon(
    image="logoscore:smoke-portable",
    modules_dir="./my-module/result/modules",   # host dir with your Qt plugins
) as daemon:
    client = daemon.client(binary="logoscore")
    client.load_module("my_module")
    print(client.call("my_module", "do_something", 42))
```

Equivalent raw `docker run` (from `tests/docker_smoke/README.md`):

```bash
docker run --rm -p 6000:6000 \
    -v "$PWD/config":/config \
    -v "$PWD/persistence":/persistence \
    -v "$PWD/my-modules/modules":/user-modules:ro \
    logoscore:smoke-portable \
    daemon --config-dir /config --persistence-path /persistence \
           --transport tcp --tcp-host 0.0.0.0 --tcp-port 6000 \
           -m /opt/logoscore/modules -m /user-modules
```

### Multiple daemons on a shared docker network

```python
import subprocess
from logoscore import LogoscoreDockerDaemon

subprocess.run(["docker", "network", "create", "my-net"], check=True)
try:
    a = LogoscoreDockerDaemon(image="logoscore:smoke-portable",
                              modules_dir="./my-module/result/modules",
                              container_name="alice", network="my-net")
    b = LogoscoreDockerDaemon(image="logoscore:smoke-portable",
                              modules_dir="./my-module/result/modules",
                              container_name="bob", network="my-net")
    with a, b:
        # alice resolves "bob" via docker's embedded DNS, and vice versa
        ...
finally:
    subprocess.run(["docker", "network", "rm", "my-net"])
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
from logoscore import build_modules_in_docker, LogoscoreDockerDaemon

modules_dir = build_modules_in_docker(
    builds=[("github:user/my-module", "packages.x86_64-linux.install-portable")],
    output_dir="./build/modules",
)
with LogoscoreDockerDaemon(image="logoscore:smoke-portable", modules_dir=modules_dir) as d:
    ...
```

### LogosResult / method matrix

`tests/_basic_module_cases.py::BASIC_MODULE_CASES` is the shared `(method, args,
expected)` matrix exercised against `test_basic_module` over every transport and codec.
It pins, among others, that a `LogosResult` round-trips as `{"success", "value",
"error"}`:

```python
("successResult", (),         {"success": True,  "value": "operation succeeded", "error": None})
("errorResult",   (),         {"success": False, "value": None, "error": "deliberate error for testing"})
("validateInput", ("hello",), {"success": True,  "value": {"input": "hello", "length": 5}, "error": None})
```

---

## Known Limitations

- **CLI must be on PATH.** The `logoscore` binary must be on `PATH` (or passed via
  `binary=` / `LOGOSCORE_BIN`). The package has no fallback and no C++ bindings.
- **Subprocess per call.** Every operation spawns a fresh `logoscore` subprocess and
  pays its startup cost — fine for testing/automation, not designed for high-throughput
  RPC.
- **No per-call port override, by design.** The CLI applies a single
  `LOGOSCORE_CLIENT_TCP_PORT` uniformly to all modules, which would collapse
  `capability_module` onto `core_service`'s port. Multi-port daemons must use
  `connect()` / `write_config` (a full per-module config file).
- **`build_modules_in_docker` rejects local `path:` flake refs** — the host filesystem
  isn't mounted into the one-shot builder container; push to github and reference
  `github:…`, or build outside and pass `result/modules` directly.
- **Environment-gated tests.** Integration tests skip unless `LOGOSCORE_BIN` and
  `LOGOSCORE_TEST_MODULES_DIR` are set; `tcp_ssl` also needs `openssl`. Docker smoke
  tests skip without docker (and the TLS smoke skips without `openssl`), and cannot run
  inside the nix sandbox (no docker socket).
- **Container files are root-owned 0600.** Files the daemon writes under the container's
  `/config` must be read via `docker exec cat` (`read_container_file` / `state_json`),
  not direct host filesystem reads — a host-side `read_text()` hits `PermissionError`.
- **`pick_free_port()` is TOCTOU-racy** in theory (another process could grab the port
  before the caller rebinds) — fine at typical test concurrency.
- **ABI / flavor matching.** Modules compiled on macOS (`.dylib`) or with a mismatched
  glibc won't load in the Linux daemon container, and the flavor (`portable` / `dev`) of
  user modules must match the image flavor (`.install-portable` ↔ `portable`,
  `.install` ↔ `dev`).
