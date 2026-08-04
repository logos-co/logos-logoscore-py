"""Thin wrappers around the `logosctl token issue` / `token revoke` /
`token ls` subcommands.

These are daemon-less operations: they read and write
`$CONFIG_DIR/daemon/tokens.json` (the hashed-at-rest token list) and
`$CONFIG_DIR/daemon/tokens/<name>.json` (per-token raw files) directly.
Use when provisioning client credentials before or alongside a daemon —
no running daemon needed.

`config_dir` reaches the CLI as `LOGOSCTL_CONFIG_DIR` in the child's
environment rather than as `--config-dir`: the flag is app-level, and a
client subcommand swallows anything it doesn't recognize as a positional
argument, so a trailing `--config-dir DIR` would be handed to the token
command instead of selecting the session.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import _proc


def issue_token(
    name: str,
    *,
    binary: str = "logosctl",
    config_dir: str | Path | None = None,
    replace: bool = False,
    local_only: bool = False,
    expires: str | None = None,
    timeout: float | None = 30.0,
) -> dict[str, Any]:
    """Issue a new token under `name`. Returns `{"name", "token", "file", ...}`.

    The raw token is visible here (it's freshly minted). After this call it
    only exists in the returned dict and in the per-client file at
    `file`; the daemon's `tokens.json` stores only a hash.

    `local_only=True` marks the token valid only over the local (QLocalSocket)
    transport — the daemon rejects it when presented over tcp / tcp_ssl, so
    it is the wrong choice for a client dialing a network listener.
    `expires` accepts a relative duration (``"30s"``, ``"5m"``, ``"2h"``,
    ``"30d"``) or an absolute ISO 8601 UTC date/timestamp
    (``"2026-12-31"`` / ``"2026-12-31T23:59:59Z"``); anything else fails
    with INVALID_EXPIRES.

    Raises ModuleError (exit 3) when a token of that name already exists
    and `replace` is False.
    """
    args = ["token", "issue", "--name", name]
    if replace:
        args.append("--replace")
    if local_only:
        args.append("--local-only")
    if expires:
        args += ["--expires", expires]
    return _proc.run_json(
        binary, args,
        config_dir=Path(config_dir) if config_dir else None,
        token=None, timeout=timeout,
    )


def revoke_token(
    name: str,
    *,
    binary: str = "logosctl",
    config_dir: str | Path | None = None,
    timeout: float | None = 30.0,
) -> dict[str, Any]:
    """Revoke a previously-issued token by name. Raises ModuleError on
    exit code 3 (no token with that name)."""
    return _proc.run_json(
        binary, ["token", "revoke", name],
        config_dir=Path(config_dir) if config_dir else None,
        token=None, timeout=timeout,
    )


def list_tokens(
    *,
    binary: str = "logosctl",
    config_dir: str | Path | None = None,
    timeout: float | None = 30.0,
) -> list[dict[str, Any]]:
    """List currently-issued tokens.

    Returns `[{"name", "issued_at", "expires_at", "local_only",
    "raw_file_present"}, ...]`. `expires_at` is None for a non-expiring
    token, and `raw_file_present` is False once the per-token file under
    `daemon/tokens/` has been handed out and deleted — the hash in
    `tokens.json` still validates it, but the raw value is unrecoverable.
    """
    result = _proc.run_json(
        binary, ["token", "ls"],
        config_dir=Path(config_dir) if config_dir else None,
        token=None, timeout=timeout,
    )
    return result if isinstance(result, list) else []
