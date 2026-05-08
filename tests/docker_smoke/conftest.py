"""Shared hook + fixtures for all docker-smoke test files.

The `docker_flavor` fixture is injected via `pytest_generate_tests` so
every test that asks for it gets parametrised once per flavor the user
requested (`--docker-flavor=dev|portable|both`, defaults to `portable`
— see tests/conftest.py for the option definition).

The `linux_test_modules_dir` fixture builds the test modules inside
docker once per pytest session and is shared by every test that needs
to mount user modules into a daemon container. Without this each
test file would either rebuild from scratch or rely on a
host-architecture nix-store path the docker daemon can't always mount.

Lives in `conftest.py` rather than inside any one test file so that
running any subset (e.g. `pytest tests/docker_smoke/test_docker_ssl_smoke.py`)
still gets both the hook and the shared fixture.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest

from logoscore import build_modules_in_docker, docker_available


def _flavors_to_run(config) -> list[str]:
    """Turn `--docker-flavor` into the list of flavors to parametrise on.
    `dev` | `portable` run the matrix once; `both` replays it twice."""
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
def linux_test_modules_dir(tmp_path_factory) -> Path:
    """Build test_basic_module inside docker, once per pytest session,
    shared by every smoke test that needs to mount user modules.

    Why session scope: building takes a noticeable fraction of a
    minute even with a warm nix store; module-scoping per file would
    rebuild for each `test_docker_*` file. This fixture is independent
    of `docker_flavor` because the `.install-portable` output works in
    both the `portable` and `dev` images (portable's binary doesn't
    need /nix/store; dev's image happens to have it but doesn't need
    portable bundles' embedded libs).

    Override the source flake via `LOGOSCORE_TEST_MODULES_FLAKE` if
    you've forked test-modules locally.
    """
    if not docker_available():
        pytest.skip("docker not available")

    machine = platform.machine().lower()
    system = "aarch64-linux" if machine in ("arm64", "aarch64") else "x86_64-linux"

    flake_ref = os.environ.get(
        "LOGOSCORE_TEST_MODULES_FLAKE",
        "github:logos-co/logos-test-modules",
    )
    out = tmp_path_factory.mktemp("docker-test-modules")
    build_modules_in_docker(
        builds=[
            (flake_ref, f"modules.{system}.test_basic_module.install-portable"),
            # Add more (flake, attr) pairs here when the suite grows —
            # they all share the one container/nix-store invocation, so
            # the marginal cost of an extra module is just its compile.
        ],
        output_dir=out,
    )
    return out
