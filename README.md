# logos-logoscore-py

Python wrapper for the [`logosctl`](https://github.com/logos-co/logos-logoscore-cli)
CLI. Launch a daemon, load modules, call methods, and subscribe to events
from Python — without shelling out and parsing output by hand.

The wrapper is a thin layer over the `logosctl` CLI: every operation
spawns a `logosctl --json <subcommand>` subprocess and parses its output.
No C++ bindings, no IPC code.

`logosctl` has no daemon configuration flags. Module directories, the
persistence path and per-module listeners are a YAML document installed
into the session with `logosctl daemon config set` before the daemon
boots, and which daemon a client dials is a second document,
`<config_dir>/client/config.yaml`. The wrapper writes both for you; the
only per-call knobs left are the session dir and the token.

## Install

```bash
pip install logoscore
```

The PyPI distribution is named `logoscore`; the package it installs is
`logosctl` (`import logosctl`). It used to also ship a `logoscore` client
for the retired `logoscore` CLI. The `logosctl` binary must be on `PATH`.
See [logos-logoscore-cli](https://github.com/logos-co/logos-logoscore-cli)
for install instructions, or use the included Nix flake, which puts the
binary in the dev shell.

## Quickstart — local daemon

Installs a daemon config into an isolated session dir, then spawns
`logosctl daemon start` as a subprocess.

```python
from logosctl import LogosctlDaemon

with LogosctlDaemon(modules_dir="./modules") as daemon:
    client = daemon.client()

    client.load_module("chat")

    # List + introspect
    modules = client.list_modules(loaded=True)
    info = client.module_info("chat")
    print([m["name"] for m in info["methods"]])

    # Call a method
    result = client.call("chat", "send_message", "hello world")

    # Subscribe to events (callback runs on a background thread)
    def on_msg(event: dict) -> None:
        print(f"{event['event']}: {event['data']}")

    sub = client.on_event("chat", "chat-message", on_msg)
    try:
        ...
    finally:
        sub.cancel()
# Daemon stopped + temp session dir cleaned up on __exit__.
```

## Quickstart — daemon in docker

Use `LogosctlDockerDaemon` to run the daemon inside a container and drive
it over TCP. Good for testing your module against a real distributed
build of `logosctl` without polluting your dev environment, and for
anything that needs the daemon reachable from multiple processes.

```python
from logosctl import LogosctlDockerDaemon

with LogosctlDockerDaemon(
    image="logosctl:smoke-portable",
    modules_dir="./my-module/result/modules",  # host dir with your Qt plugins
) as daemon:
    client = daemon.client(binary="logosctl")  # host-side CLI
    client.load_module("my_module")
    print(client.call("my_module", "do_something", 42))
```

What the helper handles for you:

- Picks a free host TCP port per module; the container binds `6000`
  (`core_service`) and `6001` (`capability_module`) internally, reached
  via `-p $host_core:6000 -p $host_cap:6001`. Same pattern as
  [status-go tests-functional](https://github.com/status-im/status-go/tree/develop/tests-functional).
- Installs the daemon config before boot, in a throwaway
  `daemon config set /config/daemon.yaml` container over the same
  bind-mount. The document lists the image's own modules dir,
  `/user-modules` and any `extra_module_dirs`, sets `persistence_path`,
  declares a `0.0.0.0` listener for each module, and sets
  `insecure_tcp: true` for plaintext `tcp`. A rejected document raises
  `LogosctlError` with the CLI's message.
- Bind-mounts three host dirs into the container:
  `/config` (the session dir; the daemon writes `daemon/state.json` etc.
  there as root), `/persistence` (`persistence_path`; pre-seed for
  session restore, inspect after), `/user-modules` (your compiled Qt
  plugins, read-only).
- Waits for `daemon/state.json` to appear before returning.
- Returns a `LogosctlClient` bound to a separate, host-owned session dir
  whose `client/config.yaml` carries both modules' forwarded host ports
  (written via `LogosctlClient.write_config`) next to a copy of the
  daemon's boot token.

Building the image:
[`tests/logosctl/docker_smoke/build_smoke_image.sh`](tests/logosctl/docker_smoke/README.md)
produces `logosctl:smoke-portable`. The image contains only the CLI and
its bundled modules — user modules are always bind-mounted at runtime.

Knobs: `host_port=`, `persistence_dir=` (pre-seeded + not cleaned up on
exit), `codec="cbor"`, `transport="tcp_ssl"` (with `ssl_cert=` /
`ssl_key=`, plus `ssl_ca=` / `verify_peer=`), `extra_module_dirs=[...]`,
`extra_config={...}` (extra daemon-config keys), `extra_args=[...]`,
`container_name=`, `network=` (attach to a caller-managed docker
network), `env={...}` (env vars for the daemon process).
See `help(LogosctlDockerDaemon)` for the full list.

### Multiple daemons in a shared docker network

Pass `network=<name>` to attach each container to an EXISTING docker
network. The daemon never creates or removes networks — caller manages
the lifecycle. Use this when daemon containers need to discover each
other by container name via docker's embedded DNS:

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
        # alice resolves "bob" via docker DNS, and vice versa
        ...
finally:
    subprocess.run(["docker", "network", "rm", "my-net"])
```

## Connect to an already-running daemon

If a `logosctl` daemon is already running on the host (started with
`logosctl daemon start` from a shell, by a service manager, by another
tool, etc.), drop in a `LogosctlClient` directly — no `LogosctlDaemon`
needed.

```python
from logosctl import LogosctlClient

# Daemon in the default session, ~/.logosctl — no args.
client = LogosctlClient()
print(client.status())
client.load_module("chat")

# Call a Q_INVOKABLE method on a loaded module.
result = client.call("chat", "send_message", "hello world")
print(result)

# Daemon started with `logosctl --config-dir /custom/path daemon start`.
client = LogosctlClient(config_dir="/custom/path")
```

Every method spawns a `logosctl --json <subcommand>` subprocess and
parses its output. The wrapper only sets `LOGOSCTL_CONFIG_DIR` (when
`config_dir` is given) and `LOGOSCTL_TOKEN` (when `token` is given) on
that subprocess, which otherwise inherits your environment. The CLI reads
`<config_dir>/client/config.yaml` for the daemon endpoint and the token
file it names (`client/auto.json`); the daemon writes both into its
session on every boot, so you don't have to pass a token explicitly for a
same-host, same-user daemon.

Cross-host or different-user setups need the [Tokens](#tokens) flow. For
a daemon on **another host**, or any daemon whose session dir you aren't
pointing at, write the dial spec for its two well-known modules
(`core_service` and `capability_module`) with `LogosctlClient.connect(...)`:
see [Connect to a daemon on a remote host](#connect-to-a-daemon-on-a-remote-host).

## Connect to a daemon on a remote host

`LogosctlClient.connect(...)` builds a client from explicit per-module
endpoints, so you can reach a daemon that isn't on `localhost` (or whose
`core_service` / `capability_module` bound separate ports — two
`QTcpServer`s can't share an address:port). Each `DaemonEndpoint` is one
module's dial spec; pass the raw token the daemon issued for this client
(see [Tokens](#tokens)).

```python
from logosctl import LogosctlClient, DaemonEndpoint

client = LogosctlClient.connect(
    {
        "core_service":      DaemonEndpoint(transport="tcp", host="daemon.example.com", port=6000),
        "capability_module": DaemonEndpoint(transport="tcp", host="daemon.example.com", port=6001),
    },
    token="<raw-token-issued-for-this-client>",
)
print(client.status())
client.load_module("chat")
```

`connect()` materializes a `client/config.yaml` (plus an `auto.json`
holding the token) in a private temp dir that is cleaned up when the
client is garbage-collected. Pass `config_dir=...` to write it somewhere
you control and keep it around — but not into the daemon's own session:
a daemon rewrites its `client/config.yaml` on boot. The on-disk spec is
the whole story; `logosctl` has no client-side flag or env var that
could override it.

For TLS, use `transport="tcp_ssl"` and set `ca=` / `verify_peer=` per
endpoint (both only honoured for `tcp_ssl`). Verification without a `ca`
fails the handshake closed, so a dev setup with a self-signed cert wants
`verify_peer=False`:

```python
client = LogosctlClient.connect(
    {
        "core_service":      DaemonEndpoint("tcp_ssl", "daemon.example.com", 6000,
                                            verify_peer=True, ca="/etc/logos/tls/ca.pem"),
        "capability_module": DaemonEndpoint("tcp_ssl", "daemon.example.com", 6001,
                                            verify_peer=True, ca="/etc/logos/tls/ca.pem"),
    },
    token="...",
)
```

Need the config file without a client? `LogosctlClient.write_config(
config_dir, endpoints, token=...)` writes the same `client/config.yaml`
into a dir you own — the lower-level primitive `connect()` (and the
daemon helpers) are built on.

For a daemon you spawned, `daemon.remote_client(config_dir=None, ...)`
does this for you: it reads the bound endpoints back from `state.json`,
copies the daemon's boot token, and returns a client living in a separate
session dir. `daemon.endpoints()` returns just the `DaemonEndpoint`s.

## Transports

By default the daemon listens on a local Unix socket and the client
connects to it. To open remote-reachable transports, pass
`transports=[...]` to `LogosctlDaemon`; each protocol becomes one listener
per well-known module in the daemon config:

```python
with LogosctlDaemon(
    modules_dir="./modules",
    transports=["tcp"],          # or ["tcp_ssl"], or ["local", "tcp"]
    tcp_host="0.0.0.0",
    tcp_port=6000,               # core_service
    tcp_cap_port=6001,           # capability_module; 0 on both = daemon picks
    tcp_codec="json",            # or "cbor"
) as daemon:
    client = daemon.client()     # dials the per-module tcp endpoints the
                                 # wrapper read back from state.json
```

The daemon always keeps a local listener on every module, so naming
`local` is a no-op. A plaintext `tcp` listener on a non-loopback host gets
`insecure_tcp: true` added to the document, which the daemon requires
before it will bind one. TLS (`tcp_ssl`) needs `ssl_cert` / `ssl_key`,
and optionally `ssl_ca` and `verify_peer` (default `False`, for
self-signed certs).

`daemon.client()` needs no transport args: once the daemon is up, the
wrapper rebuilds `client/config.yaml` from `state.json` for `tcp` /
`tcp_ssl`, so `core_service` and `capability_module` each dial their own
bound port.

`client(transport=, tcp_host=, codec=, no_verify_peer=)` accepts uniform
overrides (host/transport/codec/verify are shared by both modules); they
rewrite the per-module `client/config.yaml` on disk, each module's own
port left intact. `logosctl` reads that file verbatim, with no env
override, so the file is the only way to retarget a client. There is
deliberately **no per-call port override**. To reach a daemon whose
modules sit on ports or a host you specify, use
[`LogosctlClient.connect(...)`](#connect-to-a-daemon-on-a-remote-host),
which writes a full per-module config. That's how `LogosctlDockerDaemon`
bridges the container boundary: it forwards each module to its own host
port and hands back a client wired to a per-module `client/config.yaml`.

## Daemon configuration

`LogosctlDaemon.start()` builds the daemon document from its arguments —
`modules_dirs` (from `modules_dir`), `persistence_path`, the `modules`
listeners, `insecure_tcp` — writes it to `<config_dir>/daemon.yaml`, and
installs it with `logosctl daemon config set`. Relative paths are
absolutised first, so `modules_dir="./modules"` means your cwd. The
installed copy is at `daemon.daemon_config_file`
(`<config_dir>/daemon/config.yaml`).

`extra_config={...}` merges extra top-level keys into the document last,
so they win: `access_policy` (a JSON *string*), `access_group`, `dirs`,
`logging`, and so on. The CLI allowlists keys, so a typo fails
`config set` and `start()` raises `LogosctlError` carrying the CLI's
message instead of booting a daemon without your setting.

## Tokens

The daemon issues a signed token for each authorised client; `logosctl`
authenticates the client's connection with that token. When you spawn a
daemon via `LogosctlDaemon`, `client()` picks up the boot token the
daemon writes to `client/auto.json`.

For daemons you didn't spawn (e.g. a long-running one, or one in a
container you want to share across several clients), manage tokens
directly. These operate on the session dir and need no running daemon:

```python
from logosctl import issue_token, revoke_token, list_tokens

issued = issue_token("alice", config_dir="/path/to/daemon-cfg")
raw = issued["token"]            # only time the raw value is returned
print(list_tokens(config_dir="/path/to/daemon-cfg"))
revoke_token("alice", config_dir="/path/to/daemon-cfg")
```

`issue_token` also takes `replace=True`, `local_only=True` (valid only
over the local socket) and `expires="30d"` (or an ISO 8601 UTC date).

## API overview

### `LogosctlDaemon`

Context manager that installs a daemon config and spawns
`logosctl daemon start` in an isolated `--config-dir` (temp dir by
default). Multiple daemons can run concurrently without colliding on
`~/.logosctl/daemon/`.

```python
LogosctlDaemon(
    modules_dir,              # str | Path | list — the `modules_dirs` config key
    *,
    binary="logosctl",
    config_dir=None,          # override to share state across instances
    persistence_path=None,    # the `persistence_path` config key
    extra_args=None,          # extra argv after `daemon start`
    extra_config=None,        # extra top-level daemon-config keys, merged last
    env=None,                 # extra env vars for the daemon process
    startup_timeout=30.0,     # seconds to wait for state.json + status
    # Transports (see section above)
    transports=None,          # ["tcp"] | ["tcp_ssl"] | ["local", "tcp"] | ...
    tcp_host="127.0.0.1",
    tcp_port=0,               # core_service; 0 = let daemon pick
    tcp_cap_port=0,           # capability_module's own port
    tcp_codec="json",         # "json" | "cbor"
    tcp_ssl_host="127.0.0.1",
    tcp_ssl_port=0,
    tcp_ssl_cap_port=0,
    tcp_ssl_codec="json",
    ssl_cert=None, ssl_key=None, ssl_ca=None,
    verify_peer=False,        # verify_peer in the tcp_ssl client spec
)
```

Also: `client()`, `remote_client()`, `endpoints()`, `logs()` (captured
stdout/stderr), `daemon_log()` (`<config_dir>/logs/daemon.log`), and the
`config_dir`, `state_file`, `daemon_config_file`, `client_token_file`,
`log_file` and `pid` properties.

### `LogosctlDockerDaemon`

Same shape, but the daemon runs inside a container. Construction just
stores config; `.start()` / `__enter__` installs the config and runs
`docker run`.

```python
LogosctlDockerDaemon(
    *,
    image,                    # e.g. "logosctl:smoke-portable"
    modules_dir,              # host dir → /user-modules inside container
    config_dir=None,          # defaults to tmpdir (cleaned up on stop)
    persistence_dir=None,     # defaults to tmpdir (cleaned up on stop)
    host_port=None,           # core_service's host port; None → pick_free_port()
    codec="json",             # "json" | "cbor"
    transport="tcp",          # "tcp" | "tcp_ssl"
    ssl_cert=None, ssl_key=None,  # mounted into the container for tcp_ssl
    ssl_ca=None,              # HOST path the client verifies against
    verify_peer=False,
    container_name=None,
    network=None,             # attach to existing caller-managed docker network
    extra_module_dirs=None,   # extra `modules_dirs` entries *inside* the container
    extra_config=None,        # extra top-level daemon-config keys, merged last
    extra_args=None,          # extra argv after `daemon start`
    env=None,                 # extra env vars for the daemon process
    startup_timeout=20.0,
)
```

Pass a caller-owned `persistence_dir` (or `config_dir`) to keep it
around after the container exits — useful for session-restore tests
(pre-seed → run → assert against what the modules wrote).

Files the daemon writes under `/config` are root-owned, so read them with
`read_container_file(path)`, `state_json()`, `instance_id` or
`daemon_log()` (all via `docker exec`), not from the host path.

Also exported: `docker_available()`, `image_present(image)`,
`pick_free_port()`, `CONTAINER_TCP_PORT` (= 6000), and
`build_modules_in_docker(builds, output_dir=...)`, which builds module
flakes inside a `nixos/nix` container so the plugins match the image's
ABI (remote flake refs only).

### `LogosctlClient`

Obtained via `daemon.client(...)`, `LogosctlClient.connect(endpoints,
token=...)` (a remote daemon — see
[Connect to a daemon on a remote host](#connect-to-a-daemon-on-a-remote-host)),
or constructed directly for a same-host daemon:
`LogosctlClient(binary="logosctl", *, config_dir=None, token=None, timeout=30.0)`.
Every method returns parsed JSON (dict or list) on success and raises on
failure:

| Method | CLI equivalent |
|---|---|
| `status()` | `logosctl status` |
| `list_modules(loaded=False)` | `logosctl module ls [--loaded]` |
| `module_info(name)` | `logosctl module show <name>` |
| `load_module(name)` | `logosctl module load <name>` |
| `unload_module(name)` | `logosctl module unload <name>` |
| `reload_module(name)` | `logosctl module reload <name>` |
| `call(module, method, *args)` | `logosctl call <module> <method> …` |
| `stats()` | `logosctl module stats` |
| `stop()` | `logosctl daemon stop` |
| `on_event(module, event, callback)` | `logosctl watch <module> --event <event>` |

`call(...)` returns the method's unwrapped `result` value, with the
`{"_bytes": …}` tagged form decoded to `bytes` (`decode_bytes=False`
keeps it). `Path` arguments are passed as `@file` so the CLI loads their
contents; `bytes`, lists, tuples and dicts travel as `json:` values. A
bare `--json` / `-j` / `--quiet` argument would be taken as a global
flag; pass it as `"str:--json"`.

### Events

```python
sub = client.on_event("chat", "chat-message", callback, error_callback=None)
sub.alive        # False once the watcher exits
sub.stderr_tail  # last lines the watcher wrote to stderr
sub.cancel()     # SIGINT → SIGTERM → SIGKILL
```

Pass `event=None` to receive every event from the module. The callback
runs on a daemon thread; exceptions, malformed events and a watcher that
exits non-zero are routed to `error_callback` (default: logged via
`logging`).

### Exceptions

`LogosctlError` is the base class. It carries `exit_code`, `stderr`,
`code` (the envelope verdict, e.g. `METHOD_FAILED`) and `detail_code`
(the failure class under it, e.g. `invalid_args`; read
`detail_code or code`). Subclasses map to the CLI's exit codes:

| Exit code | Exception |
|---|---|
| 2 | `DaemonNotRunningError` |
| 3 | `ModuleError` |
| 4 | `MethodError` |

Set `LOGOSCTL_PY_FORWARD_OUTPUT=1` to mirror the CLI's stderr (and, for
docker daemons, the container logs) to your stderr while debugging.

## Development

The repo ships a Nix flake that pulls `logosctl` from
`logos-logoscore-cli` (its `ctl` output), so tests run out of the box:

```bash
nix develop        # python + pytest + logosctl on PATH
pytest             # unit + integration; docker smoke skips without docker
nix flake check    # unit, integration and conformance checks, under nix
```

With docker running, a plain `pytest` also reaches the docker smoke
tests, which build their test modules in a container before checking for
the image; `--ignore=tests/logosctl/docker_smoke` leaves them out.

Test layout:

```
tests/
├── unit/              # conformance-table guards; no binary required
└── logosctl/
    ├── unit/          # client tests against a fake binary; runs anywhere
    ├── integration/   # spawns local logosctl daemons; nix checks cover this
    └── docker_smoke/  # docker-required; see tests/logosctl/docker_smoke/README.md
```

The integration suite skips unless `LOGOSCTL_BIN` and
`LOGOSCTL_TEST_MODULES_DIR` are set (the dev shell and the nix checks set
both). It runs once per transport: `pytest tests/logosctl/integration
--transport={local,tcp,tcp_ssl}`, or the `integration-local`,
`integration-tcp` and `integration-tcp_ssl` checks.

Docker smoke tests live in their own directory because they need the
host's docker socket (not available inside `nix build`). Run them
explicitly:

```bash
./tests/logosctl/docker_smoke/build_smoke_image.sh   # logosctl:smoke-portable
pytest tests/logosctl/docker_smoke -v
```

See [`tests/logosctl/docker_smoke/README.md`](tests/logosctl/docker_smoke/README.md)
for the docker-side details.

`conformance/run_matrix.py` drives the LIDL conformance matrix through
`LogosctlDaemon` (`--logosctl BIN`, default `$LOGOSCTL_BIN`, else
`logosctl`); the
`conformance-matrix` and `conformance-matrix-ext` checks run it, and CI
publishes the report.

Inside the [logos-workspace](https://github.com/logos-co/logos-workspace):

```bash
ws test logos-logoscore-py --auto-local
```

## Licence

Dual-licensed under MIT or Apache-2.0.
