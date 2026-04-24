"""Shared hook for all docker-smoke test files.

The `docker_flavor` fixture is injected via `pytest_generate_tests` so
every test that asks for it gets parametrised once per flavor the user
requested (`--docker-flavor=dev|portable|both`, defaults to `portable`
— see tests/conftest.py for the option definition).

Lives in `conftest.py` rather than inside `test_docker_smoke.py` so
that running any subset (e.g. `pytest tests/docker_smoke/test_docker_ssl_smoke.py`)
still gets the hook.
"""
from __future__ import annotations

import pytest


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
