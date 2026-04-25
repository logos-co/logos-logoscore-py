"""Python wrapper for the logoscore CLI.

Launch a `logoscore` daemon, load modules, call methods, and subscribe to
events from Python. Internally spawns `logoscore` subprocesses and parses
their JSON output.

Two daemon lifecycle flavors:

* `LogoscoreDaemon` — spawns a local `logoscore` subprocess. Use this
  when you want in-process-parent tests and fast iteration.

* `LogoscoreDockerDaemon` — spawns a logoscore daemon inside a docker
  container and speaks TCP to it from the host. Use this to smoke-test
  a real distribution of logoscore (or your own module against one)
  without polluting your dev environment, and for anything that needs
  the daemon to be reachable from multiple processes.

Example (local):
    from logoscore import LogoscoreDaemon

    with LogoscoreDaemon(modules_dir="./modules") as daemon:
        client = daemon.client()
        client.load_module("chat")
        result = client.call("chat", "send_message", "hello")

Example (docker):
    from logoscore import LogoscoreDockerDaemon

    with LogoscoreDockerDaemon(
        image="logoscore:smoke-portable",
        modules_dir="./my-module/result/modules",
    ) as daemon:
        client = daemon.client(binary="logoscore")
        client.load_module("my_module")
        print(client.call("my_module", "do_something", 42))
"""

from .client import LogoscoreClient
from .daemon import LogoscoreDaemon
from .docker_daemon import (
    CONTAINER_TCP_PORT,
    LogoscoreDockerDaemon,
    build_modules_in_docker,
    docker_available,
    image_present,
    pick_free_port,
)
from .errors import (
    DaemonNotRunningError,
    LogoscoreError,
    MethodError,
    ModuleError,
)
from .events import Subscription
from .tokens import issue_token, revoke_token, list_tokens

__all__ = [
    "LogoscoreDaemon",
    "LogoscoreDockerDaemon",
    "LogoscoreClient",
    "Subscription",
    "LogoscoreError",
    "DaemonNotRunningError",
    "ModuleError",
    "MethodError",
    "issue_token",
    "revoke_token",
    "list_tokens",
    # Docker helpers
    "CONTAINER_TCP_PORT",
    "build_modules_in_docker",
    "docker_available",
    "image_present",
    "pick_free_port",
]

__version__ = "0.1.0"
