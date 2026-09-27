"""Python wrapper for the logoscore CLI.

Launch a `logoscore` daemon, load modules, call methods, and subscribe to
events from Python. Internally spawns `logoscore` subprocesses and parses
their JSON output. A client reaches a daemon on this machine; operating one
elsewhere is the `logosctl` package's Remote Runtime Control.

Example:
    from logoscore import LogoscoreDaemon

    with LogoscoreDaemon(modules_dir="./modules") as daemon:
        client = daemon.client()
        client.load_module("chat")
        result = client.call("chat", "send_message", "hello")
"""

from .client import DaemonEndpoint, LogoscoreClient
from .daemon import LogoscoreDaemon
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
    "LogoscoreClient",
    "DaemonEndpoint",
    "Subscription",
    "LogoscoreError",
    "DaemonNotRunningError",
    "ModuleError",
    "MethodError",
    "issue_token",
    "revoke_token",
    "list_tokens",
]

__version__ = "0.1.0"
