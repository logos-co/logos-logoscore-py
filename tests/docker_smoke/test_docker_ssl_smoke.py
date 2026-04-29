"""Smoke test for the `tcp_ssl` transport via docker.

The container runs the daemon with a self-signed cert generated on the
host at fixture time; `logoscore` on the host dials the daemon over TLS
with `--no-verify-peer` (because self-signed). That's the minimal path
that a real deployment with a proper cert would follow — if this works
end-to-end, TLS handshake + SDK framing + codec + token auth all work
together under the transport.

Opt-in like the rest of `docker_smoke/`: skips cleanly when docker or
openssl is missing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import pytest

from logoscore import (
    LogoscoreDockerDaemon,
    docker_available,
    image_present,
)

MODULE = "test_basic_module"


# Same image-tag convention as `test_docker_smoke.py`. Shared so that one
# FLAVOR=... build_smoke_image.sh run covers both suites.
DOCKER_IMAGE_FMT = os.environ.get(
    "LOGOSCORE_DOCKER_IMAGE_FMT", "logoscore:smoke-{flavor}")


def _docker_image_for(flavor: str) -> str:
    return DOCKER_IMAGE_FMT.format(flavor=flavor)


# Building test_basic_module in the linux container happens once per
# pytest session via the `linux_test_modules_dir` fixture in
# conftest.py — see that file for rationale.


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
def self_signed_cert(tmp_path_factory) -> tuple[Path, Path]:
    """Generate a throwaway self-signed cert+key for CN=localhost.

    Module-scoped so every test in this file shares the same cert —
    generation takes a noticeable fraction of a second and there's no
    isolation reason to do it per-test.
    """
    if not shutil.which("openssl"):
        pytest.skip("openssl not on PATH; can't generate self-signed cert")

    d = tmp_path_factory.mktemp("tls")
    cert = d / "cert.pem"
    key = d / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509",
            "-newkey", "rsa:2048",
            "-keyout", str(key),
            "-out", str(cert),
            "-days", "1",
            "-nodes",
            "-subj", "/CN=localhost",
        ],
        check=True, capture_output=True,
    )
    return cert, key


@pytest.fixture
def ssl_daemon(
    docker_flavor: str,
    self_signed_cert: tuple[Path, Path],
    linux_test_modules_dir: Path,
) -> Iterator[LogoscoreDockerDaemon]:
    """A logoscore daemon inside docker listening on `tcp_ssl`, using the
    throwaway cert. Function-scoped: each test gets a clean container."""
    _require_docker_and_image(docker_flavor)
    cert, key = self_signed_cert

    with LogoscoreDockerDaemon(
        image=_docker_image_for(docker_flavor),
        modules_dir=linux_test_modules_dir,
        transport="tcp_ssl",
        ssl_cert=cert,
        ssl_key=key,
    ) as daemon:
        yield daemon


def test_docker_ssl_status(ssl_daemon, logoscore_bin):
    """Minimal smoke: TLS handshake + `status` round-trip inside docker.

    `rpc_error` must be absent — its presence means `getStatus` never
    actually reached core_service and the client built a fallback status
    from daemon.json metadata. Without this check the test gives a
    false positive: `daemon.status == "running"` is synthesised from
    the mere existence of a parseable daemon.json, which would pass
    even if TLS were completely broken.
    """
    client = ssl_daemon.client(binary=logoscore_bin)
    status = client.status()
    assert isinstance(status, dict)
    assert "rpc_error" not in status, (
        "TLS path didn't actually reach core_service — status is "
        f"synthesised from daemon.json: {status}"
    )
    assert status.get("daemon", {}).get("status") == "running", status


def test_docker_ssl_load_and_call(ssl_daemon, logoscore_bin):
    """Load a module and call a method over tcp_ssl — the full payload
    path (method args → TLS → RPC → return value → TLS → client)."""
    client = ssl_daemon.client(binary=logoscore_bin)
    client.load_module(MODULE)
    assert client.call(MODULE, "echo", "ssl-hello") == "ssl-hello"
