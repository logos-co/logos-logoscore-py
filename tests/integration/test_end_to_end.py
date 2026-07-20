"""End-to-end integration tests against a real logoscore daemon.

Uses `logos-test-modules` (its `test_fullapi_cpp` exposes
`fireStringEvent(v)`, a bool-returning trigger that emits a `stringEvent`
carrying `v` as its `data.arg0` payload — see
`test_fullapi_module_cpp.py` for the full type surface).
"""
from __future__ import annotations

import threading
import time

import pytest

from logoscore import LogoscoreDaemon

MODULE = "test_fullapi_cpp"


@pytest.fixture
def daemon(
    logoscore_bin,
    test_modules_dir,
    transport,
    tcp_port,
    tcp_ssl_port,
    request,
):
    # transport == "local"   — QLocalSocket; zero extra flags.
    # transport == "tcp"     — adds a `--module-transport ...=tcp...`
    #                          listener pinned to `tcp_port`.
    # transport == "tcp_ssl" — same shape, plus the daemon needs
    #                          ssl_cert / ssl_key. We pull those from
    #                          the session-scoped `self_signed_cert`
    #                          fixture, but only request it when
    #                          actually needed so tcp / local runs
    #                          don't pay the openssl cost.
    kwargs = {}
    if transport != "local":
        kwargs["transports"] = [transport]
        if transport == "tcp":
            kwargs["tcp_port"] = tcp_port
        elif transport == "tcp_ssl":
            cert, key = request.getfixturevalue("self_signed_cert")
            kwargs["tcp_ssl_port"] = tcp_ssl_port
            kwargs["ssl_cert"] = cert
            kwargs["ssl_key"] = key
    with LogoscoreDaemon(
        modules_dir=test_modules_dir, binary=logoscore_bin, **kwargs,
    ) as d:
        yield d


@pytest.fixture
def client(daemon, transport):
    """Return a callable that builds a daemon client wired to the
    transport being tested. Replaces an earlier import-time monkeypatch
    of ``LogoscoreDaemon.client`` that leaked across the whole test
    session and could break tests depending on import order.

    For `tcp_ssl`, defaults `no_verify_peer=True` — the daemon is
    using the throwaway self-signed cert from the `self_signed_cert`
    fixture, which won't validate against a real CA. Tests that want
    to exercise the verification path can override by passing
    `no_verify_peer=False` and pointing at a CA bundle.
    """
    def _make(**kw):
        kw.setdefault("transport", transport)
        if transport == "tcp_ssl":
            kw.setdefault("no_verify_peer", True)
        return daemon.client(**kw)
    return _make


def test_daemon_starts_and_reports_status(daemon, client):
    status = client().status()
    assert isinstance(status, dict)
    assert daemon.connection_file.exists()


def test_list_modules_returns_entries(client):
    mods = client().list_modules()
    assert isinstance(mods, list)
    names = {m.get("name") for m in mods if isinstance(m, dict)}
    assert any(n and "test_fullapi" in n for n in names), names


def test_load_call_and_event_roundtrip(client):
    conn = client()

    conn.load_module(MODULE)

    received: list[dict] = []
    received_evt = threading.Event()

    def on_event(event: dict) -> None:
        received.append(event)
        received_evt.set()

    # Re-fire the (idempotent) trigger until the event lands — a fixed
    # sleep can't cover a slow-to-subscribe watcher on CI.
    with conn.on_event(MODULE, "stringEvent", on_event):
        deadline = time.monotonic() + 20.0
        while True:
            assert conn.call(MODULE, "fireStringEvent", "hello from python") is True
            if received_evt.wait(timeout=1.0):
                break
            assert time.monotonic() < deadline, "event not received in time"

    assert received, "expected at least one event"
    evt = received[0]
    assert evt.get("event") == "stringEvent" or evt.get("event") is None  # schema tolerance
    # payload should flow through
    payload = evt.get("data") if isinstance(evt.get("data"), dict) else evt
    assert any("hello from python" in str(v) for v in payload.values())


def test_isolated_config_dir_is_used(daemon):
    # The daemon's connection file must live under its isolated config_dir,
    # not in the user's ~/.logoscore.
    assert daemon.connection_file.exists()
    assert str(daemon.connection_file).startswith(str(daemon.config_dir))
