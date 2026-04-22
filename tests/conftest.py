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
