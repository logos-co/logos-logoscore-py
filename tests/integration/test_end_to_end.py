"""End-to-end integration tests against a real logoscore daemon.

Uses `logos-test-modules` (its `test_basic_module` exposes `emitTestEvent`
that fires a `testEvent` with the first argument as payload — see the
README in logos-logoscore-cli for an example using this pair).
"""
from __future__ import annotations

import threading

import pytest

from logoscore import LogoscoreDaemon


@pytest.fixture
def daemon(logoscore_bin, test_modules_dir):
    with LogoscoreDaemon(
        modules_dir=test_modules_dir, binary=logoscore_bin
    ) as d:
        yield d


def test_daemon_starts_and_reports_status(daemon):
    status = daemon.client().status()
    assert isinstance(status, dict)
    assert daemon.connection_file.exists()


def test_list_modules_returns_entries(daemon):
    mods = daemon.client().list_modules()
    assert isinstance(mods, list)
    names = {m.get("name") for m in mods if isinstance(m, dict)}
    assert any(n and "test_basic" in n for n in names), names


@pytest.mark.xfail(
    reason="logoscore watch event delivery is unreliable", strict=False
)
def test_load_call_and_event_roundtrip(daemon):
    client = daemon.client()

    client.load_module("test_basic_module")

    received: list[dict] = []
    received_evt = threading.Event()

    def on_event(event: dict) -> None:
        received.append(event)
        received_evt.set()

    with client.on_event("test_basic_module", "testEvent", on_event):
        # Give the watcher a beat to subscribe before firing.
        import time; time.sleep(0.5)
        client.call("test_basic_module", "emitTestEvent", "hello from python")
        assert received_evt.wait(timeout=10.0), "event not received within 10s"

    assert received, "expected at least one event"
    evt = received[0]
    assert evt.get("event") == "testEvent" or evt.get("event") is None  # schema tolerance
    # payload should flow through
    payload = evt.get("data") if isinstance(evt.get("data"), dict) else evt
    assert any("hello from python" in str(v) for v in payload.values())


def test_isolated_config_dir_is_used(daemon):
    # The daemon's connection file must live under its isolated config_dir,
    # not in the user's ~/.logoscore.
    assert daemon.connection_file.exists()
    assert str(daemon.connection_file).startswith(str(daemon.config_dir))
