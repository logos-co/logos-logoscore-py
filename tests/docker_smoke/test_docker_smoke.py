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

All tests are opt-in: they require docker on the host and a pre-built
`logoscore:smoke` image (see `docker/build_smoke_image.sh`). If either is
missing the suite skips cleanly rather than failing.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from logoscore import LogoscoreClient

from .._basic_module_cases import BASIC_MODULE_CASES


# Docker image tag convention: logoscore:smoke-<flavor>, where <flavor>
# is `dev` or `portable`. Override for one-off images via the env var.
DOCKER_IMAGE_FMT = os.environ.get(
    "LOGOSCORE_DOCKER_IMAGE_FMT", "logoscore:smoke-{flavor}")
MODULE = "test_basic_module"


def _docker_image_for(flavor: str) -> str:
    return DOCKER_IMAGE_FMT.format(flavor=flavor)


def _flavors_to_run(config) -> list[str]:
    """Turn `--docker-flavor` into the list of flavors to parametrise on.
    `dev` | `portable` run the matrix once; `both` replays it twice."""
    choice = config.getoption("--docker-flavor")
    if choice == "both":
        return ["dev", "portable"]
    if choice in ("dev", "portable"):
        return [choice]
    raise pytest.UsageError(
        f"--docker-flavor must be dev|portable|both (got: {choice!r})"
    )


def pytest_generate_tests(metafunc):
    """Inject a `docker_flavor` fixture wherever a test requests it so
    each test is invoked once per flavor the user asked for."""
    if "docker_flavor" in metafunc.fixturenames:
        flavors = _flavors_to_run(metafunc.config)
        metafunc.parametrize("docker_flavor", flavors, scope="module",
                             ids=[f"flavor={f}" for f in flavors])


# ── Docker helpers ─────────────────────────────────────────────────────────

def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "info"], capture_output=True, text=True)
    return r.returncode == 0


def _image_present(image: str) -> bool:
    r = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_conn_file(config_dir: Path, timeout: float = 20.0) -> bool:
    """Block until <config_dir>/daemon.json appears (daemon wrote it)
    or timeout elapses. Returns True on success."""
    deadline = time.monotonic() + timeout
    conn_file = config_dir / "daemon.json"
    while time.monotonic() < deadline:
        if conn_file.exists():
            return True
        time.sleep(0.1)
    return False


def _run_daemon_container(
    *,
    name: str,
    host_port: int,
    config_dir: Path,
    codec: str = "json",
    flavor: str = "dev",
) -> str:
    """Spawn a detached docker container running the logoscore daemon with a
    TCP listener on port 6000 inside the container, published on
    host_port on the host. Returns the container id.

    Built for the host's native Linux platform (see build_smoke_image.sh),
    so we skip --platform here — on Apple Silicon this means linux/arm64
    natively, avoiding Rosetta emulation issues with Boost.Asio's socket
    acceptor."""
    # Bind the same port inside and outside the container. The daemon
    # writes its own port into daemon.json, so if host and container
    # ports differ the client would read the container-internal port
    # and connect to the wrong endpoint on localhost.
    r = subprocess.run([
        "docker", "run", "--rm", "-d",
        "--name", name,
        "-p", f"{host_port}:{host_port}",
        "-v", f"{config_dir}:/config",
        _docker_image_for(flavor),
        "daemon",
        "--config-dir", "/config",
        "--transport", "tcp",
        "--tcp-host", "0.0.0.0",
        "--tcp-port", str(host_port),
        "--tcp-codec", codec,
        # Default modules dir in the new unified image layout, works
        # for both dev and portable flavors.
        "-m", "/opt/logoscore/modules",
    ], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"docker run failed: {r.stderr}")
    return r.stdout.strip()


def _remove_container(container_id: str) -> None:
    subprocess.run(["docker", "rm", "-f", container_id],
                   capture_output=True, text=True)


def _require_docker_and_image(flavor: str = "dev") -> None:
    if not _docker_available():
        pytest.skip("docker not available")
    image = _docker_image_for(flavor)
    if not _image_present(image):
        pytest.skip(
            f"docker image '{image}' not built — run "
            f"FLAVOR={flavor} tests/docker_smoke/build_smoke_image.sh first"
        )


# ── Legacy single-container fixture (kept for the old fallback tests) ─────

@pytest.fixture(scope="module")
def dockerized_daemon(tmp_path_factory, docker_flavor) -> Iterator[tuple[str, Path, int]]:
    _require_docker_and_image(docker_flavor)
    host_port  = _pick_free_port()
    config_dir = tmp_path_factory.mktemp(f"logoscore-docker-{docker_flavor}-cfg")
    container  = f"logoscore-smoke-{docker_flavor}-{uuid.uuid4().hex[:8]}"

    cid = _run_daemon_container(
        name=container, host_port=host_port, config_dir=config_dir,
        flavor=docker_flavor,
    )
    if not _wait_for_conn_file(config_dir):
        _remove_container(cid)
        pytest.fail(f"daemon ({docker_flavor}) never wrote daemon.json")
    try:
        yield cid, config_dir, host_port
    finally:
        _remove_container(cid)


def test_docker_tcp_status(dockerized_daemon, logoscore_bin):
    _, config_dir, _ = dockerized_daemon
    client = LogoscoreClient(
        binary=logoscore_bin, config_dir=config_dir,
        transport="tcp", tcp_host="localhost",
    )
    status = client.status()
    assert isinstance(status, dict)
    assert "daemon" in status or "modules" in status


def test_docker_tcp_load_and_call(dockerized_daemon, logoscore_bin):
    _, config_dir, _ = dockerized_daemon
    client = LogoscoreClient(
        binary=logoscore_bin, config_dir=config_dir,
        transport="tcp", tcp_host="localhost",
    )
    modules = client.list_modules()
    names = {m.get("name") for m in modules if isinstance(m, dict)}
    assert any("test_basic" in (n or "") for n in names), names
    client.load_module(MODULE)
    assert client.call(MODULE, "echo", "hello") is not None


# ── Matrix fixture: one daemon per codec, reused across the full test set ─

@pytest.fixture(scope="module", params=["json", "cbor"])
def docker_matrix_client(request, tmp_path_factory, logoscore_bin, docker_flavor):
    """Yield a LogoscoreClient connected over TCP to a fresh docker daemon
    running with the requested wire codec + flavor. Module-scoped so the
    ~40-case matrix below doesn't spin up a container per test."""
    _require_docker_and_image(docker_flavor)
    codec = request.param

    host_port  = _pick_free_port()
    config_dir = tmp_path_factory.mktemp(
        f"logoscore-docker-{docker_flavor}-{codec}-cfg")
    container  = f"logoscore-matrix-{docker_flavor}-{codec}-{uuid.uuid4().hex[:8]}"

    cid = _run_daemon_container(
        name=container, host_port=host_port, config_dir=config_dir,
        codec=codec, flavor=docker_flavor,
    )
    if not _wait_for_conn_file(config_dir):
        _remove_container(cid)
        pytest.fail(f"daemon ({docker_flavor},{codec}) never wrote daemon.json")
    try:
        client = LogoscoreClient(
            binary=logoscore_bin, config_dir=config_dir,
            transport="tcp", tcp_host="localhost",
            codec=codec,
        )
        client.load_module(MODULE)
        yield client
    finally:
        _remove_container(cid)


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
def two_dockerized_daemons(tmp_path_factory, docker_flavor):
    """Spin up two independent daemon containers on different host ports,
    each with its own config dir. Used by `test_two_daemons_in_docker` to
    verify that the wrapper doesn't accidentally share any global state
    between daemon handles."""
    _require_docker_and_image(docker_flavor)

    specs = []
    for label in ("alpha", "beta"):
        host_port  = _pick_free_port()
        config_dir = tmp_path_factory.mktemp(f"logoscore-docker-{docker_flavor}-{label}")
        container  = f"logoscore-twopair-{docker_flavor}-{label}-{uuid.uuid4().hex[:8]}"
        cid = _run_daemon_container(
            name=container, host_port=host_port, config_dir=config_dir,
            flavor=docker_flavor)
        if not _wait_for_conn_file(config_dir):
            for s in specs:
                _remove_container(s["cid"])
            _remove_container(cid)
            pytest.fail(f"daemon '{label}' never wrote daemon.json")
        specs.append({
            "label": label, "cid": cid,
            "host_port": host_port, "config_dir": config_dir,
        })
    try:
        yield specs
    finally:
        for s in specs:
            _remove_container(s["cid"])


def test_two_daemons_in_docker(two_dockerized_daemons, logoscore_bin):
    """Talk to two containerised daemons from one test process: each client
    is bound to its own config_dir and --tcp-host localhost. Confirm the
    wrapper doesn't leak state between them by (a) reading distinct
    instance_ids from each connection file, (b) loading the module on
    daemon A but NOT on B, and (c) verifying only A reports it loaded
    while B remains untouched."""
    specs = two_dockerized_daemons
    assert len(specs) == 2

    clients = []
    instance_ids = []
    for s in specs:
        c = LogoscoreClient(
            binary=logoscore_bin, config_dir=s["config_dir"],
            transport="tcp", tcp_host="localhost",
        )
        clients.append(c)
        cfg = json.loads((s["config_dir"] / "daemon.json").read_text())
        instance_ids.append(cfg["instance_id"])

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
