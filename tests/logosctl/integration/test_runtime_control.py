"""Remote Runtime Control on this machine: a daemon with `runtime_control` on,
and a logosctl client with a config dir of its own and no daemon, paired with
it through a runtime-control invite (`logosctl.remote.RuntimeControl`).

Pairing grants nothing; the daemon's remote policy decides each core_service
method and each module method, and `"*"` never covers core_service. The same
flow, run from the host against a daemon in a container, is the docker smoke.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time

import pytest

from logosctl import LogosctlDaemon, LogosctlError, RuntimeControl, runtime_control_config
from logosctl.remote import DEFAULT_GRANTS

MODULE = "test_fullapi_cpp"

GRANTS = {"core_service": ["getStatus", "listModules", "loadModule", "callModuleMethod"],
          MODULE: ["echoString"]}


def _has_remote(binary: str) -> bool:
    """False for a logosctl built without libpeering (Windows, for now)."""
    with tempfile.TemporaryDirectory() as config_dir:
        r = subprocess.run([binary, "--json", "remote", "ls"], capture_output=True,
                           text=True, env={**os.environ, "LOGOSCTL_CONFIG_DIR": config_dir})
    return "without Remote Runtime Control" not in r.stdout


@pytest.fixture(scope="module")
def daemon(logosctl_bin, test_modules_dir):
    if not _has_remote(logosctl_bin):
        pytest.skip("this logosctl was built without Remote Runtime Control")
    with LogosctlDaemon(test_modules_dir, binary=logosctl_bin,
                        extra_config={"peering": runtime_control_config("node")}) as d:
        yield d


@pytest.fixture(scope="module")
def rc(daemon, logosctl_bin):
    with RuntimeControl(daemon, binary=logosctl_bin) as rc:
        rc.pair()
        yield rc


def _refused(call) -> LogosctlError:
    with pytest.raises(LogosctlError) as excinfo:
        call()
    assert excinfo.value.code == "NOT_AUTHORISED", excinfo.value
    return excinfo.value


def test_the_invite_enrolls_a_runtime_control_client(daemon, rc):
    mine = [p for p in daemon.peer("ls")["peers"] if p["runtime_id"] == rc.runtime_id]
    assert mine and "runtime-control" in mine[0]["uses"], mine
    assert any(p["runtime_id"] == rc.daemon_id and "runtime-control" in p["granted_uses"]
               for p in rc.peers()), rc.peers()


def test_pairing_alone_grants_nothing(rc):
    rc.grant({})
    _refused(rc.client().status)


def test_granted_methods_run_on_the_daemon(daemon, rc):
    rc.grant(GRANTS)
    remote = rc.client()
    assert remote.status()["daemon"]["status"] == "running"
    assert MODULE in {m.get("name") for m in remote.list_modules()}
    remote.load_module(MODULE)
    assert remote.call(MODULE, "echoString", "across") == "across"
    # It ran there: the daemon's own operator sees the module loaded.
    assert MODULE in {m.get("name") for m in daemon.client().list_modules(loaded=True)}


def test_an_ungranted_method_is_refused(daemon, rc):
    daemon.client().load_module(MODULE)
    rc.grant(GRANTS)
    remote = rc.client()
    _refused(lambda: remote.call(MODULE, "echoInt", 3))
    _refused(lambda: remote.module_info(MODULE))


def test_star_never_covers_core_service(rc):
    rc.grant({"*": "*"})
    _refused(rc.client().status)


def test_the_default_grants_reach_every_user_module_method(daemon, rc):
    daemon.client().load_module(MODULE)
    rc.grant()
    remote = rc.client()
    assert remote.call(MODULE, "echoInt", 3) == 3
    assert remote.module_info(MODULE)["name"] == MODULE
    assert rc.policy_key in daemon.peer("policy")
    assert daemon.peer("policy")[rc.policy_key] == DEFAULT_GRANTS


def test_an_event_reaches_a_remote_watcher(daemon, rc):
    daemon.client().load_module(MODULE)
    rc.grant()
    remote = rc.client()
    seen: list[dict] = []
    got = threading.Event()
    with remote.on_event(MODULE, "stringEvent", lambda e: (seen.append(e), got.set())):
        deadline = time.monotonic() + 20.0
        while not got.is_set():
            assert remote.call(MODULE, "fireStringEvent", "across") is True
            if got.wait(timeout=1.0):
                break
            assert time.monotonic() < deadline, "the event never reached the remote watcher"
    assert seen[0]["event"] == "stringEvent" and seen[0]["data"]["arg0"] == "across"


def test_a_removed_client_is_refused(daemon, logosctl_bin):
    with RuntimeControl(daemon, binary=logosctl_bin) as other:
        other.pair()
        other.grant()
        assert other.client().status()["daemon"]["status"] == "running"
        daemon.peer("remove", other.runtime_id)
        with pytest.raises(LogosctlError):
            other.client().status()
