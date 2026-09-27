"""Remote Runtime Control: a logosctl client that operates a daemon it paired with.

A daemon with `runtime_control: true` in its `peering` section also serves
core_service on `tls_tcp` (`runtime_control_config`). `RuntimeControl` gives
a client a config dir of its own and no daemon, pairs it through a
runtime-control invite the daemon mints and then accepts, grants it methods
in the daemon's remote policy, and hands out `LogosctlClient`s that run every
command on the daemon (`logosctl --remote`). The daemon side is anything with
`peer(verb, *args)` and `set_remote_policy(policy)`: a `LogosctlDaemon` on
this machine, or a `LogosctlDockerDaemon` in a container.

    peering = {"peering": runtime_control_config()}
    with LogosctlDaemon("./modules", extra_config=peering) as daemon, \\
            RuntimeControl(daemon) as rc:
        rc.pair()
        rc.grant()        # DEFAULT_GRANTS
        rc.client().call("my_module", "do_something", 42)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Protocol

from . import _proc
from .client import LogosctlClient
from .errors import LogosctlError

# What a paired logosctl calls the daemon as; its policy key is
# "<client runtime id>/logosctl".
CONSUMER = "logosctl"

# Every LogosctlClient command but `stop`, and every method of the daemon's
# user modules ("*" never covers core_service, nor the runtime's own modules).
DEFAULT_GRANTS: dict[str, Any] = {
    "core_service": ["getStatus", "listModules", "getModuleInfo", "getModuleStats",
                     "loadModule", "unloadModule", "reloadModule",
                     "callModuleMethod", "watchModuleEvents"],
    "*": "*",
}


def runtime_control_config(name: str = "node", *, host: str = "127.0.0.1",
                           port: int = 0) -> dict[str, Any]:
    """A daemon's `peering:` section for Remote Runtime Control: a control
    endpoint on `host:port` (0: one the daemon picks and keeps)."""
    return {"name": name,
            "control": {"enabled": True, "host": host, "port": port},
            "runtime_control": True}


class RemoteDaemon(Protocol):
    """The daemon side of a pairing, driven as its local operator."""

    def peer(self, verb: str, *args: str) -> Any: ...

    def set_remote_policy(self, policy: Mapping[str, Any]) -> Any: ...


class RuntimeControl:
    """A logosctl client config dir paired with one daemon for Remote Runtime
    Control. `config_dir=None` uses a temp dir, removed by `close()`."""

    def __init__(
        self,
        daemon: RemoteDaemon,
        *,
        binary: str = "logosctl",
        config_dir: str | Path | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.daemon = daemon
        self.binary = binary
        self.timeout = timeout
        self._owns_config_dir = config_dir is None
        if config_dir is None:
            self.config_dir = Path(tempfile.mkdtemp(prefix="logosctl-rc-"))
        else:
            self.config_dir = Path(config_dir)
            self.config_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_id = ""    # this client's, once paired
        self.daemon_id = ""     # the daemon's, once paired

    @property
    def policy_key(self) -> str:
        return f"{self.runtime_id}/{CONSUMER}"

    def pair(self) -> dict[str, Any]:
        """Redeem a runtime-control invite from the daemon, accept the pairing
        there, and return the daemon as this client now lists it."""
        self.runtime_id = self._run(["remote", "ls"])["self"]["runtime_id"]
        invite = self.daemon.peer("invite", "--runtime-control", "--ttl", "600")["invite"]
        deadline = time.monotonic() + self.timeout
        # `remote pair` blocks until the daemon accepts. The invite, a
        # secret, reaches its stdin from an unlinked file, never argv.
        with tempfile.TemporaryFile("w+", encoding="utf-8") as invite_file:
            invite_file.write(invite)
            invite_file.seek(0)
            proc = subprocess.Popen(
                [self.binary, "--json", "remote", "pair", "-"],
                stdin=invite_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8",
                env=_proc._prep_env(self.config_dir, None, None),
            )
        try:
            self.daemon.peer("accept", self._incoming(proc, deadline)["id"])
            out, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
        except BaseException:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            raise
        if proc.returncode != 0:
            raise LogosctlError(
                f"`logosctl remote pair` failed (exit {proc.returncode}): "
                f"{(out or err).strip()}", exit_code=proc.returncode, stderr=err)
        paired = json.loads(out)
        self.daemon_id = paired["runtime_id"]
        return paired

    def grant(self, grants: Mapping[str, Any] | None = None) -> None:
        """Set what this client may call in the daemon's remote policy
        (`DEFAULT_GRANTS` when None), keeping every other entry."""
        policy = self.daemon.peer("policy")
        policy = dict(policy) if isinstance(policy, dict) else {}
        policy[self.policy_key] = dict(DEFAULT_GRANTS if grants is None else grants)
        self.daemon.set_remote_policy(policy)

    def peers(self) -> list[dict[str, Any]]:
        """The daemons this client is paired with (`logosctl remote ls`)."""
        return self._run(["remote", "ls"])["peers"]

    def client(self, *, timeout: float | None = 30.0) -> LogosctlClient:
        """A client whose every command runs on the daemon."""
        if not self.daemon_id:
            raise LogosctlError("not paired yet — call pair() first")
        return LogosctlClient(self.binary, config_dir=self.config_dir,
                              remote=self.daemon_id, timeout=timeout)

    def close(self) -> None:
        if self._owns_config_dir:
            shutil.rmtree(self.config_dir, ignore_errors=True)

    def __enter__(self) -> "RuntimeControl":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ── Internal ────────────────────────────────────────────────────────────

    def _run(self, args: list[str]) -> Any:
        return _proc.run_json(self.binary, args, config_dir=self.config_dir,
                              timeout=self.timeout)

    def _incoming(self, proc: subprocess.Popen[str], deadline: float) -> dict[str, Any]:
        """The daemon's pending pairing from this client, once it shows."""
        while time.monotonic() < deadline:
            for p in (self.daemon.peer("pending") or {}).get("pending", []):
                if p.get("direction") == "incoming" and p.get("peer_runtime_id") == self.runtime_id:
                    return p
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise LogosctlError(
                    f"`logosctl remote pair` exited {proc.returncode} before the "
                    f"daemon saw it: {(out or err).strip()}")
            time.sleep(0.2)
        raise LogosctlError(f"the daemon saw no pairing from {self.runtime_id} "
                            f"within {self.timeout}s")
