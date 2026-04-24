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

- Picks a free host TCP port; the container always binds `6000` internally
  and is reached via `-p $host_port:6000`. Same pattern as
  [status-go tests-functional](https://github.com/status-im/status-go/tree/develop/tests-functional).
- Bind-mounts three host dirs into the container:
  `/config` (daemon writes `daemon.json`), `/persistence`
  (`--persistence-path`; pre-seed for session restore, inspect after),
  `/user-modules` (your compiled Qt plugins, read-only).
- Waits for `daemon.json` to appear before returning.
- Returns a `LogoscoreClient` preconfigured with the host port override
  (`LOGOSCORE_CLIENT_TCP_PORT`) so it dials the external endpoint rather
  than the one the daemon wrote into its connection file.

Building the image: the logoscore CLI repo produces a reusable base
image via
[`tests/docker_smoke/build_smoke_image.sh`](tests/docker_smoke/README.md).
The image contains only the CLI and its built-in modules — user
modules are always bind-mounted at runtime.

Knobs: `host_port=`, `persistence_dir=` (pre-seeded + not cleaned up on
exit), `codec="cbor"`, `extra_module_dirs=[...]`, `extra_args=[...]`,
`container_name=`. See `help(LogoscoreDockerDaemon)` for the full list.

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
    client = daemon.client(transport="tcp", tcp_host="localhost", tcp_port=6000)
```

TLS (`tcp_ssl`) additionally accepts `ssl_cert` / `ssl_key` / `ssl_ca`.
When talking to a daemon whose advertised host/port differs from the
reachable one (NAT, port-forwarding, SSH tunnels), use
`tcp_host` / `tcp_port` on the **client** as overrides — the client
will dial the overridden endpoint instead of what `daemon.json` says.
That's exactly how `LogoscoreDockerDaemon` bridges the container boundary.

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
colliding on `~/.logoscore/daemon.json`.

```python
LogoscoreDaemon(
    modules_dir,              # str | Path | list — one or more -m dirs
    binary="logoscore",
    config_dir=None,          # override to share state across instances
    persistence_path=None,    # --persistence-path
    extra_args=None,          # extra flags to pass to the daemon
    env=None,                 # extra env vars for the daemon process
    startup_timeout=15.0,     # seconds to wait for daemon.json + status
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

Obtained via `daemon.client(...)`. Every method returns parsed JSON (dict
or list) on success and raises on failure:

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
into `daemon.json`.

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
