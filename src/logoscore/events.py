"""Event subscriptions backed by a `logoscore watch` subprocess.

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
from pathlib import Path
from typing import Callable, Sequence

_log = logging.getLogger(__name__)


class Subscription:
    """A live event subscription. Returned by `LogoscoreClient.on_event`."""

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
        env = os.environ.copy()
        if config_dir is not None:
            env["LOGOSCORE_CONFIG_DIR"] = str(config_dir)
        if token is not None:
            env["LOGOSCORE_TOKEN"] = token
        if extra_env:
            env.update(extra_env)

        cmd = [binary, *args, "--json"]
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
            name=f"logoscore-watch-{'-'.join(args)}",
            daemon=True,
        )
        sub._thread = thread
        thread.start()
        return sub

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
                # SIGINT triggers the CLI's graceful shutdown path
                # (see watch_command.cpp). Fall back to SIGTERM / SIGKILL.
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
                try:
                    self._callback(event)
                except Exception as e:  # noqa: BLE001 — user callback is untrusted
                    self._report_error(e)
        except Exception as e:  # noqa: BLE001
            self._report_error(e)

    def _report_error(self, exc: BaseException) -> None:
        if self._error_callback is not None:
            try:
                self._error_callback(exc)
            except Exception:  # noqa: BLE001
                _log.exception("error_callback itself raised")
        else:
            _log.warning("logoscore event handler error: %s", exc)
