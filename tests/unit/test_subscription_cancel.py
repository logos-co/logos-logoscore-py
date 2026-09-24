"""Subscription.cancel() where the watcher cannot be sent SIGINT.

On Windows, Popen.send_signal(SIGINT) raises ValueError. cancel() used to let
it escape, so leaving every `with ... on_event(...)` block raised there.
"""
import signal
import subprocess
import threading

from logoscore.events import Subscription


class _WindowsLikeWatcher:
    """A Popen stand-in that, like Windows', has no SIGINT to send."""

    def __init__(self) -> None:
        self.terminated = False

    def poll(self):
        return 1 if self.terminated else None

    def send_signal(self, sig) -> None:
        if sig == signal.SIGINT:
            raise ValueError(f"Unsupported signal: {sig}")

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        if not self.terminated:
            raise subprocess.TimeoutExpired("watch", timeout)
        return 1


def test_cancel_ends_a_watcher_that_takes_no_sigint() -> None:
    watcher = _WindowsLikeWatcher()
    reader = threading.Thread(target=lambda: None)
    reader.start()
    sub = Subscription(watcher, reader, callback=lambda event: None, error_callback=None)
    sub.cancel(timeout=0.1)
    assert watcher.terminated
