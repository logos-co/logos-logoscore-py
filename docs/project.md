# logos-logoscore-py — Project Description

## Overview

`logos-logoscore-py` is published on PyPI as **`logoscore`** and installs the
**`logosctl`** Python package — a thin, dependency-free wrapper around the headless
[`logosctl`](https://github.com/logos-co/logos-logoscore-cli) CLI. Every operation
spawns a `logosctl --json <subcommand>` subprocess and parses its JSON stdout. There
are **no C++ bindings and no IPC code** in the package itself: all of the wire work
(local Unix socket, TCP, TCP+TLS; JSON / CBOR codecs; Qt Remote Objects under the
hood) lives in the CLI it drives.

It exists so test suites and Python tooling can drive a real Logos daemon — load
compiled Qt-plugin modules, invoke their `Q_INVOKABLE` methods, watch their events —
without shelling out and parsing text by hand. It is primarily a **testing and
automation surface**: what module authors use to smoke-test their plugins against a
real distributed build of `logosctl`, and what the platform uses to exercise the
full wire stack end-to-end (including the LIDL conformance matrix in `conformance/`).

`logosctl` takes no daemon configuration flags. Module directories, the persistence
path and per-module listeners are a YAML document installed into the session with
`logosctl daemon config set` before `daemon start`; which daemon a client dials is
`<config_dir>/client/config.yaml`. The wrapper generates both, and the only knobs a
client invocation takes are the session dir (`LOGOSCTL_CONFIG_DIR`) and the token
(`LOGOSCTL_TOKEN`).

### Place in the Logos platform

The daemon this package drives is the headless CLI runtime over `logos-liblogos`,
which hosts compiled Logos modules (process-isolated Qt plugins, or pure-C++ universal
modules). This package sits at the **frontend edge**, one hop above the CLI:

```
  logos-logoscore-py   (this repo — Python wrapper, PyPI `logoscore`, import `logosctl`)
        │  spawns `logosctl --json <subcommand>` subprocesses
        ▼
  logos-logoscore-cli  (the `logosctl` daemon + client CLI)
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
is the `logosctl` binary on `PATH`. The Nix flake propagates that binary and pulls in
`logos-test-modules` so the test suite runs out of the box.

### Three lifecycle flavors

| Class | What it drives | When to use |
|---|---|---|
| `LogosctlDaemon` | installs a daemon config into an isolated temp `--config-dir`, then spawns a local `logosctl daemon start` subprocess | fast local iteration, in-process tests; multiple daemons coexist without colliding on `~/.logosctl` |
| `LogosctlDockerDaemon` | runs the daemon inside a docker container, dials it over forwarded TCP ports | smoke-test a real distribution of `logosctl`, or anything needing the daemon reachable from multiple processes |
| `LogosctlClient` | connects to an already-running / remote daemon | a daemon started elsewhere (shell, service manager, container), or a multi-port/remote daemon via `LogosctlClient.connect()` |

---

## Project Structure

```
logos-logoscore-py/
├── pyproject.toml                  # hatchling build; distribution `logoscore` v0.1.0; no runtime deps
├── flake.nix                       # python package + docker bundle + dev shell + unit/integration/conformance checks
├── flake.lock
├── README.md                       # user-facing quickstart + API overview
├── LICENSE-MIT / LICENSE-APACHE-v2 # dual-licensed MIT OR Apache-2.0
│
├── src/logosctl/                   # the package
│   ├── __init__.py                 # public API re-exports + __all__; __version__ = "0.1.0"
│   ├── client.py                   # LogosctlClient, DaemonEndpoint, write_config/connect,
│   │                               #   arg coercion (_arg_to_str)
│   ├── daemon.py                   # LogosctlDaemon — daemon config document (YAML emitter,
│   │                               #   `daemon config set`), subprocess lifecycle,
│   │                               #   client/config.yaml rebuild from state.json
│   ├── docker_daemon.py            # LogosctlDockerDaemon + docker helpers + build_modules_in_docker
│   ├── events.py                   # Subscription — background-thread NDJSON pump over `logosctl watch`
│   ├── tokens.py                   # daemon-less issue_token / revoke_token / list_tokens
│   ├── errors.py                   # LogosctlError hierarchy + from_exit_code() exit-code mapping
│   └── _proc.py                    # internal run_json() + tagged-bytes encode/decode
│
├── conformance/
│   ├── run_matrix.py               # LIDL conformance matrix driver (LogosctlDaemon; --logosctl BIN)
│   └── matrix_report.py            # renders the matrix (text / HTML / merged page); measures nothing
│
├── tests/
│   ├── conftest.py                 # shared wire fixtures: transport, self_signed_cert,
│   │                               #   tcp_port/tcp_ssl_port; --transport / --docker-flavor
│   ├── _fullapi_module_cases.py    # FULLAPI_METHOD_CASES + FULLAPI_EVENT_CASES — shared matrices
│   ├── unit/                       # conformance-table guards (no binary needed)
│   │   ├── test_conformance_table_consistency.py  # inline full_api table vs the shared cases.json
│   │   ├── test_matrix_proxy_consumer.py          # --proxy-consumer spec parsing
│   │   ├── test_matrix_report.py                  # report rendering rules
│   │   └── test_matrix_table_keys.py              # unknown expectation keys fail the run
│   └── logosctl/
│       ├── conftest.py             # logosctl_bin, test_modules_dir (LOGOSCTL_BIN / LOGOSCTL_TEST_MODULES_DIR)
│       ├── unit/                   # fake binary; runs anywhere
│       │   ├── test_client_config.py      # daemon + client config documents, write_config/connect
│       │   ├── test_client_with_fake.py   # argv/env construction via monkeypatched subprocess.run
│       │   ├── test_error_detail_code.py  # envelope code vs failure-class detail_code
│       │   └── test_errors.py             # exit-code → exception mapping
│       ├── integration/            # spawns real local daemons; parametrised over --transport
│       │   ├── test_end_to_end.py         # status / list / load+call+event round-trip
│       │   ├── test_fullapi_module_cpp.py # test_fullapi_cpp — full param/return/event type surface
│       │   └── test_token_auth.py         # operator-issued token authorization per transport
│       └── docker_smoke/           # docker-required (can't run inside the nix sandbox)
│           ├── Dockerfile                 # multi-stage; stage 1 runs `nix build` in nixos/nix
│           ├── build_smoke_image.sh       # builds logosctl:smoke-portable
│           ├── conftest.py                # docker_flavor parametrisation + linux_test_modules_dir
│           ├── test_docker_smoke.py       # method+event matrix over TCP in json & cbor; two daemons; networks; env
│           ├── test_docker_ssl_smoke.py   # TLS smoke (self-signed cert, verification off)
│           └── README.md                  # image, build and run
│
├── docs/
│   ├── index.md
│   ├── spec.md                     # stack-agnostic spec (business logic, domain model, workflows)
│   └── project.md                  # this file
│
└── .github/workflows/
    ├── ci.yml                      # nix build + unit + integration-{local,tcp,tcp_ssl} + docker smoke
    ├── conformance.yml             # full + ext conformance tables, merged report on gh-pages
    └── publish.yml                 # PyPI trusted publishing on v* tags
```

---

## Technology Stack

| Component | Type | Purpose |
|---|---|---|
| Python ≥ 3.10 (3.10 / 3.11 / 3.12) | language | Wrapper implementation. Standard library only at runtime — `subprocess`, `json`, `threading`, `socket`, `tempfile`, `weakref`, `signal`, `base64`, `logging` |
| hatchling | build backend | PEP 517 build of the `logoscore` wheel (`tool.hatch.build.targets.wheel` → `src/logosctl`) |
| pytest ≥ 7 | test runner | Optional `[test]` extra; provided by the Nix dev shell and checks |
| Nix flakes | packaging | Reproducible package build, docker bundle, dev shell, and CI checks |
| Docker / buildx | tooling | Smoke tests; the daemon-in-a-container path and `build_modules_in_docker` |
| OpenSSL | tooling | `self_signed_cert` fixture for `tcp_ssl` integration / smoke tests |

### Runtime dependencies

The package declares **zero runtime Python dependencies** (`dependencies = []` in
`pyproject.toml`); the daemon config YAML is emitted by a small built-in writer
rather than PyYAML. Its one hard requirement is the `logosctl` CLI binary on `PATH`
(or supplied via the `binary=` kwarg / `LOGOSCTL_BIN` env in tests).

### Flake inputs

| Input | Purpose |
|---|---|
| `logos-nix` | Provides the shared nixpkgs pin (`nixpkgs.follows = "logos-nix/nixpkgs"`) |
| `logos-logoscore-cli` | The `logosctl` binary the package wraps. Its `ctl` package is `propagatedBuildInputs` of the Python package and on `PATH` in the dev shell and checks; `ctl-bundle-dir` feeds `logosctlDockerBundlePortable` |
| `logos-test-modules` | `test_fullapi_cpp` (universal C++, full param/return/event type surface) — the plugin the integration suite loads (`.install`); the Rust, ext and Qt-proxy providers plus `conformance/` (case tables, xfail registry) for the conformance checks |
| `nixpkgs` | `python3`, `hatchling`, `openssl`, `qt6.qtbase` for builds and checks |

---

## Components

Everything below is re-exported from `logosctl/__init__.py`. The package version is
`__version__ = "0.1.0"`.

### `LogosctlClient` (`client.py`)

A thin client around `logosctl` subcommands against a running daemon. Each method
spawns `logosctl --json <subcommand>` and parses the result.

```python
LogosctlClient(
    binary="logosctl", *,
    config_dir=None, token=None, timeout=30.0,
)
```

There are no transport kwargs: `logosctl` reads `client/config.yaml` verbatim and
honours no per-call override, so retargeting a client means writing a different dial
spec (`connect()` / `write_config()`).

Method → CLI subcommand map (`--json` always goes before the subcommand):

| Method | CLI subcommand | Returns |
|---|---|---|
| `status()` | `status` | `dict` |
| `stats()` | `module stats` | `Any` |
| `stop()` | `daemon stop` | `None` |
| `list_modules(*, loaded=False)` | `module ls [--loaded]` | `list[dict]` |
| `module_info(name)` | `module show <name>` | `dict` |
| `load_module(name)` | `module load <name>` | `dict` |
| `unload_module(name)` | `module unload <name>` | `dict` |
| `reload_module(name)` | `module reload <name>` | `dict` |
| `call(module, method, *args, timeout=None, decode_bytes=True)` | `call <module> <method> …` | unwrapped `result` value |
| `on_event(module, event, callback, *, error_callback=None)` | `watch <module> [--event <event>]` | `Subscription` |

`call(...)` details:
- **Argument coercion** (`_arg_to_str`): a `pathlib.Path` becomes `@<path>` so the CLI
  loads the file's contents; `bool` becomes `"true"`/`"false"`; `bytes`/`bytearray`,
  `list`/`tuple` and `dict` become `json:<JSON>`, with bytes (top-level or nested) in
  the tagged form `{"_bytes": "<base64url, unpadded>"}`; everything else is `str(arg)`
  for the CLI's own type coercion. A bare `--json`, `-j`, `--no-json`, `--human`, `-q`
  or `--quiet` argument is lifted out as a global flag by the CLI; pass
  `"str:--json"` instead.
- **Tagged-bytes decode** (`_proc.decode_bytes_tags`): the result is recursively
  scanned for `{"_bytes": …}` and decoded back to `bytes` — exactly once, at this
  boundary. `decode_bytes=False` returns the result verbatim.
- Returns the `result` field of the JSON envelope; raises `MethodError` (with `code`
  and `detail_code`) when the envelope reports `status == "error"`.

#### `LogosctlClient.connect(...)` (classmethod)

```python
@classmethod
def connect(
    endpoints: Mapping[str, DaemonEndpoint], *,
    token=None, binary="logosctl", config_dir=None,
    timeout=30.0, instance_id=None,
) -> LogosctlClient
```

Builds a client dialing a (possibly remote, possibly multi-port) daemon from explicit
per-module endpoints. Materializes `client/config.yaml` + the token file via
`write_config`; the on-disk spec is the whole story. When `config_dir` is `None` a
private temp dir is created and removed via `weakref.finalize` when the client is
collected; pass `config_dir` to keep it. Point it at a dir the daemon does **not**
own — a daemon rewrites its own `client/config.yaml` on boot.

#### `LogosctlClient.write_config(...)` (staticmethod)

```python
@staticmethod
def write_config(
    config_dir, endpoints: Mapping[str, DaemonEndpoint], *,
    token=None, instance_id=None, merge=False,
) -> None
```

The single source of truth for the on-disk `<config_dir>/client/config.yaml` (schema
**version 2**): `version`, `token_file` (default `auto.json`, always present — the CLI
treats a spec without one as absent), optional `instance_id`, and a `daemon` block with
one entry per well-known module. The document is written as JSON text, which YAML
reads back unchanged. Writes the raw `token` (wrapped as `{"token": …}`) to the file
named by `token_file`, with a fallback to `auto.json` when a merged config carries a
`token_file` that isn't a plain filename. `instance_id` is required for a `local` dial
from a foreign config dir. `merge=True` keeps pre-existing top-level keys when the
existing file parses as JSON.

### `DaemonEndpoint` (`client.py`)

```python
@dataclass(frozen=True)
class DaemonEndpoint:
    transport: str            # "tcp" | "tcp_ssl" | "local"
    host: str | None = None
    port: int | None = None
    codec: str = "json"
    verify_peer: bool | None = None
    ca: str | None = None     # CA bundle path, tcp_ssl only
```

One well-known module's dial spec, serialized into a single `daemon`-block entry.
`verify_peer` and `ca` are only emitted for `tcp_ssl`. These are the client's key
names; the daemon document spells the same listener `protocol` / `ca_file`.

### `LogosctlDaemon` (`daemon.py`)

Context manager that installs a daemon config and spawns `logosctl daemon start` in an
isolated `--config-dir`.

```python
LogosctlDaemon(
    modules_dir,                      # str | Path | list → `modules_dirs`
    *, binary="logosctl",
    config_dir=None, persistence_path=None,
    extra_args=None, extra_config=None, env=None, startup_timeout=30.0,
    transports=None,                  # ["tcp"] | ["tcp_ssl"] | ["local", "tcp"] | …
    tcp_host="127.0.0.1", tcp_port=0, tcp_cap_port=0, tcp_codec="json",
    tcp_ssl_host="127.0.0.1", tcp_ssl_port=0, tcp_ssl_cap_port=0, tcp_ssl_codec="json",
    ssl_cert=None, ssl_key=None, ssl_ca=None, verify_peer=False,
)
```

- **Config document** (`_daemon_config_document()`): `modules_dirs` and
  `persistence_path` (absolutised — the daemon resolves relative entries against its
  own cwd / the session, not the caller's); a `modules` block with one listener per
  (network protocol, well-known module), each module on its **own** port (`tcp_port` /
  `tcp_ssl_port` for `core_service`, `tcp_cap_port` / `tcp_ssl_cap_port` for
  `capability_module`, because two `QTcpServer`s can't share an address:port; `0` lets
  the daemon pick distinct ephemerals); `tcp_ssl` listeners carry absolutised
  `cert` / `key` / `ca_file`. `local` adds nothing — the daemon always binds a local
  listener. `extra_config` is merged last. `insecure_tcp: true` is added when the final
  document binds plaintext `tcp` on a non-loopback host, unless `extra_config` sets it.
  `modules_dirs`, `persistence_path`, `access_policy` (a JSON string), `access_group`
  and `insecure_tcp` are type-checked before submission, since a wrong type there
  aborts the daemon's loader instead of producing a config error.
- **Startup** (`start()` / `__enter__`): writes `<config_dir>/daemon.yaml` and runs
  `logosctl --config-dir <dir> daemon config set <file>`; a rejection raises
  `LogosctlError` carrying the CLI's message. Then runs
  `logosctl --config-dir <dir> daemon start [extra_args…]` in the foreground under
  `Popen` (stdout/stderr to `daemon.stdout.log` / `daemon.stderr.log`), waits for
  `daemon/state.json`, rebuilds `client/config.yaml` from the resolved endpoints (for
  `tcp`/`tcp_ssl`), then verifies with `status`. A daemon that dies or never becomes
  ready raises with its log tail attached.
- **Shutdown** (`stop(timeout=10.0)` / `__exit__`): runs `logosctl daemon stop`, then
  escalates `terminate()` → `kill()`, and removes the temp config dir it created. Safe
  to call repeatedly.
- **Properties**: `config_dir`, `state_file` (`<config_dir>/daemon/state.json`),
  `connection_file` (backward-compat alias for `state_file`), `daemon_config_file`
  (`<config_dir>/daemon/config.yaml`, the installed copy), `client_token_file`
  (`<config_dir>/client/auto.json`), `log_file` (`<config_dir>/logs/daemon.log`), `pid`.
- **`client(*, timeout=30.0, transport=None, tcp_host=None, no_verify_peer=False, codec=None)`** —
  returns a `LogosctlClient` on the daemon's own session and boot token. The optional
  overrides rebuild `client/config.yaml` from `state.json`, uniformly across both
  modules, each module's own port left intact. There is deliberately **no per-call port
  override**.
- **`remote_client(config_dir=None, *, transport=None, host=None, codec=None, verify_peer=None, timeout=30.0, binary=None)`** —
  a client in a **separate** session dir: endpoints read from `state.json`, the boot
  token copied, `instance_id` carried for `local` dials (which also need a shared
  `$TMPDIR`). Built on `LogosctlClient.connect`.
- **`endpoints(transport=None, *, host=None, codec=None, verify_peer=None)`** — the
  per-module `DaemonEndpoint`s from `state.json`; how a test learns ephemeral ports.
- **`logs() -> (stdout, stderr)`** — the captured `daemon.stdout.log` /
  `daemon.stderr.log`, falling back to `daemon_log()` when stdout is empty.
  **`daemon_log()`** reads `<config_dir>/logs/daemon.log`.

### `LogosctlDockerDaemon` (`docker_daemon.py`)

Context manager that `docker run`s the daemon and dials it over forwarded TCP ports.

```python
LogosctlDockerDaemon(
    *, image, modules_dir,
    config_dir=None, persistence_dir=None, host_port=None,
    codec="json", transport="tcp",
    ssl_cert=None, ssl_key=None, ssl_ca=None, verify_peer=False,
    container_name=None, network=None,
    extra_module_dirs=None, extra_config=None, extra_args=None, env=None,
    startup_timeout=20.0,
)
```

- `modules_dir` must exist and be a directory (`FileNotFoundError` /
  `NotADirectoryError`); `transport` must be `tcp` or `tcp_ssl` (`ValueError`), and
  `tcp_ssl` needs `ssl_cert` + `ssl_key`.
- **Config document**: `modules_dirs` = `/opt/logosctl/modules` (the image's own),
  `/user-modules`, then `extra_module_dirs`; `persistence_path: /persistence`; one
  `0.0.0.0` listener per well-known module on `CONTAINER_TCP_PORT` (6000) and
  `CONTAINER_CAP_TCP_PORT` (6001), with `/certs/cert.pem` / `/certs/key.pem` for
  `tcp_ssl`; `insecure_tcp: true` for plain `tcp`; then `extra_config`. Every path is
  the container's.
- **Startup**: picks the two host ports, checks `network` exists, writes the document
  to `<config_dir>/daemon.yaml` and installs it with a throwaway
  `docker run --rm … daemon config set /config/daemon.yaml`, then
  `docker run -d … daemon start [extra_args…]` (no `--rm`, so a failed start keeps its
  logs). `LOGOSCTL_CONFIG_DIR=/config` selects the session in both. Waits for
  `daemon/state.json` and the boot token via `docker exec`.
- Bind-mounts `/config` (session dir), `/persistence`, `/user-modules:ro`, and for
  `tcp_ssl` the cert and key as individual read-only files under `/certs`.
- Forwards each well-known module to its own dynamically-picked host port via
  `-p $host_core:6000 -p $host_cap:6001`.
- The host client lives in its own temp session dir (the bind-mounted `/config` is
  root-owned and rewritten by the daemon on boot): `client/config.yaml` with the
  forwarded ports plus `client/auto.json` with the daemon's boot token, copied out via
  `docker exec cat`.
- Daemon files under `/config` are root-owned with restrictive modes; read them via
  `read_container_file` / `container_file_exists` / `state_json` / `daemon_log` (which
  go through `docker exec`) rather than direct host reads.
- **Properties**: `host_port`, `config_dir`, `persistence_dir`, `container_id`,
  `container_name`, `instance_id`.
- **`client(*, binary="logosctl", timeout=30.0, tcp_host="localhost", codec=None, no_verify_peer=None)`** —
  rewrites the host client's `client/config.yaml` and returns a `LogosctlClient` via
  `LogosctlClient.connect(...)`. For `tcp_ssl`, `no_verify_peer` defaults to `True` (so
  a self-signed smoke cert connects); `no_verify_peer=False` uses the constructor's
  `verify_peer`, which needs `ssl_ca` (a host path) to succeed.
- **`stop()`**: `docker rm -f`, then removes the dirs it created; caller-supplied
  `config_dir` / `persistence_dir` are kept.

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
whose `$out/modules/<name>/…` matches the `modules_dirs` layout (e.g.
`packages.x86_64-linux.install-portable`). Returns the merged host modules dir. The
builder image is overridable via `builder_image=` or `LOGOSCTL_BUILDER_IMAGE` (read at
import). **Local `path:` flake refs are not supported** — the host filesystem isn't
mounted into the one-shot container, so push to github and reference `github:…`.

### `Subscription` (`events.py`)

A live event subscription backed by a `logosctl --json watch …` subprocess. A daemon
thread reads NDJSON from the watcher's stdout, decodes tagged bytes, and dispatches
each event dict to `callback`; a second thread drains stderr.

```python
Subscription.start(*, binary, args, config_dir, token, callback, error_callback, extra_env=None)
sub.alive         # False once the watcher exits
sub.stderr_tail   # last 50 lines of the watcher's stderr
sub.cancel(timeout=5.0)   # SIGINT → SIGTERM → SIGKILL
# also usable as a context manager (__enter__/__exit__ → cancel)
```

The callback runs on a daemon thread; exceptions, JSON-decode and bad-tag errors are
routed to `error_callback`, or logged via `logging` when none is given. A watcher that
exits non-zero without being cancelled is reported the same way, with its stderr tail.

### Tokens (`tokens.py`) — daemon-less

These read/write the session dir directly; no running daemon needed.

| Function | CLI subcommand | Returns |
|---|---|---|
| `issue_token(name, *, binary="logosctl", config_dir=None, replace=False, local_only=False, expires=None, timeout=30.0)` | `token issue --name <name> [--replace] [--local-only] [--expires D]` | `{"name", "token", "file", …}` (raises `ModuleError` if the name exists and `replace` is false) |
| `revoke_token(name, *, binary="logosctl", config_dir=None, timeout=30.0)` | `token revoke <name>` | `dict` (raises `ModuleError` on exit 3) |
| `list_tokens(*, binary="logosctl", config_dir=None, timeout=30.0)` | `token ls` | `[{"name", "issued_at", "expires_at", "local_only", "raw_file_present"}, …]` |

The daemon stores only a hash (`daemon/tokens.json`); the raw token is visible only in
the `issue_token` return value and the per-token file it points at
(`daemon/tokens/<name>.json`). A `local_only` token is rejected over `tcp`/`tcp_ssl`.
`expires` takes a relative duration (`30s`, `5m`, `2h`, `30d`) or an ISO 8601 UTC
date/timestamp.

### Errors (`errors.py`)

`LogosctlError(message, *, exit_code=None, stderr=None, code=None, detail_code=None)` is
the base class. `code` is the envelope verdict (`METHOD_FAILED` for every failed call);
`detail_code` is the failure class under it (`object_unavailable`, `timeout`,
`invalid_args`, `dispatch_failed`, …), `None` when the envelope has no `error` object.
`from_exit_code(code, message, *, stderr=None, error_code=None, detail_error_code=None)`
dispatches CLI exit codes to subclasses (unknown codes fall back to the base
`LogosctlError`):

| Exit code | Exception |
|---|---|
| 2 | `DaemonNotRunningError` |
| 3 | `ModuleError` |
| 4 | `MethodError` |

### Internal subprocess runner (`_proc.py`)

`run_json(binary, args, *, config_dir=None, token=None, env=None, timeout=30.0)` is the
single chokepoint: it builds `[binary, "--json", *args]`, sets `LOGOSCTL_CONFIG_DIR` and
`LOGOSCTL_TOKEN` on the subprocess env (plus any `env` overrides — the session is passed
by env because a `--config-dir` after a client subcommand would reach it as positional
arguments), runs it, maps a non-zero exit via `from_exit_code` (carrying the JSON
`code` and `error.code` from stdout when present), and parses stdout as a single JSON
value (`None` when empty). Setting `LOGOSCTL_PY_FORWARD_OUTPUT=1` (or
`true`/`yes`/`on`) mirrors the CLI's **stderr** (its qDebug/qWarning trail) to the
parent's stderr and adds `--verbose` to the invocation; stdout is deliberately **not**
forwarded (it may carry raw tokens). `LogosctlDockerDaemon.stop()` honours the same
switch for the container's logs.

---

## Domain Concepts

| Term | Meaning |
|---|---|
| **logosctl daemon** | the `logosctl daemon start` runtime that hosts Logos modules and exposes them over RPC; this package launches and dials it |
| **session (config dir)** | the directory a daemon and its clients act on — default `~/.logosctl`, else `--config-dir` / `LOGOSCTL_CONFIG_DIR`. Holds `daemon/`, `client/`, `logs/` and the session's module dirs |
| **daemon config document** | `<config_dir>/daemon/config.yaml`, installed by `logosctl daemon config set FILE`: `modules_dirs`, `persistence_path`, `modules` (per-module listeners: `protocol`, `host`, `port`, `codec`, `cert`, `key`, `ca_file`, `verify_peer`), `insecure_tcp`, `access_policy`, `access_group`, `dirs`, `logging`, … Keys are allowlisted |
| **well-known modules** | `core_service` and `capability_module` — always served by the daemon, each on its **own** listener/port. In docker: `core_service` → 6000, `capability_module` → 6001 |
| **`client/config.yaml` (v2)** | on-disk dial spec under `<config_dir>/client/`: `version`, `token_file`, `instance_id`, and a `daemon` block with one `DaemonEndpoint` per well-known module (`transport`, `host`, `port`, `codec`, `ca`, `verify_peer`). The only thing the CLI reads to decide what to dial |
| **`state.json`** | `<config_dir>/daemon/state.json`, written once every listener has bound; carries `instance_id`, `pid`, `started_at`, and resolved per-module transports. Its appearance signals daemon readiness |
| **token / `auto.json`** | the daemon issues a signed token per client. Its own boot token lands in `<config_dir>/client/auto.json`; named tokens' raw values in `daemon/tokens/<name>.json`; the hashed-at-rest list is `daemon/tokens.json` |
| **`Q_INVOKABLE`** | a C++/Qt module method exposed for RPC; `LogosctlClient.call(module, method, *args)` invokes one |
| **tagged-bytes** | logos-protocol's NUL-safe form for byte arrays crossing JSON, `{"_bytes": "<base64url>"}`; decoded once at the `call()` / event boundary |
| **`LogosResult`** | a module return struct serialized as `{"success": bool, "value": any, "error": any}`; pinned across the full-API matrix |
| **portable docker flavor** | the smoke image is built from `ctl-bundle-dir` (self-contained, matches released binaries); it is the only flavor. User modules must be built `.install-portable` to load in it |

---

## Building and Testing

### Workspace forms (preferred)

```bash
export PATH="/workspace/scripts:$PATH"

ws build logos-logoscore-py                 # build the python package
ws build logos-logoscore-py --auto-local    # build with local dep overrides
ws test  logos-logoscore-py                 # run the repo's nix checks
ws test  logos-logoscore-py --auto-local    # with local overrides
```

### Raw Nix

```bash
nix build                                  # default = logoscore-py, the python package
nix build .#logoscore-py                   # same package, explicit attr
nix build .#logosctlDockerBundlePortable   # self-contained CLI bundle for the Linux smoke image

nix develop                                # python + pytest + logosctl on PATH;
                                           # LOGOSCTL_BIN + LOGOSCTL_TEST_MODULES_DIR preset
```

The dev shell exports `LOGOSCTL_BIN` and `LOGOSCTL_TEST_MODULES_DIR` (the
`test_fullapi_cpp` `.install` modules) and prepends `src/` to `PYTHONPATH`, so `pytest`
works without extra setup.

### Nix checks

```bash
nix flake check                                     # every check below
nix build '.#checks.x86_64-linux.unit'              # pytest tests/unit tests/logosctl/unit (no daemon)
nix build '.#checks.x86_64-linux.integration-local'
nix build '.#checks.x86_64-linux.integration-tcp'
nix build '.#checks.x86_64-linux.integration-tcp_ssl'
nix build '.#checks.x86_64-linux.integration'       # back-compat alias = integration-local
nix build '.#checks.x86_64-linux.conformance-matrix'         # full_api table
nix build '.#checks.x86_64-linux.conformance-matrix-ext'     # full_api_ext table
nix build '.#checks.x86_64-linux.conformance-matrix-merged'  # one page over both
```

The integration suite (`pytest tests/logosctl/integration --transport=…`) is replicated
as three separate checks (one per transport) so CI can fan them out and a `tcp_ssl`
failure doesn't mask the `local`/`tcp` signal. The `unit` check points
`LOGOS_CONFORMANCE_DIR` at `logos-test-modules/conformance` so the table guards run in
the sandbox. The conformance checks run `conformance/run_matrix.py` against both
providers and every consumer, failing on a red cell, an `xpass` or a coverage gap, and
write text, JSONL and HTML reports into `$out`.

### pytest directly

```bash
pytest                                       # testpaths = tests (docker smoke skips without docker)
pytest tests/unit tests/logosctl/unit -v     # no logosctl binary required
pytest tests/logosctl/integration -v --transport=tcp   # also: --transport={local,tcp,tcp_ssl}
```

Integration tests **skip** unless `LOGOSCTL_BIN` and `LOGOSCTL_TEST_MODULES_DIR` are
set (the dev shell / nix checks set both); `tcp_ssl` additionally needs `openssl` on
`PATH` for the `self_signed_cert` fixture.

### Docker smoke tests

These need a docker socket and so cannot run inside the nix sandbox — they live in
`tests/logosctl/docker_smoke/` and run only in the dedicated CI steps.

```bash
./tests/logosctl/docker_smoke/build_smoke_image.sh   # logosctl:smoke-portable

pytest tests/logosctl/docker_smoke -v
nix develop --command pytest tests/logosctl/docker_smoke -v   # as CI runs it
```

The suite builds `test_fullapi_cpp` (`.install-portable`) inside docker once per
session via `build_modules_in_docker`, from `github:logos-co/logos-test-modules` unless
`LOGOSCTL_TEST_MODULES_FLAKE` overrides it.

### Distribution

```bash
python -m build         # sdist + wheel (publish.yml does this on v* tags → PyPI trusted publishing)
pip install logoscore   # installs `import logosctl`; the `logosctl` CLI must already be on PATH
```

### CI (`.github/workflows`)

`ci.yml` runs one `test` job on `x86_64-linux` and `aarch64-linux`: `nix build`, then
the `unit` and `integration-{local,tcp,tcp_ssl}` checks sequentially, then builds
`logosctl:smoke-portable` with buildx (GHA layer cache) and runs the docker smoke
suite via `nix develop`. `conformance.yml` runs the `conformance-matrix` and
`conformance-matrix-ext` checks, merges their reports into one page, and publishes it to
`gh-pages` (with a PR comment linking it). `publish.yml` builds the sdist+wheel and
publishes to PyPI via trusted publishing on `v*` tags.

---

## Examples

### Local daemon

```python
from logosctl import LogosctlDaemon

with LogosctlDaemon(modules_dir="./modules") as daemon:
    client = daemon.client()
    client.load_module("chat")

    info = client.module_info("chat")
    print([m["name"] for m in info["methods"]])

    result = client.call("chat", "send_message", "hello world")
# Daemon stopped + temp session dir cleaned up on __exit__.
```

### Extra daemon configuration

```python
from logosctl import LogosctlDaemon

with LogosctlDaemon(
    modules_dir="./modules",
    persistence_path="./state",
    extra_config={"signature_policy": "require"},   # any allowlisted top-level key
) as daemon:
    print(daemon.daemon_config_file.read_text())    # the installed document
```

`access_policy` is the one key whose value is a JSON *string*, not a mapping —
`json.dumps` it first.

### Connect to an already-running daemon

```python
from logosctl import LogosctlClient

client = LogosctlClient()                        # default session, ~/.logosctl
print(client.status())
client.load_module("chat")

client = LogosctlClient(config_dir="/custom/path")   # daemon started with --config-dir
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
from logosctl import LogosctlClient, DaemonEndpoint

client = LogosctlClient.connect(
    {
        "core_service":      DaemonEndpoint("tcp_ssl", "daemon.example.com", 6000,
                                            verify_peer=True, ca="/etc/logos/tls/ca.pem"),
        "capability_module": DaemonEndpoint("tcp_ssl", "daemon.example.com", 6001,
                                            verify_peer=True, ca="/etc/logos/tls/ca.pem"),
    },
    token="<raw-token-issued-for-this-client>",
)
print(client.status())
```

### Daemon in docker

```python
from logosctl import LogosctlDockerDaemon

with LogosctlDockerDaemon(
    image="logosctl:smoke-portable",
    modules_dir="./my-module/result/modules",   # host dir with your Qt plugins
) as daemon:
    client = daemon.client(binary="logosctl")
    client.load_module("my_module")
    print(client.call("my_module", "do_something", 42))
```

The equivalent by hand is two containers over one session dir — install the config,
then start:

```bash
mkdir -p config persistence
cat > config/daemon.yaml <<'EOF'
modules_dirs:
  - /opt/logosctl/modules
  - /user-modules
persistence_path: /persistence
modules:
  core_service:
    - protocol: tcp
      host: "0.0.0.0"
      port: 6000
      codec: json
  capability_module:
    - protocol: tcp
      host: "0.0.0.0"
      port: 6001
      codec: json
insecure_tcp: true
EOF

docker run --rm -v "$PWD/config":/config -e LOGOSCTL_CONFIG_DIR=/config \
    logosctl:smoke-portable daemon config set /config/daemon.yaml

docker run -d -p 6000:6000 -p 6001:6001 \
    -v "$PWD/config":/config \
    -v "$PWD/persistence":/persistence \
    -v "$PWD/my-modules/modules":/user-modules:ro \
    -e LOGOSCTL_CONFIG_DIR=/config \
    logosctl:smoke-portable daemon start
```

### Multiple daemons on a shared docker network

```python
import subprocess
from logosctl import LogosctlDockerDaemon

subprocess.run(["docker", "network", "create", "my-net"], check=True)
try:
    a = LogosctlDockerDaemon(image="logosctl:smoke-portable",
                             modules_dir="./my-module/result/modules",
                             container_name="alice", network="my-net")
    b = LogosctlDockerDaemon(image="logosctl:smoke-portable",
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
from logosctl import issue_token, revoke_token, list_tokens

issued = issue_token("alice", config_dir="/path/to/daemon-cfg", expires="30d")
raw = issued["token"]
print(list_tokens(config_dir="/path/to/daemon-cfg"))   # [{"name": "alice", "issued_at": …}, …]
revoke_token("alice", config_dir="/path/to/daemon-cfg")
```

### ABI-safe module builds for the container

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
expected)` matrix exercised against `test_fullapi_cpp` over every transport and codec
(its sibling `FULLAPI_EVENT_CASES` does the same for one typed event per type). It
pins, among others, that a `LogosResult` round-trips as `{"success", "value",
"error"}` (absent side is `null`):

```python
("makeResult", (True,),  {"success": True,  "value": {"ok": True, "provider": "test_fullapi_cpp"}, "error": None})
("makeResult", (False,), {"success": False, "value": None, "error": "deliberate error for testing"})
```

---

## Known Limitations

- **CLI must be on PATH.** The `logosctl` binary must be on `PATH` (or passed via
  `binary=` / `LOGOSCTL_BIN`). The package has no fallback and no C++ bindings.
- **Subprocess per call.** Every operation spawns a fresh `logosctl` subprocess and
  pays its startup cost — fine for testing/automation, not designed for high-throughput
  RPC.
- **The dial spec is a file.** `logosctl` has no per-call connection override at all,
  so every retarget rewrites `client/config.yaml`; there is no per-call port override,
  and multi-port or remote daemons go through `connect()` / `write_config`.
- **Configuration is installed before boot.** A daemon setting the CLI rejects fails
  `start()` with the CLI's message rather than being silently dropped, and there is no
  way to change a running daemon's configuration — stop it and start a new one.
- **`build_modules_in_docker` rejects local `path:` flake refs** — the host filesystem
  isn't mounted into the one-shot builder container; push to github and reference
  `github:…`, or build outside and pass `result/modules` directly.
- **Environment-gated tests.** Integration tests skip unless `LOGOSCTL_BIN` and
  `LOGOSCTL_TEST_MODULES_DIR` are set; `tcp_ssl` also needs `openssl`. Docker smoke
  tests skip without docker or the image (the TLS smoke also without `openssl`), and
  cannot run inside the nix sandbox (no docker socket).
- **Container files are root-owned.** Files the daemon writes under the container's
  `/config` must be read via `docker exec cat` (`read_container_file` / `state_json`),
  not direct host filesystem reads — a host-side `read_text()` hits `PermissionError`.
- **`pick_free_port()` is TOCTOU-racy** in theory (another process could grab the port
  before the caller rebinds) — fine at typical test concurrency.
- **ABI / flavor matching.** Modules compiled on macOS (`.dylib`) or with a mismatched
  glibc won't load in the Linux daemon container, and the image is portable-only, so
  user modules must be `.install-portable` builds.
