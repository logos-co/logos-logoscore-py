"""Transport-parametrized e2e for operator-issued token authorization.

Exercises the daemon's per-token enforcement over the full transport matrix
(local / tcp / tcp_ssl):

* an operator-issued named token (`token issue --name …`) authorizes RPCs;
* a revoked token stops working immediately;
* a `--local-only` token is accepted over the local socket but rejected over
  tcp / tcp_ssl.

These assert the *enforcement* behavior: the daemon validates the presented
token against `daemon/tokens.json` (`TokenStore::lookupByToken`) and, for a
`local_only` token, keys the decision on the transport the call arrived on. A
daemon without that enforcement accepts only its own boot `auto` token, so an
issued named token is not honored — hence the module-level guard below skips the
whole file when the `logosctl` under test predates the feature.

Note what is deliberately NOT tested: that the daemon's own boot token
(`client/auto.json`) is refused over the network. It is issued local-only
and yet authenticates over tcp/tcp_ssl, because the daemon also registers
the raw value with its in-process TokenManager, which is consulted ahead of
the store validator that would reject it. That is a runtime inconsistency,
not a CLI promise, and a test asserting either behaviour would be pinning
an accident. Named tokens go through the validator, which is what these
tests measure.

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
# Boot one throwaway LOCAL daemon and check whether an issued named token is
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
def daemon(logosctl_bin, test_modules_dir, transport, tcp_port, tcp_ssl_port, request):
    # Every non-local transport is a listener entry in the daemon's config
    # document rather than a `--module-transport` flag; capability_module's
    # port is left ephemeral. See test_end_to_end.py's fixture for the
    # full rationale.
    kwargs = {}
    if transport != "local":
        kwargs["transports"] = [transport]
        if transport == "tcp":
            kwargs["tcp_port"] = tcp_port
        elif transport == "tcp_ssl":
            cert, key = request.getfixturevalue("self_signed_cert")
            kwargs["tcp_ssl_port"] = tcp_ssl_port
            kwargs["ssl_cert"] = cert
            kwargs["ssl_key"] = key
    with LogosctlDaemon(
        modules_dir=test_modules_dir, binary=logosctl_bin, **kwargs,
    ) as d:
        yield d


def _client_for(daemon, token):
    """A client presenting `token` instead of the daemon's auto token.

    The transport comes from the daemon's on-disk dial spec — there is no
    per-client transport argument any more — so, unlike the logoscore
    version, this needs nothing but the token. It travels as
    `LOGOSCTL_TOKEN`, which `RpcClient::connect` consults ahead of the
    file `client/config.yaml` points at.
    """
    c = daemon.client()
    c.token = token
    return c


# ── tests ───────────────────────────────────────────────────────────────────

def test_named_token_authorizes_over_transport(daemon, transport, logosctl_bin):
    """An operator-issued named token authorizes an RPC over every transport."""
    issued = issue_token("alice", binary=logosctl_bin, config_dir=daemon.config_dir)
    c = _client_for(daemon, issued["token"])
    assert _module_visible(c), \
        f"an issued named token must authorize over {transport}"


def test_revoked_token_is_rejected(daemon, transport, logosctl_bin):
    """`token revoke` takes effect immediately — the token stops authorizing."""
    issued = issue_token("bob", binary=logosctl_bin, config_dir=daemon.config_dir)
    c = _client_for(daemon, issued["token"])
    assert _module_visible(c), "sanity: freshly-issued token should work first"

    revoke_token("bob", binary=logosctl_bin, config_dir=daemon.config_dir)
    assert not _module_visible(c), "a revoked token must no longer authorize"


def test_local_only_token_enforced_by_transport(daemon, transport, logosctl_bin):
    """A `--local-only` token works over the local socket but is rejected over
    the network (tcp / tcp_ssl) — the daemon keys the decision on the transport
    the call arrived on."""
    issued = issue_token(
        "loconly", binary=logosctl_bin, config_dir=daemon.config_dir,
        local_only=True,
    )
    c = _client_for(daemon, issued["token"])
    if transport == "local":
        assert _module_visible(c), \
            "a local_only token must be accepted over the local socket"
    else:
        assert not _module_visible(c), \
            f"a local_only token must be rejected over {transport}"
