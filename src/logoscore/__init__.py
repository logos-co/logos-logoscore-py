"""Python wrapper for the logoscore CLI.

Launch a `logoscore` daemon, load modules, call methods, and subscribe to
events from Python. Internally spawns `logoscore` subprocesses and parses
their JSON output.

Example:
    from logoscore import LogoscoreDaemon

    with LogoscoreDaemon(modules_dir="./modules") as daemon:
        client = daemon.client()
        client.load_module("chat")
        result = client.call("chat", "send_message", "hello")
        sub = client.on_event("chat", "chat-message", print)
        ...
        sub.cancel()
"""

from .client import LogoscoreClient
from .daemon import LogoscoreDaemon
from .errors import (
    DaemonNotRunningError,
    LogoscoreError,
    MethodError,
    ModuleError,
)
from .events import Subscription

__all__ = [
    "LogoscoreDaemon",
    "LogoscoreClient",
    "Subscription",
    "LogoscoreError",
    "DaemonNotRunningError",
    "ModuleError",
    "MethodError",
]

__version__ = "0.1.0"
