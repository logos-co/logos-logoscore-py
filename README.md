# logos-logoscore-py

Python wrapper for the [`logoscore`](https://github.com/logos-co/logos-logoscore-cli)
CLI. Launch a daemon, load modules, call methods, and subscribe to events
from Python — without shelling out and parsing output by hand.

The wrapper is a thin layer over the `logoscore` CLI: every operation
spawns a `logoscore <subcommand> --json` subprocess and parses its output.
No C++ bindings, no IPC code.

## Two clients

The CLI repo now ships [two binaries](https://github.com/logos-co/logos-logoscore-cli#two-binaries),
so this package ships two clients — one each, side by side:

| Import | Drives | |
|---|---|---|
| **`logoscore`** | the `logoscore` binary | The client that exists today. Unchanged. **Use this one.** |
| **`logosctl`** | the `logosctl` binary | Same shape, ported to the new CLI's surface. **Being validated; not yet the default.** |

```python
from logoscore import LogoscoreDaemon, LogoscoreClient   # today
from logosctl  import LogosctlDaemon,  LogosctlClient    # under validation
```

The two share no code and no state, and neither can reach the other's
binary. Everything below documents `logoscore` unless it says otherwise;
`logosctl` mirrors it method for method, with one structural difference —
the new CLI configures a daemon with a document installed before it starts
(`daemon config set`) rather than with flags. A client of either reaches a
daemon on its own machine over the daemon's local socket. Operating a
daemon elsewhere, in a container included, is `logosctl`'s alone: Remote
Runtime Control, below.

`src/logosctl/` and `tests/logosctl/` are the whole of the new client, so
whichever way the validation goes, the losing half is a delete.

## Install

```bash
pip install logoscore
```

One distribution, both clients — `import logoscore` and `import logosctl`
both work after installing. The matching CLI must be on `PATH`. See
[logos-logoscore-cli](https://github.com/logos-co/logos-logoscore-cli)
for install instructions, or use the included Nix flake, which puts both
binaries in the dev shell.

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

## Quickstart — daemon in docker (logosctl)

Use `LogosctlDockerDaemon` to run a `logosctl` daemon inside a container
and operate it from the host over Remote Runtime Control. Good for testing
your module against a real distributed build of logosctl without polluting
your dev environment.

```python
from logosctl import LogosctlDockerDaemon

with LogosctlDockerDaemon(
    image="logosctl:smoke-portable",
    modules_dir="./my-module/result/modules",  # host dir with your Qt plugins
    binary="logosctl",                         # the host-side client
) as daemon:
    client = daemon.client()
    client.load_module("my_module")
    print(client.call("my_module", "do_something", 42))
```

What the helper handles for you:

- Installs the daemon's config document into the session (`daemon config
  set`), with a `peering` section that puts its control endpoint on a fixed
  loopback port and turns `runtime_control` on.
- Runs the container on the host's network (`--network host`), so the
  host reaches the control endpoint and the runtime-control listener the
  daemon picks a port for. That needs Linux; Docker Desktop has host
  networking only as an opt-in setting.
- Bind-mounts three host dirs into the container: `/config` (the
  session), `/persistence` (`persistence_path`; pre-seed for session
  restore, inspect after), `/user-modules` (your compiled Qt plugins,
  read-only).
- Waits for `state.json`, then mints a runtime-control invite inside the
  container, pairs a host-side logosctl (a config dir of its own) with
  `logosctl remote pair`, accepts it inside the container, and grants it
  `DEFAULT_GRANTS` in the daemon's remote policy.
- Returns clients that run every command with `--remote`; its
  `runtime_control.grant(...)` changes what they may call.

Building the image:
[`tests/docker_smoke/build_smoke_image.sh`](tests/docker_smoke/README.md).
The image contains only logosctl and its built-in modules — user modules
are always bind-mounted at runtime.

Knobs: `control_port=`, `name=` (the daemon's peering name), `grants=`,
`persistence_dir=` (pre-seeded + not cleaned up on exit),
`extra_module_dirs=[...]`, `extra_config={...}`, `extra_args=[...]`,
`container_name=`. See `help(LogosctlDockerDaemon)` for the full list.

## Two daemons, one calling the other's modules (logosctl)

`PeeredDaemons` starts two `logosctl` daemons on this machine. The
exporter loads your modules and shares them, and the importer imports
them. On the importer, each import is a facade that forwards every call
and event to the exporter's copy of the module. The two pair through the
exporter's local invite, which needs no code.

```python
from logosctl import PeeredDaemons

with PeeredDaemons(modules_dir="./modules", exports=["my_module"]) as pair:
    importer = pair.importer_client()
    print(importer.call("my_module", "do_something", 42))  # answered by the exporter

    pair.set_policy({})           # the exporter grants the importer nothing now
    pair.wait_for_import("my_module", "error")
    pair.set_policy({f"{pair.importer_id}/*": ["*"]})
    pair.wait_for_import("my_module")

    pair.restart_exporter()       # same runtime, pairing and control port
    pair.wait_for_import("my_module")
```

Only a plain module (`"transport": "qt_remote_plain"`) can be exported.
`LogosctlClient.peer(verb, …)` runs any `logosctl peer` verb: `status`,
`ls`, `routes`, `import`, `policy set FILE`, and so on.
`importer_placement={"single_process": True}` gives the importer a
single-process runtime, which runs peering and each facade in its own
process: no host process for either. Peering is how a module on one
runtime calls one on another; operating a daemon from another machine is
Remote Runtime Control, below.
`tests/logosctl/integration/test_peering.py` replays the full_api tables
through an import, with the facade in a host process and in a single-process
importer, and checks that a `concurrency: multi` provider
(`test_concurrency_cpp`) keeps its calls parallel through one. The
conformance matrix's peered coordinate (`run_matrix.py --peered`, check
`conformance-transport-peered`) measures each plain provider again through
an import, as `<provider>@peered`, and compares every cell with the provider
measured locally. A facade reports a provider's failure as `dispatch_failed`
with the provider's class after `remote/`, and the driver compares that class.

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

A client in a config dir the daemon does not own needs a dial spec and a
token of its own: `LogoscoreClient.connect(endpoints, token=...,
instance_id=...)` writes both (`LogoscoreClient.write_config` is the
lower-level primitive). A same-user client can use the daemon's boot
token; for any other, see [Tokens](#tokens). A daemon on another machine
is operated over Remote Runtime Control, next.

## Operate a daemon on another machine (logosctl)

Remote Runtime Control: a `logosctl` client on one machine runs its
commands on a daemon on another, as that daemon's operator. The daemon
turns it on in its config's `peering` section; the client pairs once
through a runtime-control invite the daemon mints and accepts, and needs
no daemon or token of its own — the daemon knows it by its key. Pairing
grants nothing: the daemon's remote policy names the `core_service`
methods, and the module methods, the client may call; anything else is
refused with `NOT_AUTHORISED`. See the Logos developer guide, [§9.6
Linking runtimes](https://github.com/logos-co/logos-tutorial/blob/master/logos-developer-guide.md#96-linking-runtimes-peering).

`RuntimeControl` does the whole flow against a daemon it can operate
locally (a `LogosctlDaemon`, or a `LogosctlDockerDaemon` from inside its
container):

```python
from logosctl import LogosctlDaemon, RuntimeControl, runtime_control_config

peering = {"peering": runtime_control_config("node")}  # control on 127.0.0.1
with LogosctlDaemon("./modules", extra_config=peering) as daemon, \
        RuntimeControl(daemon) as rc:
    rc.pair()        # peer invite --runtime-control / remote pair / peer accept
    rc.grant({"core_service": ["getStatus", "loadModule", "callModuleMethod"],
              "my_module": ["do_something"]})
    remote = rc.client()               # every command runs with --remote
    remote.load_module("my_module")
    print(remote.call("my_module", "do_something", 42))
```

`rc.grant()` with no argument grants `logosctl.remote.DEFAULT_GRANTS`:
every `LogosctlClient` command but `stop`, and every method of the
daemon's user modules (`"*"` never covers `core_service`, nor the
runtime's own modules). The same by hand, the client on its own machine:

```bash
logosctl peer invite --runtime-control > invite.txt     # daemon
logosctl remote pair invite.txt                         # client: waits for the daemon
logosctl peer pending; logosctl peer accept <id>        # daemon
logosctl peer policy set policy.json                    # daemon:
#   {"<client runtime id>/logosctl": {"core_service": ["getStatus", ...], "*": "*"}}
logosctl --remote node status                           # client
```

`LogosctlClient(config_dir=..., remote="node")` drives a daemon a config
dir is already paired with.

## Transports

A client reaches a daemon on its machine over the daemon's local socket,
the only client transport. The legacy `tcp` and `tcp_ssl` transports
(plain TCP, and server-only TLS with bearer tokens) are gone, and with
them the JSON/CBOR codec matrix they carried. What replaced them rides
`tls_tcp` — mutual TLS 1.3 with pinned keys: Remote Runtime Control to
operate a daemon from elsewhere, and peering (`PeeredDaemons`) for a
module on one runtime to call a module on another.

## Tokens

The daemon issues a signed token for each authorised client; `logoscore`
authenticates the client's connection with that token. When you spawn a
daemon via `LogoscoreDaemon`, it issues and stores one for you; the
`client()` factory wires it through.

For daemons you didn't spawn (e.g. a long-running one on this machine),
manage tokens directly:

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
)
```

### `LogosctlDockerDaemon` (logosctl)

Same shape, but the daemon runs inside a container and the host operates
it over Remote Runtime Control. Construction just stores config;
`.start()` / `__enter__` runs the container and pairs the host client.

```python
LogosctlDockerDaemon(
    image,                    # e.g. "logosctl:smoke-portable"
    modules_dir,              # host dir → /user-modules inside container
    binary="logosctl",        # the host-side client that pairs with it
    config_dir=None,          # defaults to tmpdir (cleaned up on stop)
    persistence_dir=None,     # defaults to tmpdir (cleaned up on stop)
    control_port=None,        # None → pick_free_port(), on 127.0.0.1
    name="node",              # the daemon's peering name
    grants=None,              # None → logosctl.remote.DEFAULT_GRANTS
    container_name=None,
    extra_module_dirs=None,   # extra modules_dirs *inside* the container
    extra_config=None,        # extra daemon config keys, merged last
    extra_args=None,          # extra app-level flags
    startup_timeout=30.0,
)
```

Pass a caller-owned `persistence_dir` (or `config_dir`) to keep it
around after the container exits — useful for session-restore tests
(pre-seed → run → assert against what the modules wrote). `peer(verb,
...)` runs `logosctl peer` inside the container; `runtime_control` is the
host client's pairing.

Also exported from `logosctl`: `RuntimeControl`, `runtime_control_config`,
`docker_available()`, `image_present(image)`, `pick_free_port()`,
`build_modules_in_docker(...)`.

### `LogoscoreClient`

Obtained via `daemon.client()`, `LogoscoreClient.connect(endpoints,
token=...)` (a daemon on this machine, from a config dir it does not own),
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

The repo ships a Nix flake that pulls both binaries out of
`logos-logoscore-cli` — its `default` output for `logoscore`, its `ctl`
output for `logosctl` — so tests run out of the box:

```bash
nix develop        # python + pytest + logoscore + logosctl on PATH
pytest             # runs unit + integration, both clients (docker smoke skips without an image)
nix flake check    # same, under nix
```

Test layout — one tree per client, duplicated on purpose:

```
tests/
├── unit/          # no logoscore required; runs anywhere
├── integration/   # spawns local logoscore daemons; nix check covers this
├── docker_smoke/  # logosctl in docker, over Remote Runtime Control; see its README
└── logosctl/      # the same two suites against logosctl
    ├── unit/
    └── integration/   # + peering (test_peering.py) and Remote Runtime Control
```

`tests/logosctl/` is a deliberate duplicate rather than a parametrisation:
the two CLIs configure a daemon through different mechanisms (flags versus
an installed config document), so a shared suite would be mostly branches.
The nix checks are duplicated the same way — `unit-logosctl` and
`integration-logosctl-local` alongside the originals, in their own CI
job, so a red logosctl run cannot mask a logoscore regression. The
conformance matrix is *not* duplicated: it measures the LIDL type
contract in the shared runtime, which a second CLI would only re-measure.

The logosctl suites skip unless `LOGOSCTL_BIN` and
`LOGOSCTL_TEST_MODULES_DIR` are set (the dev shell and the nix checks set
both), mirroring the `LOGOSCORE_*` pair.

Docker smoke tests live in their own directory because they need the
host's docker socket (not available inside `nix build`). Run them
explicitly:

```bash
./tests/docker_smoke/build_smoke_image.sh  # FLAVOR=portable (default)
nix develop --command pytest tests/docker_smoke   # --docker-flavor={portable|dev|both}
```

See [`tests/docker_smoke/README.md`](tests/docker_smoke/README.md)
for the full docker-side story (image flavors, mount layout, host
networking).

Inside the [logos-workspace](https://github.com/logos-co/logos-workspace):

```bash
ws test logos-logoscore-py --auto-local
```

## Licence

Dual-licensed under MIT or Apache-2.0.
