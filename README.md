# logos-logoscore-py

Python wrapper for the [`logoscore`](https://github.com/logos-co/logos-logoscore-cli)
CLI. Launch a daemon, load modules, call methods, and subscribe to events
from Python — without shelling out and parsing output by hand.

The wrapper is a thin layer over the `logoscore` CLI: every operation
spawns a `logoscore <subcommand> --json` subprocess and parses its output.
No C++ bindings, no IPC code.

## Install

```bash
pip install logoscore
```

The `logoscore` CLI must be on `PATH`. See
[logos-logoscore-cli](https://github.com/logos-co/logos-logoscore-cli)
for install instructions, or use the included Nix flake which pulls it in.

## Quickstart — local daemon

Spawns `logoscore -D` as a subprocess with an isolated config dir.

```python
from logoscore import LogoscoreDaemon

with LogoscoreDaemon(modules_dir="./modules") as daemon:
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
# Daemon stopped + temp config dir cleaned up on __exit__.
```

## Quickstart — daemon in docker

Use `LogoscoreDockerDaemon` to run the daemon inside a container and drive
it over TCP. Good for testing your module against a real distributed
build of logoscore without polluting your dev environment, and for
anything that needs the daemon reachable from multiple processes.

```python
from logoscore import LogoscoreDockerDaemon

with LogoscoreDockerDaemon(
    image="logoscore:smoke-portable",
    modules_dir="./my-module/result/modules",  # host dir with your Qt plugins
) as daemon:
    client = daemon.client(binary="logoscore")  # host-side CLI
    client.load_module("my_module")
    print(client.call("my_module", "do_something", 42))
```

What the helper handles for you:

- Picks a free host TCP port per module; the container binds `6000`
  (`core_service`) and `6001` (`capability_module`) internally, reached
  via `-p $host_core:6000 -p $host_cap:6001`. Same pattern as
  [status-go tests-functional](https://github.com/status-im/status-go/tree/develop/tests-functional).
- Bind-mounts three host dirs into the container:
  `/config` (daemon writes `state.json`), `/persistence`
  (`--persistence-path`; pre-seed for session restore, inspect after),
  `/user-modules` (your compiled Qt plugins, read-only).
- Waits for `state.json` to appear before returning.
- Returns a `LogoscoreClient` whose `client/config.json` carries both
  modules' distinct forwarded host ports (written via
  `LogoscoreClient.write_config`), so it dials the external endpoints —
  not what the daemon wrote into its own connection file. Each module
  needs its own port, so this is config-file-driven rather than relying
  on the single-endpoint `LOGOSCORE_CLIENT_TCP_PORT` override.

Building the image: the logoscore CLI repo produces a reusable base
image via
[`tests/docker_smoke/build_smoke_image.sh`](tests/docker_smoke/README.md).
The image contains only the CLI and its built-in modules — user
modules are always bind-mounted at runtime.

Knobs: `host_port=`, `persistence_dir=` (pre-seeded + not cleaned up on
exit), `codec="cbor"`, `extra_module_dirs=[...]`, `extra_args=[...]`,
`container_name=`, `network=` (attach to a caller-managed docker
network). See `help(LogoscoreDockerDaemon)` for the full list.

### Multiple daemons in a shared docker network

Pass `network=<name>` to attach each container to an EXISTING docker
network. The daemon never creates or removes networks — caller manages
the lifecycle. Use this when daemon containers need to discover each
other by container name via docker's embedded DNS:

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
        # alice resolves "bob" via docker DNS, and vice versa
        ...
finally:
    subprocess.run(["docker", "network", "rm", "my-net"])
```

## Connect to an already-running daemon

If a `logoscore` daemon is already running on the host (started with
`logoscore -D` from a shell, by a service manager, by another tool,
etc.), drop in a `LogoscoreClient` directly — no `LogoscoreDaemon`
needed.

```python
from logoscore import LogoscoreClient

# Daemon at the default ~/.logoscore — no args.
client = LogoscoreClient()
print(client.status())
client.load_module("chat")

# Call a Q_INVOKABLE method on a loaded module.
result = client.call("chat", "send_message", "hello world")
print(result)

# Daemon launched with --config-dir /custom/path.
client = LogoscoreClient(config_dir="/custom/path")
```

Every method spawns a `logoscore <subcommand> --json` subprocess and
parses its output. The wrapper only sets `LOGOSCORE_CONFIG_DIR` on
that subprocess; the CLI reads `<config_dir>/client/config.json` for
the daemon endpoint and `<config_dir>/client/auto.json` for the
local-client token (both auto-emitted by the daemon at boot), so you
don't have to pass a token explicitly for a same-host, same-user daemon.

Cross-host or different-user setups need the [Tokens](#tokens) flow. For
a daemon on **another host** — or any daemon whose two well-known modules
(`core_service` and `capability_module`) bound **different ports** — the
single `transport=` / `tcp_host=` / `tcp_port=` overrides aren't enough
(they describe one endpoint). Use `LogoscoreClient.connect(...)` instead:
see [Connect to a daemon on a remote host](#connect-to-a-daemon-on-a-remote-host).

## Connect to a daemon on a remote host

`LogoscoreClient.connect(...)` builds a client from explicit per-module
endpoints, so you can reach a daemon that isn't on `localhost` (or whose
`core_service` / `capability_module` bound separate ports — two
`QTcpServer`s can't share an address:port). Each `DaemonEndpoint` is one
module's dial spec; pass the raw token the daemon issued for this client
(see [Tokens](#tokens)).

```python
from logoscore import LogoscoreClient, DaemonEndpoint

client = LogoscoreClient.connect(
    {
        "core_service":      DaemonEndpoint(transport="tcp", host="daemon.example.com", port=6000),
        "capability_module": DaemonEndpoint(transport="tcp", host="daemon.example.com", port=6001),
    },
    token="<raw-token-issued-for-this-client>",
)
print(client.status())
client.load_module("chat")
```

`connect()` materializes a `client/config.json` (plus an `auto.json`
holding the token) in a private temp dir that is cleaned up when the
client is garbage-collected. Pass `config_dir=...` to write it somewhere
you control and keep it around. Unlike the `transport=` / `tcp_*=`
constructor kwargs, `connect()` sets **no** `LOGOSCORE_CLIENT_*` env
overrides — the on-disk config is authoritative, which is exactly what
lets it express two modules on two ports.

For TLS, use `transport="tcp_ssl"` and set `verify_peer=` per endpoint
(only honoured for `tcp_ssl`):

```python
client = LogoscoreClient.connect(
    {
        "core_service":      DaemonEndpoint("tcp_ssl", "daemon.example.com", 6000, verify_peer=True),
        "capability_module": DaemonEndpoint("tcp_ssl", "daemon.example.com", 6001, verify_peer=True),
    },
    token="...",
)
```

Need the config file without a client? `LogoscoreClient.write_config(
config_dir, endpoints, token=...)` writes the same `client/config.json`
into a dir you own — the lower-level primitive `connect()` (and the
daemon helpers) are built on.

## Transports

By default the daemon listens on a local Unix socket and the client
connects to it. To open remote-reachable transports, pass
`transports=[...]` to `LogoscoreDaemon` and point the client at the
matching endpoint:

```python
with LogoscoreDaemon(
    modules_dir="./modules",
    transports=["tcp"],          # or ["tcp_ssl"], or ["local", "tcp"]
    tcp_host="0.0.0.0",
    tcp_port=6000,
    tcp_codec="json",            # or "cbor"
) as daemon:
    client = daemon.client()     # reads the per-module tcp dial spec the
                                 # daemon wrote into client/config.json
```

`daemon.client()` needs no transport args: on startup the daemon writes a
`client/config.json` with the actual per-module endpoints (`core_service`
and `capability_module` each on their own bound port), and the client
dials from that. TLS (`tcp_ssl`) additionally accepts `ssl_cert` /
`ssl_key` / `ssl_ca`.

`client(transport=, tcp_host=, codec=, no_verify_peer=)` accepts uniform
overrides (host/transport/codec/verify are shared by both modules); they're
merged **into** the per-module `client/config.json` on disk, each module's
own port left intact — never via `LOGOSCORE_CLIENT_*` env vars. There is
deliberately **no per-call port override**: the CLI applies a single port
to every module uniformly, which would clobber `capability_module` onto
`core_service`'s port. So to reach a daemon whose modules sit on different
ports at a host you specify (the general remote case, including a remote
host), use [`LogoscoreClient.connect(...)`](#connect-to-a-daemon-on-a-remote-host),
which writes a full per-module config. That's how `LogoscoreDockerDaemon`
bridges the container boundary: it forwards each module to its own host
port and hands back a client wired to a per-module `client/config.json`.

## Tokens

The daemon issues a signed token for each authorised client; `logoscore`
authenticates the client's connection with that token. When you spawn a
daemon via `LogoscoreDaemon`, it issues and stores one for you; the
`client()` factory wires it through.

For daemons you didn't spawn (e.g. a long-running one, or one in a
container you want to share across several clients), manage tokens
directly:

```python
from logoscore import issue_token, revoke_token, list_tokens

token = issue_token(config_dir="/path/to/daemon-cfg", name="alice")
print(list_tokens(config_dir="/path/to/daemon-cfg"))
revoke_token(config_dir="/path/to/daemon-cfg", name="alice")
```

## API overview

### `LogoscoreDaemon`

Context manager that spawns `logoscore -D` with an isolated `--config-dir`
(temp dir by default). Multiple daemons can run concurrently without
colliding on `~/.logoscore/daemon/state.json`.

```python
LogoscoreDaemon(
    modules_dir,              # str | Path | list — one or more -m dirs
    binary="logoscore",
    config_dir=None,          # override to share state across instances
    persistence_path=None,    # --persistence-path
    extra_args=None,          # extra flags to pass to the daemon
    env=None,                 # extra env vars for the daemon process
    startup_timeout=15.0,     # seconds to wait for state.json + status
    # Transports (see section above)
    transports=None,          # ["tcp"] | ["tcp_ssl"] | ["local", "tcp"] | ...
    tcp_host="127.0.0.1",
    tcp_port=0,               # 0 = let daemon pick
    tcp_codec="json",         # "json" | "cbor"
    tcp_ssl_host="127.0.0.1",
    tcp_ssl_port=0,
    tcp_ssl_codec="json",
    ssl_cert=None, ssl_key=None, ssl_ca=None,
)
```

### `LogoscoreDockerDaemon`

Same shape, but the daemon runs inside a container. Construction just
stores config; `.start()` / `__enter__` actually runs `docker run`.

```python
LogoscoreDockerDaemon(
    image,                    # e.g. "logoscore:smoke-portable"
    modules_dir,              # host dir → /user-modules inside container
    config_dir=None,          # defaults to tmpdir (cleaned up on stop)
    persistence_dir=None,     # defaults to tmpdir (cleaned up on stop)
    host_port=None,           # None → pick_free_port()
    codec="json",             # "json" | "cbor"
    container_name=None,
    network=None,             # attach to existing caller-managed docker network
    extra_module_dirs=None,   # extra -m paths *inside* the container
    extra_args=None,          # extra daemon args
    startup_timeout=20.0,
)
```

Pass a caller-owned `persistence_dir` (or `config_dir`) to keep it
around after the container exits — useful for session-restore tests
(pre-seed → run → assert against what the modules wrote).

Also exported: `docker_available()`, `image_present(image)`,
`pick_free_port()`, `CONTAINER_TCP_PORT` (= 6000).

### `LogoscoreClient`

Obtained via `daemon.client(...)`, `LogoscoreClient.connect(endpoints,
token=...)` (a remote daemon — see
[Connect to a daemon on a remote host](#connect-to-a-daemon-on-a-remote-host)),
or constructed directly for a same-host daemon. Every method returns
parsed JSON (dict or list) on success and raises on failure:

| Method | CLI equivalent |
|---|---|
| `status()` | `logoscore status` |
| `list_modules(loaded=False)` | `logoscore list-modules [--loaded]` |
| `module_info(name)` | `logoscore module-info <name>` |
| `load_module(name)` | `logoscore load-module <name>` |
| `unload_module(name)` | `logoscore unload-module <name>` |
| `reload_module(name)` | `logoscore reload-module <name>` |
| `call(module, method, *args)` | `logoscore call <module> <method> …` |
| `stats()` | `logoscore stats` |
| `stop()` | `logoscore stop` |
| `on_event(module, event, callback)` | `logoscore watch <module> --event <event>` |

`call(...)` returns the method's unwrapped `result` value. `Path`
arguments are passed through as `@file` so the CLI loads their contents.

Transport-related kwargs (`transport=`, `tcp_host=`, `tcp_port=`,
`codec=`, `no_verify_peer=`) set `LOGOSCORE_CLIENT_*` env vars on the
subprocess invocation — the CLI resolves them through its
`effectiveClientTransport` path, overriding whatever the daemon wrote
into `state.json`.

### Events

```python
sub = client.on_event("chat", "chat-message", callback, error_callback=None)
sub.alive      # False once the watcher exits
sub.cancel()   # SIGINT → SIGTERM → SIGKILL
```

The callback runs on a daemon thread; exceptions are routed to
`error_callback` (default: logged via `logging`).

### Exceptions

`LogoscoreError` is the base class. Subclasses map to the CLI's exit
codes:

| Exit code | Exception |
|---|---|
| 2 | `DaemonNotRunningError` |
| 3 | `ModuleError` |
| 4 | `MethodError` |

## Development

The repo ships a Nix flake that pulls in `logoscore` from
`logos-logoscore-cli` so tests run out of the box:

```bash
nix develop        # python + pytest + logoscore on PATH
pytest             # runs unit + integration (docker smoke skipped)
nix flake check    # same, under nix
```

Test layout:

```
tests/
├── unit/          # no logoscore required; runs anywhere
├── integration/   # spawns local logoscore daemons; nix check covers this
└── docker_smoke/  # docker-required; see tests/docker_smoke/README.md
```

Docker smoke tests live in their own directory because they need the
host's docker socket (not available inside `nix build`). Run them
explicitly:

```bash
./tests/docker_smoke/build_smoke_image.sh  # FLAVOR=portable (default)
pytest tests/docker_smoke                   # --docker-flavor={portable|dev|both}
```

See [`tests/docker_smoke/README.md`](tests/docker_smoke/README.md)
for the full docker-side story (image flavors, mount layout, port
strategy).

Inside the [logos-workspace](https://github.com/logos-co/logos-workspace):

```bash
ws test logos-logoscore-py --auto-local
```

## Licence

Dual-licensed under MIT or Apache-2.0.
