"""Subprocess helpers — invoke `logoscore` and parse JSON output.

Every client command runs `logoscore <subcommand> ... --json`. Stdout is
parsed as a single JSON value; non-zero exit codes are mapped to exception
types by `errors.from_exit_code`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .errors import LogoscoreError, from_exit_code


def _prep_env(
    config_dir: Path | None,
    token: str | None,
    extra_env: dict[str, str] | None,
) -> dict[str, str]:
    env = os.environ.copy()
    if config_dir is not None:
        env["LOGOSCORE_CONFIG_DIR"] = str(config_dir)
    if token is not None:
        env["LOGOSCORE_TOKEN"] = token
    if extra_env:
        env.update(extra_env)
    return env


def _format_failure(cmd: Sequence[str], proc: subprocess.CompletedProcess[str]) -> str:
    msg = f"logoscore command failed (exit {proc.returncode}): {' '.join(cmd)}"
    stderr = (proc.stderr or "").strip()
    if stderr:
        msg += f"\n{stderr}"
    return msg


def _error_code_from_stdout(stdout: str) -> str | None:
    stdout = stdout.strip()
    if not stdout:
        return None
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and obj.get("status") == "error":
        code = obj.get("code")
        return code if isinstance(code, str) else None
    return None


def run_json(
    binary: str,
    args: Sequence[str],
    *,
    config_dir: Path | None = None,
    token: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = 30.0,
) -> Any:
    """Run `logoscore <args> --json` and return parsed JSON output."""
    cmd = [binary, *args, "--json"]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_prep_env(config_dir, token, env),
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise from_exit_code(
            proc.returncode,
            _format_failure(cmd, proc),
            stderr=proc.stderr,
            error_code=_error_code_from_stdout(proc.stdout),
        )
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise LogoscoreError(
            f"failed to parse JSON output from {' '.join(cmd)}: {e}",
            stderr=proc.stderr,
        ) from e
