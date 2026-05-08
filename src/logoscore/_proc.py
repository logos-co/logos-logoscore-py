"""Subprocess helpers — invoke `logoscore` and parse JSON output.

Every client command runs `logoscore <subcommand> ... --json`. Stdout is
parsed as a single JSON value; non-zero exit codes are mapped to exception
types by `errors.from_exit_code`.

Set ``LOGOSCORE_PY_FORWARD_OUTPUT=1`` (or any truthy value) to mirror the
CLI's *stderr* to the parent process — handy for chasing CLI-side
warnings (e.g. "Failed to acquire plugin/replica" hangs) under pytest's
``-s``. Stdout is deliberately not forwarded: it carries the structured
JSON response (which the caller already receives via the function
return) and may include raw tokens from ``issue-token``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import LogoscoreError, from_exit_code


def _forward_output_enabled() -> bool:
    return os.environ.get("LOGOSCORE_PY_FORWARD_OUTPUT", "").lower() in (
        "1", "true", "yes", "on",
    )


def _emit_captured(cmd: Sequence[str], stdout: str | None, stderr: str | None) -> None:
    """Print captured CLI *stderr* to the parent's stderr with a per-process
    header so multiple concurrent invocations stay disambiguated. Only
    runs when LOGOSCORE_PY_FORWARD_OUTPUT is set.

    Stdout is deliberately NOT forwarded: it carries the structured JSON
    response that the caller already receives via the function return,
    and `issue-token` (and any future credential-issuing subcommand)
    embeds raw tokens there. Mirroring stdout into the parent process's
    stderr in a CI environment would write those tokens straight into
    the build log. The diagnostic value of this hook is in the CLI's
    qDebug/qWarning trail, all of which goes to stderr.
    """
    if not _forward_output_enabled():
        return
    _ = stdout  # intentionally unused — see docstring.
    header = f"[logoscore-py] {' '.join(cmd)}"
    print(header, file=sys.stderr, flush=True)
    if stderr:
        for line in stderr.splitlines():
            print(f"[logoscore-py stderr] {line}", file=sys.stderr, flush=True)


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
    # We always pass `--verbose` here when forwarding is enabled so the
    # CLI emits its qDebug/qWarning trail; otherwise the SDK's
    # diagnostic logs (the "Failed to acquire plugin/replica…" warning
    # we're chasing) are silenced.
    if _forward_output_enabled() and "--verbose" not in args and "-v" not in args:
        cmd = [binary, "--verbose", *args, "--json"]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_prep_env(config_dir, token, env),
        timeout=timeout,
    )
    _emit_captured(cmd, proc.stdout, proc.stderr)
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
