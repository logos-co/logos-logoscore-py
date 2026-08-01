"""Subprocess helpers — invoke `logosctl` and parse JSON output.

Every client command runs `logosctl --json <subcommand> ...`. Stdout is
parsed as a single JSON value; non-zero exit codes are mapped to exception
types by `errors.from_exit_code`.

Set ``LOGOSCTL_PY_FORWARD_OUTPUT=1`` (or any truthy value) to mirror the
CLI's *stderr* to the parent process — handy for chasing CLI-side
warnings (e.g. "Failed to acquire plugin/replica" hangs) under pytest's
``-s``. Stdout is deliberately not forwarded: it carries the structured
JSON response (which the caller already receives via the function
return) and may include raw tokens from ``token issue``.

The session is selected through the environment rather than
``--config-dir``; see `_prep_env`.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import LogosctlError, from_exit_code


def decode_bytes_tags(value: Any) -> Any:
    """Decode the protocol's canonical tagged-bytes form into `bytes`.

    Since the logos-protocol extraction, byte arrays cross the JSON
    boundary as ``{"_bytes": "<base64url, unpadded>"}`` (NUL-safe,
    lossless). Consumers decode it exactly once at their boundary.
    Applied recursively so tagged values nested in maps/lists decode too.

    Lives here (not in ``client``) so both the call path (``client.call``)
    and the event path (``events.Subscription``) decode identically
    without an import cycle — ``events`` imports ``_proc`` but not
    ``client``.
    """
    if isinstance(value, dict):
        if set(value.keys()) == {"_bytes"} and isinstance(value["_bytes"], str):
            s = value["_bytes"]
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        return {k: decode_bytes_tags(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_bytes_tags(v) for v in value]
    return value


def encode_bytes_tag(value: bytes | bytearray) -> dict:
    """Inverse of :func:`decode_bytes_tags` — wrap raw bytes in the
    canonical tagged form so they survive a JSON round-trip losslessly
    (used when packing ``bytes`` nested inside a ``json:`` container arg).
    """
    s = base64.urlsafe_b64encode(bytes(value)).decode("ascii").rstrip("=")
    return {"_bytes": s}


def _forward_output_enabled() -> bool:
    return os.environ.get("LOGOSCTL_PY_FORWARD_OUTPUT", "").lower() in (
        "1", "true", "yes", "on",
    )


def _emit_captured(cmd: Sequence[str], stdout: str | None, stderr: str | None) -> None:
    """Print captured CLI *stderr* to the parent's stderr with a per-process
    header so multiple concurrent invocations stay disambiguated. Only
    runs when LOGOSCTL_PY_FORWARD_OUTPUT is set.

    Stdout is deliberately NOT forwarded: it carries the structured JSON
    response that the caller already receives via the function return,
    and `token issue` (and any future credential-issuing subcommand)
    embeds raw tokens there. Mirroring stdout into the parent process's
    stderr in a CI environment would write those tokens straight into
    the build log. The diagnostic value of this hook is in the CLI's
    qDebug/qWarning trail, all of which goes to stderr.
    """
    if not _forward_output_enabled():
        return
    _ = stdout  # intentionally unused — see docstring.
    header = f"[logosctl-py] {' '.join(cmd)}"
    print(header, file=sys.stderr, flush=True)
    if stderr:
        for line in stderr.splitlines():
            print(f"[logosctl-py stderr] {line}", file=sys.stderr, flush=True)


def _prep_env(
    config_dir: Path | None,
    token: str | None,
    extra_env: dict[str, str] | None,
) -> dict[str, str]:
    """Build the child environment.

    `LOGOSCTL_CONFIG_DIR` and `LOGOSCTL_TOKEN` are the only two variables
    the binary reads — the whole `LOGOSCORE_CLIENT_*` family that used to
    select a transport per call is gone, and with it `client._env_overrides`.
    Which daemon a client dials is now a document
    (`<config_dir>/client/config.yaml`), not an environment.

    Passing the session through the environment rather than `--config-dir`
    is deliberate: `--config-dir` is an app-level option, so it only works
    *before* the subcommand. Placed after one it is not recognised as a
    flag at all — a client subcommand's extras are handed to the command
    as positionals, so `call m meth --config-dir X` would call `meth` with
    two extra string arguments while still (via the CLI's argv pre-scan)
    selecting the right session. The env var has no ordering rule.
    """
    env = os.environ.copy()
    if config_dir is not None:
        env["LOGOSCTL_CONFIG_DIR"] = str(config_dir)
    if token is not None:
        env["LOGOSCTL_TOKEN"] = token
    if extra_env:
        env.update(extra_env)
    return env


def _format_failure(cmd: Sequence[str], proc: subprocess.CompletedProcess[str]) -> str:
    msg = f"logosctl command failed (exit {proc.returncode}): {' '.join(cmd)}"
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
    """Run `logosctl --json <args>` and return parsed JSON output.

    `env` is a plain escape hatch for the child environment (e.g. pinning
    a short `TMPDIR` so the local transport's `$TMPDIR/logos_<module>_<id>`
    socket path stays under the 104-byte `sun_path` limit on macOS). It no
    longer carries transport selection — that moved into the client config.
    """
    # Global flags go BEFORE the subcommand. `--json` trailing does still
    # work — main.cpp pulls -j/--json, --no-json/--human and -q/--quiet back
    # out of a client subcommand's leftovers — but that extraction is exactly
    # the problem: a `call` argument that is literally `--json` would be
    # swallowed as a flag instead of reaching the method. Emitting the flags
    # up front sidesteps the whole class of bug.
    cmd = [binary, "--json", *args]
    # We always pass `--verbose` here when forwarding is enabled so the
    # CLI emits its qDebug/qWarning trail; otherwise the SDK's
    # diagnostic logs (the "Failed to acquire plugin/replica…" warning
    # we're chasing) are silenced. It is app-level too, hence the same
    # placement.
    if _forward_output_enabled() and "--verbose" not in args and "-v" not in args:
        cmd = [binary, "--verbose", "--json", *args]
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
        raise LogosctlError(
            f"failed to parse JSON output from {' '.join(cmd)}: {e}",
            stderr=proc.stderr,
        ) from e
