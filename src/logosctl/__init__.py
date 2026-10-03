"""Python wrapper for the logosctl CLI.

Launch a `logosctl` daemon, load modules, call methods, and subscribe to
events from Python. Internally spawns `logosctl` subprocesses and parses
their JSON output.

This is the sibling of the `logoscore` package, covering the new grouped
CLI surface (`module ls`, `token issue`, `daemon start`, …). The public
shape is deliberately the same — same modules, same methods, same
arguments — with one structural difference a logoscore reader has to know
about: logosctl has no configuration flags. Module directories, the
persistence path and the client's dial spec are YAML documents installed
into the session with `daemon config set` / `client config set` before the
daemon starts. `LogosctlDaemon` and `LogosctlClient.write_config` write them
for you; the only per-call knobs left are the config dir and the token.

A daemon elsewhere is operated with Remote Runtime Control: a client with a
config dir of its own pairs with it once (`RuntimeControl`), and then runs
every command there (`LogosctlClient(remote=…)`). A module calling a module
on another runtime is peering (`PeeredDaemons`).

Two daemon lifecycle flavors:

* `LogosctlDaemon` — spawns a local `logosctl` subprocess. Use this when
  you want in-process-parent tests and fast iteration.

* `LogosctlDockerDaemon` — spawns a logosctl daemon inside a docker
  container and operates it from the host with Remote Runtime Control.
  Use this to smoke-test a real distribution of logosctl (or your own
  module against one) without polluting your dev environment.

Example (local):
    from logosctl import LogosctlDaemon

    with LogosctlDaemon(modules_dir="./modules") as daemon:
        client = daemon.client()
        client.load_module("chat")
        result = client.call("chat", "send_message", "hello")

Example (docker):
    from logosctl import LogosctlDockerDaemon

    with LogosctlDockerDaemon(
        image="logosctl:smoke-portable",
        modules_dir="./my-module/result/modules",
        binary="./logosctl",
    ) as daemon:
        client = daemon.client()
        client.load_module("my_module")
        print(client.call("my_module", "do_something", 42))
"""

from .client import DaemonEndpoint, LogosctlClient
from .daemon import LogosctlDaemon
from .docker_daemon import (
    LogosctlDockerDaemon,
    build_modules_in_docker,
    docker_available,
    image_present,
    pick_free_port,
)
from .errors import (
    DaemonNotRunningError,
    LogosctlError,
    MethodError,
    ModuleError,
)
from .events import Subscription
from .peering import PeeredDaemons
from .remote import RuntimeControl, runtime_control_config
from .tokens import issue_token, revoke_token, list_tokens

__all__ = [
    "LogosctlDaemon",
    "LogosctlDockerDaemon",
    "LogosctlClient",
    "PeeredDaemons",
    "RuntimeControl",
    "runtime_control_config",
    "DaemonEndpoint",
    "Subscription",
    "LogosctlError",
    "DaemonNotRunningError",
    "ModuleError",
    "MethodError",
    "issue_token",
    "revoke_token",
    "list_tokens",
    # Docker helpers
    "build_modules_in_docker",
    "docker_available",
    "image_present",
    "pick_free_port",
]

__version__ = "0.1.0"
