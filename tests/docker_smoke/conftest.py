"""Shared hook + fixtures for the docker smoke tests.

The `docker_flavor` fixture is injected via `pytest_generate_tests` so
every test that asks for it gets parametrised once per flavor the user
requested (`--docker-flavor=dev|portable|both`, defaults to `portable`
— see tests/conftest.py for the option definition).

The `linux_test_modules_dir` fixture provides the test modules mounted into
every daemon container: `LOGOSCTL_DOCKER_MODULES_DIR` when set (the Linux
dev shell sets it to this flake's `test_fullapi_cpp.install-portable`),
otherwise built inside docker once per session, so the `.so` files are
ABI-matched to the daemon's Linux runtime whatever the host OS.

The host-side client is `logosctl_bin`: LOGOSCTL_BIN, or `logosctl` on
PATH.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path

import pytest

from logosctl import build_modules_in_docker, docker_available


def _flavors_to_run(config) -> list[str]:
    """Turn `--docker-flavor` into the list of flavors to parametrise on.
    `dev` | `portable` run the suite once; `both` replays it twice."""
    choice = config.getoption("--docker-flavor")
    if choice == "both":
        return ["portable", "dev"]
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


@pytest.fixture(scope="session")
def logosctl_bin() -> str:
    binary = os.environ.get("LOGOSCTL_BIN") or shutil.which("logosctl")
    if not binary:
        pytest.skip("LOGOSCTL_BIN not set and `logosctl` not on PATH")
    return binary


def _locked_test_modules_flake() -> str:
    """logos-test-modules at the revision this repo's flake.lock pins."""
    lock = json.loads((Path(__file__).resolve().parents[2] / "flake.lock").read_text())
    locked = lock["nodes"]["logos-test-modules"]["locked"]
    return f"github:{locked['owner']}/{locked['repo']}/{locked['rev']}"


@pytest.fixture(scope="session")
def linux_test_modules_dir(tmp_path_factory) -> Path:
    """`test_fullapi_cpp` as a Linux `.install-portable` modules dir, which
    loads in both image flavors. Session scope: a docker build takes a
    noticeable fraction of a minute even with a warm nix store.

    Override the source flake via `LOGOSCTL_TEST_MODULES_FLAKE` if you've
    forked test-modules.
    """
    prebuilt = os.environ.get("LOGOSCTL_DOCKER_MODULES_DIR")
    if prebuilt:
        return Path(prebuilt)
    if not docker_available():
        pytest.skip("docker not available")

    machine = platform.machine().lower()
    system = "aarch64-linux" if machine in ("arm64", "aarch64") else "x86_64-linux"

    flake_ref = os.environ.get("LOGOSCTL_TEST_MODULES_FLAKE") or _locked_test_modules_flake()
    out = tmp_path_factory.mktemp("docker-test-modules")
    build_modules_in_docker(
        builds=[(flake_ref, f"modules.{system}.test_fullapi_cpp.install-portable")],
        output_dir=out,
    )
    return out
