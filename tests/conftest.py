"""Shared pytest fixtures.

Integration tests require a real `logoscore` binary and a modules directory.
They are skipped when the required env vars are not set:

    LOGOSCORE_BIN             — absolute path to the logoscore binary
    LOGOSCORE_TEST_MODULES_DIR — directory with built test module plugins

The Nix flake's `integration` check sets both. Running `pytest tests/unit`
needs neither.
"""
from __future__ import annotations

import os
import shutil

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--docker-flavor",
        action="store",
        default="portable",
        help=(
            "Which logosctl:smoke-<flavor> docker image the docker "
            "smoke tests target: `portable` (default, self-contained "
            "ctl-bundle-dir — matches how released binaries ship) or "
            "`dev` (nix-store-linked, faster to build when the nix "
            "cache is warm but requires /nix/store in the image). "
            "Use `both` to replay the suite against each in turn."
        ),
    )


@pytest.fixture(scope="session")
def logoscore_bin() -> str:
    binary = os.environ.get("LOGOSCORE_BIN") or shutil.which("logoscore")
    if not binary:
        pytest.skip("LOGOSCORE_BIN not set and `logoscore` not on PATH")
    return binary


@pytest.fixture(scope="session")
def test_modules_dir() -> str:
    path = os.environ.get("LOGOSCORE_TEST_MODULES_DIR")
    if not path:
        pytest.skip("LOGOSCORE_TEST_MODULES_DIR not set")
    return path
