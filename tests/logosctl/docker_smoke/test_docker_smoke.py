"""Docker smoke tests — daemon in a container, client on the host, over TCP.

Three scenarios:

1. **Single-container, full API** — one daemon in docker; every echo method
   and every typed event on `test_fullapi_cpp` is exercised, replayed over
   both wire codecs (`json`, `cbor`).

2. **Two-container smoke** — two daemons in separate containers, one TCP
   conversation apiece from the same test, and a sanity-check that each
   client is actually talking to its own daemon (distinct instance IDs,
   independent loaded-module state).

3. **Legacy smoke** — `test_docker_tcp_status` / `test_docker_tcp_load_and_call`,
   a minimal fallback when the matrix fixtures are skipped for
   environmental reasons.

The container lifecycle — config installation, volume mounts, port
mapping, client wiring — is handled by `LogosctlDockerDaemon`
(`src/logosctl/docker_daemon.py`).

All tests are opt-in: they require docker on the host and a pre-built
`logosctl:smoke-<flavor>` image (see `build_smoke_image.sh`). If either
is missing the suite skips cleanly rather than failing.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from typing import Iterator

import pytest

from logosctl import (
    LogosctlDockerDaemon,
    docker_available,
    image_present,
)

from ..._fullapi_module_cases import FULLAPI_METHOD_CASES, FULLAPI_EVENT_CASES


# Docker image tag convention: logosctl:smoke-<flavor>, where <flavor>
# is `portable`. Override for one-off images via the env var.
DOCKER_IMAGE_FMT = os.environ.get(
    "LOGOSCTL_DOCKER_IMAGE_FMT", "logosctl:smoke-{flavor}")
MODULE = "test_fullapi_cpp"


def _docker_image_for(flavor: str) -> str:
    return DOCKER_IMAGE_FMT.format(flavor=flavor)


# `docker_flavor` parametrisation and `linux_test_modules_dir` live in
# `conftest.py` so every file in this directory shares them.


# ── Environmental skip helpers ────────────────────────────────────────────

def _require_docker_and_image(flavor: str) -> None:
    if not docker_available():
        pytest.skip("docker not available")
    image = _docker_image_for(flavor)
    if not image_present(image):
        pytest.skip(
            f"docker image '{image}' not built — run "
            "tests/logosctl/docker_smoke/build_smoke_image.sh first"
        )


# ── Legacy single-container fixture ───────────────────────────────────────

@pytest.fixture(scope="module")
def dockerized_daemon(docker_flavor, linux_test_modules_dir) -> Iterator[LogosctlDockerDaemon]:
    _require_docker_and_image(docker_flavor)
    try:
        with LogosctlDockerDaemon(
            image=_docker_image_for(docker_flavor),
            modules_dir=linux_test_modules_dir,
        ) as daemon:
            yield daemon
    except Exception as e:
        pytest.fail(f"daemon ({docker_flavor}) failed to start: {e}")


def test_docker_tcp_status(dockerized_daemon, logosctl_bin):
    client = dockerized_daemon.client(binary=logosctl_bin)
    status = client.status()
    assert isinstance(status, dict)
    assert "daemon" in status or "modules" in status


def test_docker_tcp_load_and_call(dockerized_daemon, logosctl_bin):
    client = dockerized_daemon.client(binary=logosctl_bin)
    modules = client.list_modules()
    names = {m.get("name") for m in modules if isinstance(m, dict)}
    assert any("test_fullapi" in (n or "") for n in names), names
    client.load_module(MODULE)
    assert client.call(MODULE, "echoString", "hello") is not None


# ── Full-API matrix: every type as param/return/event, over both codecs ──

@pytest.fixture(scope="module", params=["json", "cbor"])
def docker_matrix_client(request, logosctl_bin, docker_flavor, linux_test_modules_dir):
    """Yield a LogosctlClient connected over TCP to a fresh docker daemon
    running with the requested wire codec + flavor, with `test_fullapi_cpp`
    loaded. Module-scoped so the matrix below reuses one container per
    codec rather than spinning one up per test."""
    _require_docker_and_image(docker_flavor)
    codec = request.param

    with LogosctlDockerDaemon(
        image=_docker_image_for(docker_flavor),
        modules_dir=linux_test_modules_dir,
        codec=codec,
    ) as daemon:
        client = daemon.client(binary=logosctl_bin)
        client.load_module(MODULE)
        yield client


@pytest.mark.parametrize(
    "method,args,expected", FULLAPI_METHOD_CASES,
    ids=[f"{m}{args!r}" for m, args, _ in FULLAPI_METHOD_CASES],
)
def test_docker_fullapi_method(docker_matrix_client, method, args, expected):
    """Every full-api echo method round-trips through the docker-hosted
    daemon, on both JSON and CBOR (the fixture's codec parameter)."""
    got = docker_matrix_client.call(MODULE, method, *args)
    if expected is None:
        return
    assert got == expected, f"{method}{args!r} -> {got!r}, expected {expected!r}"


@pytest.mark.parametrize(
    "event,fire_method,value", FULLAPI_EVENT_CASES,
    ids=[c[0] for c in FULLAPI_EVENT_CASES],
)
def test_docker_fullapi_event(docker_matrix_client, event, fire_method, value):
    """Every typed event round-trips through the docker daemon on both
    codecs. Re-fires the (idempotent) trigger until the event arrives or a
    deadline, so a slow-to-subscribe watcher can't miss the only emission."""
    received: list[dict] = []
    got = threading.Event()

    def on_event(e: dict) -> None:
        received.append(e)
        got.set()

    with docker_matrix_client.on_event(MODULE, event, on_event):
        deadline = time.monotonic() + 20.0
        while True:
            assert docker_matrix_client.call(MODULE, fire_method, value) is True
            if got.wait(timeout=1.0):
                break
            assert time.monotonic() < deadline, f"{event} not received in time"

    assert received[0]["event"] == event
    payload = received[0]["data"]["arg0"]
    assert payload == value, f"{event} payload {payload!r} != {value!r}"


# ── Two-daemon test: one client process, two independent containers ──────

@pytest.fixture(scope="module")
def two_dockerized_daemons(docker_flavor, linux_test_modules_dir):
    """Spin up two independent daemon containers on different host ports."""
    _require_docker_and_image(docker_flavor)

    daemons: list[LogosctlDockerDaemon] = []
    try:
        for label in ("alpha", "beta"):
            d = LogosctlDockerDaemon(
                image=_docker_image_for(docker_flavor),
                modules_dir=linux_test_modules_dir,
                container_name=f"logosctl-twopair-{docker_flavor}-{label}",
            )
            d.start()
            daemons.append(d)
        yield daemons
    finally:
        for d in daemons:
            d.stop()


def test_two_daemons_in_docker(two_dockerized_daemons, logosctl_bin):
    """Talk to two containerised daemons from one test process: each client
    is bound to its own config dir and forwarded ports. Confirm no state
    leaks between them by (a) reading distinct instance_ids from each
    daemon, (b) loading the module on daemon A but NOT on B, and (c)
    verifying only A reports it loaded while B remains untouched."""
    daemons = two_dockerized_daemons
    assert len(daemons) == 2

    clients = [d.client(binary=logosctl_bin) for d in daemons]
    # Read state.json out of each container via `docker exec cat`
    # rather than off the host bind-mount: the daemon writes it as root
    # with restrictive perms.
    instance_ids = [d.instance_id for d in daemons]
    assert all(instance_ids), (
        f"daemon didn't expose an instance_id (state.json unreadable?): "
        f"{instance_ids}"
    )

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

    assert clients[0].call(MODULE, "echoString", "two-daemon") == "two-daemon"


# ── Shared docker network: two daemons on a caller-managed network ────────

@pytest.fixture(scope="module")
def shared_docker_network(docker_flavor) -> Iterator[str]:
    """Create a docker network for the duration of the module, then remove
    it. Network is owned by the fixture, NOT by LogosctlDockerDaemon —
    daemon just attaches via the `network=` kwarg."""
    _require_docker_and_image(docker_flavor)

    name = f"logosctl-test-{docker_flavor}-{uuid.uuid4().hex[:8]}"
    r = subprocess.run(
        ["docker", "network", "create", name],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.fail(
            f"failed to create docker network {name!r}: {r.stderr.strip()}"
        )
    try:
        yield name
    finally:
        # Non-raising on teardown: cleanup must not mask test failures.
        subprocess.run(["docker", "network", "rm", name], capture_output=True)


@pytest.fixture(scope="module")
def two_dockerized_daemons_in_shared_network(
    docker_flavor, linux_test_modules_dir, shared_docker_network,
) -> Iterator[list[LogosctlDockerDaemon]]:
    """Spin up two daemon containers attached to the same docker network."""
    _require_docker_and_image(docker_flavor)
    daemons: list[LogosctlDockerDaemon] = []
    try:
        for label in ("alpha", "beta"):
            d = LogosctlDockerDaemon(
                image=_docker_image_for(docker_flavor),
                modules_dir=linux_test_modules_dir,
                container_name=f"logosctl-net-{docker_flavor}-{label}",
                network=shared_docker_network,
            )
            d.start()
            daemons.append(d)
        yield daemons
    finally:
        for d in daemons:
            d.stop()


def test_two_daemons_share_docker_network(
    two_dockerized_daemons_in_shared_network, shared_docker_network,
):
    """Both daemons appear as members of the shared docker network."""
    a, b = two_dockerized_daemons_in_shared_network
    r = subprocess.run(
        [
            "docker", "network", "inspect", shared_docker_network,
            "--format", "{{json .Containers}}",
        ],
        capture_output=True, text=True, check=True,
    )
    containers = json.loads(r.stdout)
    member_names = {c["Name"] for c in containers.values()}
    assert a.container_name in member_names, (
        f"daemon alpha {a.container_name!r} not in network "
        f"{shared_docker_network!r}; members={member_names!r}"
    )
    assert b.container_name in member_names, (
        f"daemon beta {b.container_name!r} not in network "
        f"{shared_docker_network!r}; members={member_names!r}"
    )


def test_daemons_can_resolve_peer_by_container_name(
    two_dockerized_daemons_in_shared_network,
):
    """Daemon A can resolve daemon B's container name via docker DNS."""
    a, b = two_dockerized_daemons_in_shared_network
    r = subprocess.run(
        ["docker", "exec", a.container_name,
         "getent", "hosts", b.container_name],
        capture_output=True, text=True, check=True,
    )
    assert b.container_name in r.stdout


def test_nonexistent_network_raises(docker_flavor, linux_test_modules_dir):
    """Pre-flight validation: missing network surfaces as LogosctlError
    with a readable message, not the raw docker stderr."""
    _require_docker_and_image(docker_flavor)
    from logosctl import LogosctlError

    fake = f"logosctl-nonexistent-{uuid.uuid4().hex[:8]}"
    d = LogosctlDockerDaemon(
        image=_docker_image_for(docker_flavor),
        modules_dir=linux_test_modules_dir,
        network=fake,
    )
    with pytest.raises(LogosctlError, match="does not exist"):
        d.start()
    # stop() is safe on a never-started daemon.
    d.stop()


# ── Env vars: set on the daemon process ───────────────────────────────────

def test_env_reaches_daemon_process(docker_flavor, linux_test_modules_dir):
    """`env=` entries are in the environment of the daemon (PID 1)."""
    _require_docker_and_image(docker_flavor)
    with LogosctlDockerDaemon(
        image=_docker_image_for(docker_flavor),
        modules_dir=linux_test_modules_dir,
        env={"LOGOSCTL_TEST_ENV": "from-host"},
    ) as daemon:
        r = subprocess.run(
            ["docker", "exec", daemon.container_name, "cat", "/proc/1/environ"],
            capture_output=True, text=True, check=True,
        )
    assert "LOGOSCTL_TEST_ENV=from-host" in r.stdout.split("\0")
