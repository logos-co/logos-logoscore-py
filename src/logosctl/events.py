"""Event subscriptions backed by a `logosctl watch` subprocess.

Each subscription owns a background thread that reads NDJSON from the
watcher's stdout and dispatches each parsed event to a user callback.
Cancel a subscription by calling `.cancel()` (or using it as a context
manager) — this signals the watcher process and joins the thread.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Sequence

from . import _proc

_log = logging.getLogger(__name__)


class Subscription:
    """A live event subscription. Returned by `LogosctlClient.on_event`."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        thread: threading.Thread,
        callback: Callable[[dict], None],
        error_callback: Callable[[BaseException], None] | None,
    ) -> None:
        self._process = process
        self._thread = thread
        self._callback = callback
        self._error_callback = error_callback
        self._cancelled = False
        # Bounded tail of the watcher's stderr, filled by a drain thread.
        # Bounded because a chatty watcher would otherwise grow it without
        # limit over a long subscription, and only the end is diagnostic.
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._stderr_thread: threading.Thread | None = None

    @classmethod
    def start(
        cls,
        *,
        binary: str,
        args: Sequence[str],
        config_dir: Path | None,
        token: str | None,
        callback: Callable[[dict], None],
        error_callback: Callable[[BaseException], None] | None,
        extra_env: dict[str, str] | None = None,
    ) -> "Subscription":
        # These two are the only variables logosctl reads. `watch` is an RPC
        # like any other client command, so which daemon it reaches is decided
        # by `<config_dir>/client/config.yaml`, not by the environment — the
        # LOGOSCORE_CLIENT_* family that used to retarget a single call no
        # longer exists. `extra_env` therefore carries ordinary process env
        # (TMPDIR for the local socket path, forwarding switches), never a
        # dial spec.
        env = os.environ.copy()
        if config_dir is not None:
            env["LOGOSCTL_CONFIG_DIR"] = str(config_dir)
        if token is not None:
            env["LOGOSCTL_TOKEN"] = token
        if extra_env:
            env.update(extra_env)

        # --json goes before the subcommand: a client subcommand collects
        # everything it doesn't recognize into remaining(), out of which the
        # CLI extracts the global flags — so a trailing --json works, but any
        # argument that happens to *be* `--json` is eaten by that same
        # extractor. Emitting global flags up front removes the whole class of
        # bug and matches the documented surface.
        cmd = [binary, "--json", *args]
        # start_new_session lets us signal the whole process group if needed
        # and keeps the watcher from being killed by Ctrl+C in the parent.
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            bufsize=1,  # line-buffered
            start_new_session=True,
        )
        sub = cls(process, None, callback, error_callback)  # type: ignore[arg-type]
        thread = threading.Thread(
            target=sub._pump,
            name=f"logosctl-watch-{'-'.join(args)}",
            daemon=True,
        )
        sub._thread = thread
        thread.start()
        # stderr is piped, so SOMETHING has to read it. A pipe nobody drains
        # fills at 64K and blocks the child mid-write — and a watcher blocked
        # writing stderr stops writing stdout, so the event stream silently
        # stalls forever. Draining rather than sending it to /dev/null keeps
        # the diagnostics: a watcher that dies should be able to say why.
        stderr_thread = threading.Thread(
            target=sub._drain_stderr,
            name=f"logosctl-watch-stderr-{'-'.join(args)}",
            daemon=True,
        )
        sub._stderr_thread = stderr_thread
        stderr_thread.start()
        return sub

    def _drain_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        try:
            for line in stream:
                self._stderr_tail.append(line.rstrip("\n"))
        except Exception:  # noqa: BLE001 — draining must never raise
            pass

    @property
    def stderr_tail(self) -> str:
        """The tail of the watcher's stderr. Empty when it said nothing."""
        return "\n".join(self._stderr_tail)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and self._process.poll() is None

    def cancel(self, timeout: float = 5.0) -> None:
        """Signal the watcher to stop and wait for the thread to exit."""
        if self._cancelled:
            return
        self._cancelled = True
        if self._process.poll() is None:
            try:
                # `watch` parks in QCoreApplication::exec() with no handler of
                # its own (see watch_command.cpp), so SIGINT ends it via the
                # default disposition. Fall back to SIGTERM / SIGKILL.
                self._process.send_signal(signal.SIGINT)
                try:
                    self._process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait()
            except ProcessLookupError:
                pass
        self._thread.join(timeout=timeout)
        # The child is gone, so its stderr is at EOF and this returns promptly.
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=timeout)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cancel()

    # ── Internal ────────────────────────────────────────────────────────────

    def _pump(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None
        try:
            for line in stdout:
                if self._cancelled:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as e:
                    self._report_error(e)
                    continue
                # Decode tagged bytes (`{"_bytes": "<b64url>"}`) into real
                # `bytes` so typed byte-array event payloads reach the
                # callback in the same shape `client.call` returns them. A
                # malformed tag (e.g. invalid base64) must not tear down the
                # subscription — report and skip, same as a JSON parse error.
                try:
                    event = _proc.decode_bytes_tags(event)
                except Exception as e:  # noqa: BLE001 — bad tag shouldn't end the pump
                    self._report_error(e)
                    continue
                try:
                    self._callback(event)
                except Exception as e:  # noqa: BLE001 — user callback is untrusted
                    self._report_error(e)
        except Exception as e:  # noqa: BLE001
            self._report_error(e)

        # The stream ended. If that was the watcher dying rather than us
        # cancelling it, say so — and say what it wrote on the way out. A
        # subscription that goes quiet because its process exited is otherwise
        # indistinguishable from one where nothing happened to be emitted.
        if not self._cancelled:
            code = self._process.poll()
            if code:
                if self._stderr_thread is not None:
                    self._stderr_thread.join(timeout=1.0)
                tail = self.stderr_tail.strip()
                self._report_error(RuntimeError(
                    f"watch exited with code {code}"
                    + (f"\n{tail}" if tail else "")))

    def _report_error(self, exc: BaseException) -> None:
        if self._error_callback is not None:
            try:
                self._error_callback(exc)
            except Exception:  # noqa: BLE001
                _log.exception("error_callback itself raised")
        else:
            _log.warning("logosctl event handler error: %s", exc)
