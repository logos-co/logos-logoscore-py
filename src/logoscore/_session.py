"""Get a client — attach to a running daemon, or own a private one.

Attaching needs no endpoints: a client built against a running daemon's
``<config_dir>/client/config.json`` (with the token from ``client/auto.json``)
dials it directly. This is what lets a stateless CLI / an MCP server talk to a
long-running, externally-managed daemon (systemd, a hub, …).
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .client import LogoscoreClient
from .daemon import LogoscoreDaemon


def _read_token(config_dir: Path) -> str | None:
    tf = config_dir / "client" / "auto.json"
    try:
        return json.loads(tf.read_text()).get("token") if tf.exists() else None
    except Exception:
        return None


def attach(config_dir, *, binary: str = "logoscore", timeout: float = 30.0) -> LogoscoreClient:
    """A live client for an already-running daemon, addressed by its config dir."""
    cd = Path(config_dir)
    if not (cd / "client" / "config.json").exists():
        raise FileNotFoundError(
            f"no client/config.json under {cd} — is a daemon running there?")
    return LogoscoreClient(binary=binary, config_dir=cd, token=_read_token(cd), timeout=timeout)


@contextmanager
def session(*, config_dir=None, modules_dir=None, load=(), binary="logoscore",
            env=None) -> Iterator[LogoscoreClient]:
    """Yield a client. ``config_dir`` attaches to a running daemon; otherwise a
    private daemon is brought up from ``modules_dir`` (loading ``load``) and torn
    down on exit."""
    if config_dir:
        yield attach(config_dir, binary=binary)
        return
    if not modules_dir:
        raise ValueError("pass config_dir (attach) or modules_dir (own a daemon)")
    daemon = LogoscoreDaemon(modules_dir=modules_dir, binary=binary, env=dict(env or {}))
    daemon.start()
    try:
        client = daemon.client()
        for module in load:
            client.load_module(module)
        yield client
    finally:
        daemon.stop()
