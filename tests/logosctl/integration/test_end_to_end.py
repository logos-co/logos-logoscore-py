"""End-to-end integration tests against a real logosctl daemon.

Uses `logos-test-modules` (its `test_fullapi_cpp` exposes
`fireStringEvent(v)`, a bool-returning trigger that emits a `stringEvent`
carrying `v` as its `data.arg0` payload — see
`test_fullapi_module_cpp.py` for the full type surface).

Deliberate duplicate of `tests/integration/test_end_to_end.py`: that file
drives the `logoscore` CLI, this one drives `logosctl`, and the two
surfaces genuinely differ (flags vs. config documents — see the `daemon`
fixture). Keeping them apart means retiring logoscore is a delete of
`src/logosctl`'s counterpart tree, not an unpick. Please don't merge them
back together.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from logosctl import LogosctlDaemon

MODULE = "test_fullapi_cpp"


@pytest.fixture
def daemon(
    logosctl_bin,
    test_modules_dir,
    transport,
    tcp_port,
    tcp_ssl_port,
    request,
):
    # Same three-way split as the logoscore suite, but every one of these
    # kwargs now lands in the daemon's YAML document rather than on its
    # command line — the flags that used to express them are gone.
    #
    # transport == "local"   — the daemon prepends a local listener to
    #                          every module unconditionally, so the
    #                          document names no listener at all.
    # transport == "tcp"     — one `protocol: tcp` listener per
    #                          well-known module, core_service pinned to
    #                          `tcp_port`.
    # transport == "tcp_ssl" — same shape plus per-listener cert/key,
    #                          pulled from the session-scoped
    #                          `self_signed_cert` fixture. Requested only
    #                          when actually needed so local / tcp runs
    #                          don't pay the openssl cost.
    #
    # capability_module's port is deliberately left unset (0 → the daemon
    # allocates an ephemeral): it needs a port of its own, since two
    # listeners can't share an address:port pair, and the client reads
    # whatever got bound back out of `state.json` anyway.
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
    with LogosctlDaemon(
        modules_dir=test_modules_dir, binary=logosctl_bin, **kwargs,
    ) as d:
        yield d


@pytest.fixture
def client(daemon):
    """Return a callable that builds a client for `daemon`.

    Unlike the logoscore fixture this takes no transport argument, and
    that absence is the whole point of the port: the transport is a
    property of the daemon's on-disk dial spec, which `LogosctlDaemon`
    rewrites from `state.json` once the listeners have really bound.
    There is no per-call override left — the `LOGOSCORE_CLIENT_*` env
    family that used to carry one was deleted.

    Nor does `tcp_ssl` need a `no_verify_peer` default here: the cert is
    the throwaway self-signed one from `self_signed_cert`, which carries
    no subjectAltName and wouldn't validate against any CA, and
    `LogosctlDaemon` already writes `verify_peer: false` for exactly that
    reason. Exercising the verification path means building the daemon
    with `verify_peer=True` and an `ssl_ca`, plus a cert minted with
    `subjectAltName=IP:127.0.0.1` — not an argument here.
    """
    def _make(**kw):
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
    # The daemon's state file must live under its isolated config_dir,
    # not in the user's ~/.logosctl.
    assert daemon.connection_file.exists()
    assert str(daemon.connection_file).startswith(str(daemon.config_dir))


def test_client_in_a_separate_config_dir_reaches_the_daemon(daemon, tmp_path):
    """Drive the daemon from a config dir it doesn't own.

    logoscore expressed this with the `LOGOSCORE_CLIENT_*` env vars, which
    merged a dial spec over the on-disk one per call. The whole family was
    deleted, so a client living outside the daemon's session now needs a
    `client/config.yaml` of its own plus a token the daemon accepts —
    which is exactly what `remote_client` writes, and the only remaining
    way to reach a daemon whose session you don't own. Worth its own test
    for that reason: it is the one path with no env-var fallback behind
    it.

    The spec goes into a dir the daemon does NOT own — it rewrites
    `client/config.yaml` in its own session whenever the recorded instance
    id stops matching, which would silently replace one written there.
    """
    remote = daemon.remote_client(tmp_path / "remote")
    assert isinstance(remote.status(), dict)
    names = {m.get("name") for m in remote.list_modules()}
    assert any(n and "test_fullapi" in n for n in names), names


def test_installed_config_document_drives_the_daemon(daemon, test_modules_dir):
    """The modules dir reaches the daemon as configuration, not as a flag.

    logoscore passed `-m <dir>`, so a wrapper bug there showed up as a
    parse error. Here the value is a `modules_dirs` entry in a document
    installed by `daemon config set` before boot: get it wrong and the
    daemon starts perfectly happily with no modules dir at all. This
    pins the document the CLI actually accepted (it re-emits it as
    canonical YAML, so only the path is asserted, not the bytes).
    """
    assert daemon.daemon_config_file.exists()
    installed = daemon.daemon_config_file.read_text()
    # The wrapper absolutises every path it emits: a relative
    # `modules_dirs` entry would resolve against the daemon process's
    # cwd, which is not the caller's.
    assert str(Path(test_modules_dir).expanduser().absolute()) in installed
