# ─────────────────────────────────────────────────────────────────────────────
# Part of the logosctl half of the suite — a deliberate duplicate of the
# logoscore fixtures, not a shared abstraction. See tests/logosctl/__init__.py.
# ─────────────────────────────────────────────────────────────────────────────
"""Fixtures specific to the logosctl suite.

Integration tests require a real `logosctl` binary and a modules directory.
They are skipped when the required env vars are not set:

    LOGOSCTL_BIN               — absolute path to the logosctl binary
    LOGOSCTL_TEST_MODULES_DIR  — directory with built test module plugins

The Nix flake's `integration` check sets both. Running
`pytest tests/logosctl/unit` needs neither. That skip-by-default is the
point: `pytest` on a plain Python checkout must not start spawning daemons.

Everything that describes the wire rather than the CLI is inherited from
tests/conftest.py — the `--transport` / `--docker-flavor` options and the
`transport`, `tcp_port`, `tcp_ssl_port` and `self_signed_cert` fixtures.
They mean the same thing for both binaries, so they are shared rather than
copied. What is per-suite is only what names a binary or its modules, and
that is all this file holds.

One caveat when reaching for the inherited `self_signed_cert`: it is issued
for CN=localhost with no subjectAltName, so it only gets you a working
handshake with verification off — which is the default here
(`LogosctlDaemon(verify_peer=False)`, `daemon.client(no_verify_peer=True)`).
A test that wants the real verification path has to mint its own cert
carrying `subjectAltName=IP:127.0.0.1,DNS:localhost` and hand the daemon an
`ssl_ca`; with logosctl there is no per-call escape from what the on-disk
dial spec says, since the `LOGOSCORE_CLIENT_*` env family is gone.
"""
from __future__ import annotations

import os
import shutil

import pytest


@pytest.fixture(scope="session")
def logosctl_bin() -> str:
    binary = os.environ.get("LOGOSCTL_BIN") or shutil.which("logosctl")
    if not binary:
        pytest.skip("LOGOSCTL_BIN not set and `logosctl` not on PATH")
    return binary


@pytest.fixture(scope="session")
def logosctl_test_modules_dir() -> str:
    path = os.environ.get("LOGOSCTL_TEST_MODULES_DIR")
    if not path:
        pytest.skip("LOGOSCTL_TEST_MODULES_DIR not set")
    return path


@pytest.fixture(scope="session")
def test_modules_dir(logosctl_test_modules_dir: str) -> str:
    """Shadows the root conftest's `test_modules_dir` for this subtree.

    The root fixture of that name reads LOGOSCORE_TEST_MODULES_DIR. Left
    unshadowed it would still resolve here — and since both suites are
    usually pointed at the same built test modules, a logosctl test that
    asked for it would quietly pass while honouring the wrong env var, and
    then skip on a machine that sets only the LOGOSCTL_* pair. So the name
    is re-bound rather than left to fall through.

    `logosctl_test_modules_dir` above is the explicit spelling; this exists
    so the generic name can't mean the other binary's modules inside a
    logosctl test.
    """
    return logosctl_test_modules_dir
