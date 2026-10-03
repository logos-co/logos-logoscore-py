"""Docker smoke tests — a logosctl daemon in a container, operated from the
host over Remote Runtime Control.

`LogosctlDockerDaemon` runs the daemon on the host's network with a
`peering` section that turns runtime control on, pairs a host-side logosctl
(a config dir of its own, no daemon) through a runtime-control invite
accepted inside the container, and grants it `DEFAULT_GRANTS`. Every client
call below crosses that pairing: a `tls_tcp` session from the host's
logosctl to core_service in the container.

1. **Full API** — every echo method and every typed event on
   `test_fullapi_cpp`, through the pairing.
2. **The daemon's policy decides** — an ungranted method is refused, and a
   grant holds from the next call.
3. **Two containers** — two daemons, one paired client each: distinct
   instance and runtime IDs, independent module state.

Opt-in: they need docker on the host and a pre-built `logosctl:smoke-<flavor>`
image (see `build_smoke_image.sh`), and skip cleanly without either. Host
networking is complete on Linux only; Docker Desktop has it as an opt-in.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Iterator

import pytest

from logosctl import (
    LogosctlDockerDaemon,
    LogosctlError,
    docker_available,
    image_present,
)

from .._fullapi_module_cases import FULLAPI_EVENT_CASES, FULLAPI_METHOD_CASES

# Docker image tag convention: logosctl:smoke-<flavor>, where <flavor> is
# `dev` or `portable`. Override for one-off images via the env var.
DOCKER_IMAGE_FMT = os.environ.get(
    "LOGOSCTL_DOCKER_IMAGE_FMT", "logosctl:smoke-{flavor}")
MODULE = "test_fullapi_cpp"


def _docker_image_for(flavor: str) -> str:
    return DOCKER_IMAGE_FMT.format(flavor=flavor)


def _require_docker_and_image(flavor: str) -> None:
    if not docker_available():
        pytest.skip("docker not available")
    image = _docker_image_for(flavor)
    if not image_present(image):
        pytest.skip(
            f"docker image '{image}' not built — run "
            f"FLAVOR={flavor} tests/docker_smoke/build_smoke_image.sh first"
        )


@pytest.fixture(scope="module")
def dockerized_daemon(
    docker_flavor, linux_test_modules_dir, logosctl_bin,
) -> Iterator[LogosctlDockerDaemon]:
    """One daemon per flavor, with the module loaded; the matrix below
    reuses it rather than spinning up a container per case."""
    _require_docker_and_image(docker_flavor)
    with LogosctlDockerDaemon(
        image=_docker_image_for(docker_flavor),
        modules_dir=linux_test_modules_dir,
        binary=logosctl_bin,
    ) as daemon:
        daemon.client().load_module(MODULE)
        yield daemon


@pytest.fixture(scope="module")
def client(dockerized_daemon):
    return dockerized_daemon.client()


def test_docker_status_over_runtime_control(dockerized_daemon, client):
    """`status` answered by core_service in the container — no fallback built
    from a state file, which a remote client does not have — to a client the
    daemon enrolled for runtime control."""
    status = client.status()
    assert "rpc_error" not in status, status
    assert status["daemon"]["status"] == "running", status
    me = dockerized_daemon.runtime_control.runtime_id
    assert any(p["runtime_id"] == me and "runtime-control" in p["uses"]
               for p in dockerized_daemon.peer("ls")["peers"])


# ── Full API: every type as param / return / event, through the pairing ─────


@pytest.mark.parametrize(
    "method,args,expected", FULLAPI_METHOD_CASES,
    ids=[f"{m}{args!r}" for m, args, _ in FULLAPI_METHOD_CASES],
)
def test_docker_fullapi_method(client, method, args, expected):
    got = client.call(MODULE, method, *args)
    if expected is None:
        return
    assert got == expected, f"{method}{args!r} -> {got!r}, expected {expected!r}"


@pytest.mark.parametrize(
    "event,fire_method,value", FULLAPI_EVENT_CASES,
    ids=[f"{c[0]}-{i}" for i, c in enumerate(FULLAPI_EVENT_CASES)],
)
def test_docker_fullapi_event(client, event, fire_method, value):
    """Re-fires the (idempotent) trigger until the event arrives or a
    deadline, so a slow-to-subscribe watcher can't miss the only emission."""
    received: list[dict] = []
    got = threading.Event()

    def on_event(e: dict) -> None:
        received.append(e)
        got.set()

    with client.on_event(MODULE, event, on_event):
        deadline = time.monotonic() + 20.0
        while True:
            assert client.call(MODULE, fire_method, value) is True
            if got.wait(timeout=1.0):
                break
            assert time.monotonic() < deadline, f"{event} not received in time"

    assert received[0]["event"] == event
    payload = received[0]["data"]["arg0"]
    assert payload == value, f"{event} payload {payload!r} != {value!r}"


# ── The daemon's remote policy decides each call ─────────────────────────────


def test_docker_the_policy_decides(dockerized_daemon, client):
    rc = dockerized_daemon.runtime_control
    try:
        rc.grant({"core_service": ["getStatus", "callModuleMethod"], MODULE: ["echoString"]})
        assert client.call(MODULE, "echoString", "granted") == "granted"
        with pytest.raises(LogosctlError) as excinfo:
            client.call(MODULE, "echoInt", 3)
        assert excinfo.value.code == "NOT_AUTHORISED"
    finally:
        rc.grant()
    assert client.call(MODULE, "echoInt", 3) == 3


# ── Two containers, one paired client each ───────────────────────────────────


@pytest.fixture(scope="module")
def two_dockerized_daemons(docker_flavor, linux_test_modules_dir, logosctl_bin):
    _require_docker_and_image(docker_flavor)
    daemons: list[LogosctlDockerDaemon] = []
    try:
        for name in ("alpha", "beta"):
            d = LogosctlDockerDaemon(
                image=_docker_image_for(docker_flavor),
                modules_dir=linux_test_modules_dir,
                binary=logosctl_bin,
                name=name,
            )
            d.start()
            daemons.append(d)
        yield daemons
    finally:
        for d in daemons:
            d.stop()


def test_two_daemons_in_docker(two_dockerized_daemons):
    """Each client operates its own daemon: distinct instance and runtime
    IDs, and a module loaded on A is not loaded on B."""
    a, b = two_dockerized_daemons
    instance_ids = [a.instance_id, b.instance_id]
    assert all(instance_ids) and instance_ids[0] != instance_ids[1], instance_ids
    assert a.runtime_control.daemon_id != b.runtime_control.daemon_id

    clients = [a.client(), b.client()]
    for c in clients:
        assert c.status()["daemon"]["status"] == "running"

    clients[0].load_module(MODULE)

    def _is_loaded(client) -> bool:
        return any(m.get("name") == MODULE and m.get("status") == "loaded"
                   for m in client.list_modules() if isinstance(m, dict))

    assert _is_loaded(clients[0]), "A should have the module loaded"
    assert not _is_loaded(clients[1]), "B should NOT have the module loaded"
    assert clients[0].call(MODULE, "echoString", "two-daemon") == "two-daemon"
