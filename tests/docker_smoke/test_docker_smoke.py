"""Docker smoke tests — daemon in a container, client on the host, over TCP.

Three scenarios:

1. **Single-container, full API** — one daemon in docker; every Q_INVOKABLE
   in `test_basic_module` and both of its events are exercised. The fixture
   parametrises over codec (`json`, `cbor`) so the same matrix is replayed
   through both wire formats; that's the task's end-to-end confirmation
   that the codec pipeline handles every parameter/return type cleanly.

2. **Two-container smoke** — two daemons in separate containers, one TCP
   conversation apiece from the same test, and a sanity-check that each
   client is actually talking to its own daemon (distinct instance IDs,
   independent loaded-module state).

3. **Legacy smoke** — the original `test_docker_tcp_status` /
   `test_docker_tcp_load_and_call`, kept as a minimal fallback when the
   matrix fixtures are skipped for environmental reasons.

All the container lifecycle — volume mounts, port mapping, client
wiring — is handled by the public `LogoscoreDockerDaemon` helper. This
file is just the pytest glue + test assertions; external consumers
replicating this shape for their own modules should use the helper
directly (see `src/logoscore/docker_daemon.py`).

All tests are opt-in: they require docker on the host and a pre-built
`logoscore:smoke-<flavor>` image (see `build_smoke_image.sh`). If either
is missing the suite skips cleanly rather than failing.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Iterator

import pytest

from logoscore import (
    LogoscoreDockerDaemon,
    docker_available,
    image_present,
)

from .._basic_module_cases import BASIC_MODULE_CASES


# Docker image tag convention: logoscore:smoke-<flavor>, where <flavor>
# is `dev` or `portable`. Override for one-off images via the env var.
DOCKER_IMAGE_FMT = os.environ.get(
    "LOGOSCORE_DOCKER_IMAGE_FMT", "logoscore:smoke-{flavor}")
MODULE = "test_basic_module"


def _docker_image_for(flavor: str) -> str:
    return DOCKER_IMAGE_FMT.format(flavor=flavor)


# `docker_flavor` parametrisation lives in `conftest.py` so every file
# in this directory shares the hook. Running any single file (e.g. the
# SSL smoke alone) still gets the fixture injected.


# ── Environmental skip helpers ────────────────────────────────────────────

def _resolve_user_modules_dir(flavor: str) -> str:
    """Host path to bind-mount as `/user-modules` inside the container.

    The smoke image is intentionally a *bare CLI runtime* — it doesn't
    ship test_basic_module or any other consumer-authored module. The
    test driver mounts them in at runtime, which is also the shape
    end users are expected to adopt for their own modules.

    Two env vars because the two image flavors need differently-built
    module plugins:
      * `dev`      → `.install` modules (rpath-linked into /nix/store)
      * `portable` → `.install-portable` modules (self-contained libs)
    Using the wrong flavor's modules in the wrong image fails at dlopen
    time with missing libs, so pick explicitly per flavor.
    """
    env_var = (
        "LOGOSCORE_TEST_MODULES_DIR_PORTABLE"
        if flavor == "portable"
        else "LOGOSCORE_TEST_MODULES_DIR"
    )
    d = os.environ.get(env_var)
    if not d:
        pytest.skip(
            f"{env_var} not set — can't bind-mount user modules into "
            f"the {flavor} container. Enter the dev shell (`nix develop`) "
            f"or wire the var up in your own CI."
        )
    return d


def _require_docker_and_image(flavor: str) -> None:
    if not docker_available():
        pytest.skip("docker not available")
    image = _docker_image_for(flavor)
    if not image_present(image):
        pytest.skip(
            f"docker image '{image}' not built — run "
            f"FLAVOR={flavor} tests/docker_smoke/build_smoke_image.sh first"
        )


# ── Legacy single-container fixture (kept for the old fallback tests) ─────

@pytest.fixture(scope="module")
def dockerized_daemon(docker_flavor) -> Iterator[LogoscoreDockerDaemon]:
    _require_docker_and_image(docker_flavor)
    modules_dir = _resolve_user_modules_dir(docker_flavor)
    try:
        with LogoscoreDockerDaemon(
            image=_docker_image_for(docker_flavor),
            modules_dir=modules_dir,
        ) as daemon:
            yield daemon
    except Exception as e:
        pytest.fail(f"daemon ({docker_flavor}) failed to start: {e}")


def test_docker_tcp_status(dockerized_daemon, logoscore_bin):
    client = dockerized_daemon.client(binary=logoscore_bin)
    status = client.status()
    assert isinstance(status, dict)
    assert "daemon" in status or "modules" in status


def test_docker_tcp_load_and_call(dockerized_daemon, logoscore_bin):
    client = dockerized_daemon.client(binary=logoscore_bin)
    modules = client.list_modules()
    names = {m.get("name") for m in modules if isinstance(m, dict)}
    assert any("test_basic" in (n or "") for n in names), names
    client.load_module(MODULE)
    assert client.call(MODULE, "echo", "hello") is not None


# ── Matrix fixture: one daemon per codec, reused across the full test set ─

@pytest.fixture(scope="module", params=["json", "cbor"])
def docker_matrix_client(request, logoscore_bin, docker_flavor):
    """Yield a LogoscoreClient connected over TCP to a fresh docker daemon
    running with the requested wire codec + flavor. Module-scoped so the
    ~40-case matrix below doesn't spin up a container per test."""
    _require_docker_and_image(docker_flavor)
    modules_dir = _resolve_user_modules_dir(docker_flavor)
    codec = request.param

    with LogoscoreDockerDaemon(
        image=_docker_image_for(docker_flavor),
        modules_dir=modules_dir,
        codec=codec,
    ) as daemon:
        client = daemon.client(binary=logoscore_bin)
        client.load_module(MODULE)
        yield client


@pytest.mark.parametrize(
    "method,args,expected", BASIC_MODULE_CASES,
    ids=[f"{m}{args!r}" for m, args, _ in BASIC_MODULE_CASES],
)
def test_docker_basic_module_method(docker_matrix_client, method, args, expected):
    """Every Q_INVOKABLE on test_basic_module round-trips cleanly through
    the docker-hosted daemon. Runs twice (once per codec parameter on the
    fixture) so the same matrix is validated on both JSON and CBOR."""
    got = docker_matrix_client.call(MODULE, method, *args)
    if expected is None:
        # Dispatched cleanly — we just wanted the RPC to not raise.
        return
    assert got == expected, f"{method}{args!r} -> {got!r}, expected {expected!r}"


def test_docker_basic_module_emit_test_event(docker_matrix_client):
    """Event: emitTestEvent(payload) fires a `testEvent` on the module; the
    payload must arrive through `logoscore watch` and back to the client."""
    received: list[dict] = []
    evt = threading.Event()

    def on_event(event: dict) -> None:
        received.append(event)
        evt.set()

    with docker_matrix_client.on_event(MODULE, "testEvent", on_event):
        time.sleep(0.5)  # let the watcher subscribe before emit
        docker_matrix_client.call(MODULE, "emitTestEvent", "payload-docker")
        assert evt.wait(timeout=10.0), "testEvent not received"

    assert received, "expected at least one event"
    assert "payload-docker" in str(received[0])


def test_docker_basic_module_emit_multi_arg_event(docker_matrix_client):
    """Event with two args (QString + int) — verifies both the codec and
    the event-forwarding chain preserve parameter shapes."""
    received: list[dict] = []
    evt = threading.Event()

    def on_event(event: dict) -> None:
        received.append(event)
        evt.set()

    with docker_matrix_client.on_event(MODULE, "multiArgEvent", on_event):
        time.sleep(0.5)
        docker_matrix_client.call(MODULE, "emitMultiArgEvent", "docker-label", 77)
        assert evt.wait(timeout=10.0), "multiArgEvent not received"

    flat = str(received[0])
    assert "docker-label" in flat
    assert "77" in flat


# ── Two-daemon test: one client process, two independent containers ──────

@pytest.fixture(scope="module")
def two_dockerized_daemons(docker_flavor):
    """Spin up two independent daemon containers on different host ports.
    Used by `test_two_daemons_in_docker` to verify that the wrapper
    doesn't accidentally share any global state between daemon handles."""
    _require_docker_and_image(docker_flavor)
    modules_dir = _resolve_user_modules_dir(docker_flavor)

    daemons: list[LogoscoreDockerDaemon] = []
    try:
        for label in ("alpha", "beta"):
            d = LogoscoreDockerDaemon(
                image=_docker_image_for(docker_flavor),
                modules_dir=modules_dir,
                container_name=f"logoscore-twopair-{docker_flavor}-{label}",
            )
            d.start()
            daemons.append(d)
        yield daemons
    finally:
        for d in daemons:
            d.stop()


def test_two_daemons_in_docker(two_dockerized_daemons, logoscore_bin):
    """Talk to two containerised daemons from one test process: each client
    is bound to its own config_dir and --tcp-host localhost. Confirm the
    wrapper doesn't leak state between them by (a) reading distinct
    instance_ids from each connection file, (b) loading the module on
    daemon A but NOT on B, and (c) verifying only A reports it loaded
    while B remains untouched."""
    daemons = two_dockerized_daemons
    assert len(daemons) == 2

    clients = [d.client(binary=logoscore_bin) for d in daemons]
    instance_ids = [
        json.loads((d.config_dir / "daemon.json").read_text())["instance_id"]
        for d in daemons
    ]

    # Distinct instances (these are UUID-derived, collision is negligible).
    assert instance_ids[0] != instance_ids[1], (
        f"expected distinct instance IDs, got {instance_ids}"
    )

    # Both daemons reachable independently.
    for i, c in enumerate(clients):
        status = c.status()
        assert status.get("daemon", {}).get("status") == "running", (
            f"daemon #{i} not running: {status}"
        )

    # Load module on A only, then check module state on both.
    clients[0].load_module(MODULE)

    def _is_loaded(client):
        return any(
            m.get("name") == MODULE and m.get("status") == "loaded"
            for m in client.list_modules()
            if isinstance(m, dict)
        )

    assert _is_loaded(clients[0]),     "A should have the module loaded"
    assert not _is_loaded(clients[1]), "B should NOT have the module loaded"

    # Call a method on A, confirm it returns what we expect; then prove B
    # still doesn't have the module by asking it to echo — which should
    # fail because the module isn't loaded there.
    assert clients[0].call(MODULE, "echo", "two-daemon") == "two-daemon"

    from logoscore.errors import MethodError, ModuleError
    with pytest.raises((MethodError, ModuleError)):
        clients[1].call(MODULE, "echo", "two-daemon")
