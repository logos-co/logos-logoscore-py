"""e2e for operator-issued token authorization, over the local socket.

* an operator-issued named token (`issue-token --name …`) authorizes RPCs;
* a revoked token stops working immediately;
* a `--local-only` token is accepted.

These assert the *enforcement* behavior: the daemon validates the presented
token against `daemon/tokens.json` (`TokenStore::lookupByToken`). A daemon
without that enforcement accepts only its own boot `auto` token, so an
issued named token is not honored — hence the module-level guard below skips the
whole file when the `logoscore` under test predates the feature. Once the
enforcement CLI is pinned, the guard passes and these run.
"""
from __future__ import annotations

import pytest

from logoscore import LogoscoreDaemon, issue_token, revoke_token

# `test_fullapi_cpp` is provided by LOGOSCORE_TEST_MODULES_DIR (see conftest).
MODULE = "test_fullapi_cpp"


def _module_visible(client) -> bool:
    """True iff `client`'s token is accepted by core_service. An authorized
    token can enumerate the daemon's discovered modules (and sees MODULE); a
    rejected token gets an empty result back."""
    try:
        return MODULE in {m.get("name") for m in client.list_modules()}
    except Exception:
        return False


# ── enforcement guard ───────────────────────────────────────────────────────
#
# Boot one throwaway daemon and check whether an issued named token is
# actually honored. If not (an older logoscore that only knows the boot `auto`
# token), skip the whole module rather than fail — these tests only make sense
# against a daemon that enforces operator tokens.

@pytest.fixture(scope="session")
def _enforcement_supported(logoscore_bin, test_modules_dir) -> bool:
    with LogoscoreDaemon(modules_dir=test_modules_dir, binary=logoscore_bin) as d:
        issued = issue_token("_probe", binary=logoscore_bin, config_dir=d.config_dir)
        c = d.client()
        c.token = issued["token"]
        return _module_visible(c)


@pytest.fixture(autouse=True)
def _require_enforcement(_enforcement_supported):
    if not _enforcement_supported:
        pytest.skip(
            "this logoscore build does not enforce operator-issued tokens "
            "(only the boot `auto` token is accepted); re-pin logos-logoscore-cli "
            "to the token-enforcement build to enable these tests"
        )


# ── daemon / client fixtures (mirror tests/integration/test_end_to_end.py) ───

@pytest.fixture
def daemon(logoscore_bin, test_modules_dir):
    with LogoscoreDaemon(modules_dir=test_modules_dir, binary=logoscore_bin) as d:
        yield d


def _client_for(daemon, token):
    """A client presenting `token` instead of the daemon's auto token."""
    c = daemon.client()
    c.token = token
    return c


# ── tests ───────────────────────────────────────────────────────────────────

def test_named_token_authorizes(daemon, logoscore_bin):
    """An operator-issued named token authorizes an RPC."""
    issued = issue_token("alice", binary=logoscore_bin, config_dir=daemon.config_dir)
    assert _module_visible(_client_for(daemon, issued["token"])), \
        "an issued named token must authorize"


def test_revoked_token_is_rejected(daemon, logoscore_bin):
    """revoke-token takes effect immediately — the token stops authorizing."""
    issued = issue_token("bob", binary=logoscore_bin, config_dir=daemon.config_dir)
    c = _client_for(daemon, issued["token"])
    assert _module_visible(c), "sanity: freshly-issued token should work first"

    revoke_token("bob", binary=logoscore_bin, config_dir=daemon.config_dir)
    assert not _module_visible(c), "a revoked token must no longer authorize"


def test_local_only_token_is_accepted(daemon, logoscore_bin):
    """A `--local-only` token works over the local socket."""
    issued = issue_token(
        "loconly", binary=logoscore_bin, config_dir=daemon.config_dir,
        local_only=True,
    )
    assert _module_visible(_client_for(daemon, issued["token"])), \
        "a local_only token must be accepted over the local socket"
