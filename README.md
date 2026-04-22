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

## Quickstart

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
)
```

### `LogoscoreClient`

Obtained via `daemon.client()`. Every method returns parsed JSON (dict or
list) on success and raises on failure:

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

`call(...)` returns the method's unwrapped `result` value. `Path` arguments
are passed through as `@file` so the CLI loads their contents.

### Events

```python
sub = client.on_event("chat", "chat-message", callback, error_callback=None)
sub.alive      # False once the watcher exits
sub.cancel()   # SIGINT → SIGTERM → SIGKILL
```

The callback runs on a daemon thread; exceptions are routed to
`error_callback` (default: logged via `logging`).

### Exceptions

`LogoscoreError` is the base class. Subclasses map to the CLI's exit codes:

| Exit code | Exception |
|---|---|
| 2 | `DaemonNotRunningError` |
| 3 | `ModuleError` |
| 4 | `MethodError` |

## Development

The repo ships a Nix flake that pulls in `logoscore` from
`logos-logoscore-cli` so tests run out of the box:

```bash
nix develop        # python + logoscore on PATH
pytest             # run the integration tests
nix flake check    # same, under nix
```

Inside the [logos-workspace](https://github.com/logos-co/logos-workspace):

```bash
ws test logos-logoscore-py --auto-local
```

## Licence

Dual-licensed under MIT or Apache-2.0.
