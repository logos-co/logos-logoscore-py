"""Version 2 method grants and module_config, through test_probe_module_cpp.

The probe (logos-test-modules) reports its configuration, whether that came
before its context, and its caller with the runtime's `scoped` mark. Both
settings reach the daemon as configuration: `access_policy` as a JSON string,
`module_config` as a mapping.
"""
from __future__ import annotations

import json

import pytest

from logosctl import LogosctlDaemon
from logosctl.errors import LogosctlError

PROBE = "test_probe_module_cpp"
CONFIG = {"endpoint": "https://example.test", "retries": 3}
POLICY = {
    "version": 2,
    "mode": "explicit",
    "restrictions": {PROBE: {"allowedCallers": {
        "@op:*": ["ping", "callerIdentity", "configurationText", "configuredBeforeContext"],
    }}},
}


@pytest.fixture
def probe(logosctl_bin, test_modules_dir):
    with LogosctlDaemon(
        modules_dir=test_modules_dir,
        binary=logosctl_bin,
        extra_config={"access_policy": json.dumps(POLICY), "module_config": {PROBE: CONFIG}},
    ) as daemon:
        client = daemon.client()
        client.load_module(PROBE)
        yield client


def test_the_probe_gets_its_configuration_before_it_serves(probe):
    assert json.loads(probe.call(PROBE, "configurationText")) == CONFIG
    assert probe.call(PROBE, "configuredBeforeContext") is True


def test_an_operators_method_list_is_a_scoped_grant(probe):
    assert probe.call(PROBE, "ping") == "pong"
    who = probe.call(PROBE, "callerIdentity")
    assert who["kind"] == "operator"
    assert who["scoped"] is True
    with pytest.raises(LogosctlError) as refused:
        probe.call(PROBE, "secret")
    assert refused.value.detail_code == "not_authorised", refused.value
