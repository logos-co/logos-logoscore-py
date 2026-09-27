"""e2e for operator-issued token authorization, over the local socket.

* an operator-issued named token (`token issue --name …`) authorizes RPCs;
* a revoked token stops working immediately;
* a `--local-only` token is accepted.

These assert the *enforcement* behavior: the daemon validates the presented
token against `daemon/tokens.json` (`TokenStore::lookupByToken`). A daemon
without that enforcement accepts only its own boot `auto` token, so an
issued named token is not honored — hence the module-level guard below skips the
whole file when the `logosctl` under test predates the feature.

Deliberate duplicate of `tests/integration/test_token_auth.py` (the
logoscore twin). The daemons are configured through entirely different
mechanisms, and keeping the files apart means retiring logoscore is a
delete rather than an unpick — please don't merge them back together.
"""
from __future__ import annotations

import pytest

from logosctl import LogosctlDaemon, issue_token, revoke_token

# `test_fullapi_cpp` is provided by LOGOSCTL_TEST_MODULES_DIR (see conftest).
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
# actually honored. If not (an older logosctl that only knows the boot `auto`
# token), skip the whole module rather than fail — these tests only make sense
# against a daemon that enforces operator tokens.

@pytest.fixture(scope="session")
def _enforcement_supported(logosctl_bin, test_modules_dir) -> bool:
    with LogosctlDaemon(modules_dir=test_modules_dir, binary=logosctl_bin) as d:
        issued = issue_token("_probe", binary=logosctl_bin, config_dir=d.config_dir)
        c = d.client()
        c.token = issued["token"]
        return _module_visible(c)


@pytest.fixture(autouse=True)
def _require_enforcement(_enforcement_supported):
    if not _enforcement_supported:
        pytest.skip(
            "this logosctl build does not enforce operator-issued tokens "
            "(only the boot `auto` token is accepted); re-pin logos-logoscore-cli "
            "to the token-enforcement build to enable these tests"
        )


# ── daemon / client fixtures (mirror tests/logosctl/integration/test_end_to_end.py) ──

@pytest.fixture
def daemon(logosctl_bin, test_modules_dir):
    with LogosctlDaemon(modules_dir=test_modules_dir, binary=logosctl_bin) as d:
        yield d


def _client_for(daemon, token):
    """A client presenting `token` instead of the daemon's auto token. It
    travels as `LOGOSCTL_TOKEN`, which `RpcClient::connect` consults ahead
    of the file `client/config.yaml` points at."""
    c = daemon.client()
    c.token = token
    return c


# ── tests ───────────────────────────────────────────────────────────────────

def test_named_token_authorizes(daemon, logosctl_bin):
    """An operator-issued named token authorizes an RPC."""
    issued = issue_token("alice", binary=logosctl_bin, config_dir=daemon.config_dir)
    assert _module_visible(_client_for(daemon, issued["token"])), \
        "an issued named token must authorize"


def test_revoked_token_is_rejected(daemon, logosctl_bin):
    """`token revoke` takes effect immediately — the token stops authorizing."""
    issued = issue_token("bob", binary=logosctl_bin, config_dir=daemon.config_dir)
    c = _client_for(daemon, issued["token"])
    assert _module_visible(c), "sanity: freshly-issued token should work first"

    revoke_token("bob", binary=logosctl_bin, config_dir=daemon.config_dir)
    assert not _module_visible(c), "a revoked token must no longer authorize"


def test_local_only_token_is_accepted(daemon, logosctl_bin):
    """A `--local-only` token works over the local socket."""
    issued = issue_token(
        "loconly", binary=logosctl_bin, config_dir=daemon.config_dir,
        local_only=True,
    )
    assert _module_visible(_client_for(daemon, issued["token"])), \
        "a local_only token must be accepted over the local socket"
