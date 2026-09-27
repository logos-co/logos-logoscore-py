"""RuntimeControl's pairing and grants, against a fake logosctl and a fake
daemon side: which pending pairing it accepts, that the invite never
reaches argv, and that a grant keeps the rest of the daemon's policy."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from logosctl import LogosctlError, RuntimeControl, runtime_control_config
from logosctl.daemon import _yaml_document
from logosctl.remote import DEFAULT_GRANTS

# `remote ls` names the client; `remote pair -` reads the invite from stdin
# and waits for the daemon side's accept, as the real one does.
FAKE_LOGOSCTL = """#!{python}
import json, os, pathlib, sys, time
cfg = pathlib.Path(os.environ["LOGOSCTL_CONFIG_DIR"])
with open(cfg / "argv.log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--json", "remote", "ls"]:
    print(json.dumps({{"self": {{"runtime_id": "client-1"}}, "peers": []}}))
elif sys.argv[1:] == ["--json", "remote", "pair", "-"]:
    (cfg / "invite.seen").write_text(sys.stdin.read())
    if os.environ.get("FAKE_PAIR_FAILS"):
        print(json.dumps({{"status": "error", "code": "PAIRING_FAILED",
                          "message": "the daemon refused the pairing"}}))
        sys.exit(4)
    while not (cfg / "accepted").exists():
        time.sleep(0.05)
    print(json.dumps({{"runtime_id": "daemon-1", "alias": "node"}}))
else:
    sys.exit(9)
"""


class FakeDaemon:
    def __init__(self, client_dir: Path) -> None:
        self.client_dir = client_dir
        self.calls: list[tuple] = []
        self.policy: dict = {"other/*": ["exported"]}
        self._polls = 0

    def peer(self, verb: str, *args: str):
        self.calls.append((verb, *args))
        if verb == "invite":
            return {"invite": "INVITE-SECRET"}
        if verb == "pending":
            self._polls += 1
            if self._polls == 1:
                return {"pending": []}
            return {"pending": [
                {"id": "out", "direction": "outgoing", "peer_runtime_id": "client-1"},
                {"id": "theirs", "direction": "incoming", "peer_runtime_id": "someone"},
                {"id": "mine", "direction": "incoming", "peer_runtime_id": "client-1"},
            ]}
        if verb == "accept":
            (self.client_dir / "accepted").touch()
            return {"ok": True}
        if verb == "policy":
            return dict(self.policy)
        raise AssertionError(verb)

    def set_remote_policy(self, policy):
        self.policy = dict(policy)
        return {"ok": True}


@pytest.fixture
def fake_logosctl(tmp_path: Path) -> str:
    path = tmp_path / "logosctl"
    path.write_text(FAKE_LOGOSCTL.format(python=sys.executable))
    path.chmod(0o755)
    return str(path)


@pytest.fixture(autouse=True)
def _no_forward_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOGOSCTL_PY_FORWARD_OUTPUT", raising=False)
    monkeypatch.delenv("FAKE_PAIR_FAILS", raising=False)


def test_pair_accepts_this_clients_incoming_pairing(tmp_path: Path, fake_logosctl: str):
    client_dir = tmp_path / "client"
    daemon = FakeDaemon(client_dir)
    rc = RuntimeControl(daemon, binary=fake_logosctl, config_dir=client_dir, timeout=20)

    assert rc.pair() == {"runtime_id": "daemon-1", "alias": "node"}
    assert (rc.runtime_id, rc.daemon_id) == ("client-1", "daemon-1")
    assert ("invite", "--runtime-control", "--ttl", "600") in daemon.calls
    assert [c for c in daemon.calls if c[0] == "accept"] == [("accept", "mine")]
    # The invite is a secret: stdin, never argv.
    assert (client_dir / "invite.seen").read_text() == "INVITE-SECRET"
    assert "INVITE-SECRET" not in (client_dir / "argv.log").read_text()


def test_a_failed_pairing_says_why(tmp_path: Path, fake_logosctl: str, monkeypatch):
    monkeypatch.setenv("FAKE_PAIR_FAILS", "1")
    client_dir = tmp_path / "client"
    rc = RuntimeControl(FakeDaemon(client_dir), binary=fake_logosctl,
                        config_dir=client_dir, timeout=20)
    with pytest.raises(LogosctlError, match="refused the pairing"):
        rc.pair()
    assert not rc.daemon_id


def test_grant_sets_only_this_clients_entry(tmp_path: Path):
    daemon = FakeDaemon(tmp_path)
    rc = RuntimeControl(daemon, config_dir=tmp_path)
    rc.runtime_id = "client-1"

    rc.grant()
    assert daemon.policy == {"other/*": ["exported"], "client-1/logosctl": DEFAULT_GRANTS}
    rc.grant({"core_service": ["getStatus"]})
    assert daemon.policy == {"other/*": ["exported"],
                             "client-1/logosctl": {"core_service": ["getStatus"]}}


def test_a_client_needs_the_pairing(tmp_path: Path):
    rc = RuntimeControl(FakeDaemon(tmp_path), binary="ctl", config_dir=tmp_path)
    with pytest.raises(LogosctlError, match="pair"):
        rc.client()
    rc.daemon_id = "daemon-1"
    client = rc.client(timeout=5)
    assert (client.remote, client.config_dir, client.binary, client.token) == \
        ("daemon-1", tmp_path, "ctl", None)


def test_close_removes_only_a_dir_it_made(tmp_path: Path):
    with RuntimeControl(FakeDaemon(tmp_path), config_dir=tmp_path / "kept"):
        pass
    assert (tmp_path / "kept").is_dir()
    with RuntimeControl(FakeDaemon(tmp_path)) as rc:
        made = rc.config_dir
        assert made.is_dir()
    assert not made.exists()


def test_the_runtime_control_section_survives_yaml():
    text = _yaml_document({"peering": runtime_control_config("node", port=7443)})
    assert text == ("peering:\n  name: node\n  control:\n    enabled: true\n"
                    '    host: "127.0.0.1"\n    port: 7443\n  runtime_control: true\n')
