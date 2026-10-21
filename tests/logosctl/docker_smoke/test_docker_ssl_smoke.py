"""Smoke test for the `tcp_ssl` transport via docker.

Deliberate duplicate of `tests/docker_smoke/test_docker_ssl_smoke.py`: that
file drives the `logoscore` CLI, this one drives `logosctl`. See
`tests/logosctl/__init__.py`.

The container runs the daemon with a self-signed cert generated on the
host at fixture time; `logosctl` on the host dials the daemon over TLS
with peer verification off (because self-signed). That's the minimal path
that a real deployment with a proper cert would follow — if this works
end-to-end, TLS handshake + SDK framing + codec + token auth all work
together under the transport.

Opt-in like the rest of `docker_smoke/`: skips cleanly when docker or
openssl is missing.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from logosctl import (
    LogosctlDockerDaemon,
    docker_available,
    image_present,
)

MODULE = "test_fullapi_cpp"


# Same image-tag convention as `test_docker_smoke.py`, so one
# build_smoke_image.sh run covers both files.
DOCKER_IMAGE_FMT = os.environ.get(
    "LOGOSCTL_DOCKER_IMAGE_FMT", "logosctl:smoke-{flavor}")


def _docker_image_for(flavor: str) -> str:
    return DOCKER_IMAGE_FMT.format(flavor=flavor)


# Building test_fullapi_cpp in the linux container happens once per
# pytest session via the `linux_test_modules_dir` fixture in
# conftest.py — see that file for rationale.


def _require_docker_and_image(flavor: str) -> None:
    if not docker_available():
        pytest.skip("docker not available")
    image = _docker_image_for(flavor)
    if not image_present(image):
        pytest.skip(
            f"docker image '{image}' not built — run "
            "tests/logosctl/docker_smoke/build_smoke_image.sh first"
        )


@pytest.fixture
def ssl_daemon(
    docker_flavor: str,
    self_signed_cert: tuple[Path, Path],
    linux_test_modules_dir: Path,
) -> Iterator[LogosctlDockerDaemon]:
    """A logosctl daemon inside docker listening on `tcp_ssl`, using the
    throwaway cert. Function-scoped: each test gets a clean container."""
    _require_docker_and_image(docker_flavor)
    cert, key = self_signed_cert

    with LogosctlDockerDaemon(
        image=_docker_image_for(docker_flavor),
        modules_dir=linux_test_modules_dir,
        transport="tcp_ssl",
        ssl_cert=cert,
        ssl_key=key,
    ) as daemon:
        yield daemon


def test_docker_ssl_status(ssl_daemon, logosctl_bin):
    """Minimal smoke: TLS handshake + `status` round-trip inside docker."""
    client = ssl_daemon.client(binary=logosctl_bin)
    status = client.status()
    assert isinstance(status, dict)
    assert status.get("daemon", {}).get("status") == "running", status


def test_docker_ssl_load_and_call(ssl_daemon, logosctl_bin):
    """Load a module and call a method over tcp_ssl — the full payload
    path (method args → TLS → RPC → return value → TLS → client)."""
    client = ssl_daemon.client(binary=logosctl_bin)
    client.load_module(MODULE)
    assert client.call(MODULE, "echoString", "ssl-hello") == "ssl-hello"
