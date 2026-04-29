"""Thin wrappers around the `logoscore issue-token` / `revoke-token` /
`list-tokens` subcommands.

These are daemon-less operations: they read and write `$CONFIG_DIR/tokens.db`
and `$CONFIG_DIR/tokens/<name>.json` directly. Use when provisioning client
credentials before or alongside a daemon — no running daemon needed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import _proc


def issue_token(
    name: str,
    *,
    binary: str = "logoscore",
    config_dir: str | Path | None = None,
    replace: bool = False,
    timeout: float | None = 30.0,
) -> dict[str, Any]:
    """Issue a new token under `name`. Returns `{"name", "token", "file", ...}`.

    The raw token is visible here (it's freshly minted). After this call it
    only exists in the returned dict and in the per-client file at
    `file`; the daemon's `tokens.db` stores only a hash.
    """
    args = ["issue-token", "--name", name]
    if replace:
        args.append("--replace")
    return _proc.run_json(
        binary, args,
        config_dir=Path(config_dir) if config_dir else None,
        token=None, timeout=timeout,
    )


def revoke_token(
    name: str,
    *,
    binary: str = "logoscore",
    config_dir: str | Path | None = None,
    timeout: float | None = 30.0,
) -> dict[str, Any]:
    """Revoke a previously-issued token by name. Raises ModuleError on
    exit code 3 (no token with that name)."""
    return _proc.run_json(
        binary, ["revoke-token", name],
        config_dir=Path(config_dir) if config_dir else None,
        token=None, timeout=timeout,
    )


def list_tokens(
    *,
    binary: str = "logoscore",
    config_dir: str | Path | None = None,
    timeout: float | None = 30.0,
) -> list[dict[str, str]]:
    """List currently-issued tokens. Returns `[{"name", "issued_at"}, ...]`."""
    result = _proc.run_json(
        binary, ["list-tokens"],
        config_dir=Path(config_dir) if config_dir else None,
        token=None, timeout=timeout,
    )
    return result if isinstance(result, list) else []
