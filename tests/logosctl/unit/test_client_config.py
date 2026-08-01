"""Unit tests for how a logosctl session gets configured.

DELIBERATE DUPLICATE of tests/unit/test_client_config.py in name only — do
not refactor the two together. logoscore configured a daemon with flags
(`-m`, `--persistence-path`, `--module-transport`, `--insecure-tcp`) and
retargeted a client with `LOGOSCORE_CLIENT_*` env vars. logosctl has none
of those: every one of them is a key in a YAML document installed with
`logosctl daemon config set` / written to `client/config.yaml` before the
daemon boots. So this file tests a different mechanism against the same
guarantees, and the two suites can only be merged by parametrizing over
exactly the difference that matters. Retiring logoscore should be a delete
of src/logoscore/ + tests/unit/, not an unpick.

Nothing here needs a real `logosctl` binary or docker: the CLI is faked
out, and what is asserted is the document that would reach it and the argv
that would install it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from logosctl import (
    DaemonEndpoint,
    LogosctlClient,
    LogosctlDaemon,
    LogosctlDockerDaemon,
    LogosctlError,
)


def _dumped(obj: dict) -> str:
    """How `write_config` serializes client/config.yaml and the token file.

    JSON text in a `.yaml` file, deliberately: YAML is a superset of JSON,
    so yaml-cpp reads it back exactly as written — no chance of `1.10`
    decoding as 1.1 or `no` as false the way a hand-rolled block-YAML
    emitter would risk.
    """
    return json.dumps(obj, indent=4) + "\n"


def _client_cfg(config_dir: str | Path) -> dict:
    return json.loads((Path(config_dir) / "client" / "config.yaml").read_text())


class _Recorder:
    """Captures subprocess.run calls; returns an empty-JSON success."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        returncode: int = 0,
        stdout: str = "{}",
        stderr: str = "",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = (returncode, stdout, stderr)
        monkeypatch.setattr(subprocess, "run", self._run)

    def _run(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        returncode, stdout, stderr = self._result

        class _P:
            pass

        p = _P()
        p.returncode, p.stdout, p.stderr = returncode, stdout, stderr
        return p

    @property
    def cmds(self) -> list[list[str]]:
        return [c["cmd"] for c in self.calls]


@pytest.fixture(autouse=True)
def _no_forward_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_proc.run_json` injects `--verbose` when LOGOSCTL_PY_FORWARD_OUTPUT
    # is truthy, which would shift the argv assertions below for anyone
    # who has the debug switch exported.
    monkeypatch.delenv("LOGOSCTL_PY_FORWARD_OUTPUT", raising=False)


# ── The client dial spec: write_config ───────────────────────────────────────
#
# `client/config.yaml` is now the ONLY way to say which daemon a client
# talks to — `RpcClient::connect()` reads it verbatim, with no merge layer
# and no environment override. These tests pin the bytes.


def test_write_config_tcp_minimal(tmp_path: Path):
    endpoints = {
        "core_service": DaemonEndpoint("tcp", "localhost", 8000, "json"),
        "capability_module": DaemonEndpoint("tcp", "localhost", 8001, "json"),
    }
    LogosctlClient.write_config(tmp_path, endpoints)

    expected = {
        "version": 2,
        "token_file": "auto.json",
        "daemon": {
            "core_service": {
                "transport": "tcp", "host": "localhost",
                "port": 8000, "codec": "json",
            },
            "capability_module": {
                "transport": "tcp", "host": "localhost",
                "port": 8001, "codec": "json",
            },
        },
    }
    cfg_path = tmp_path / "client" / "config.yaml"
    assert cfg_path.read_text() == _dumped(expected)
    # The file the CLI reads is config.yaml; a leftover config.json would
    # be silently ignored, which is the worst way to find out about a
    # half-finished rename.
    assert not (tmp_path / "client" / "config.json").exists()
    # No token → no auto.json.
    assert not (tmp_path / "client" / "auto.json").exists()


def test_write_config_token_file_is_present_even_without_a_token(tmp_path: Path):
    # `fileOk` is `!daemon.empty() && !token_file.empty()`, so an omitted
    # token_file makes the whole spec read as "no client config" — even
    # when the caller intends to pass the token via $LOGOSCTL_TOKEN.
    LogosctlClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 1)})
    assert _client_cfg(tmp_path)["token_file"] == "auto.json"


def test_write_config_tcp_ssl_emits_ca_then_verify_peer_last(tmp_path: Path):
    endpoints = {
        "core_service": DaemonEndpoint(
            "tcp_ssl", "remote", 6000, "cbor", verify_peer=True, ca="/ca.pem"),
    }
    LogosctlClient.write_config(tmp_path, endpoints)

    block = _client_cfg(tmp_path)["daemon"]["core_service"]
    assert block == {
        "transport": "tcp_ssl", "host": "remote",
        "port": 6000, "codec": "cbor", "ca": "/ca.pem", "verify_peer": True,
    }
    # verify_peer stays the final key (matches the order the daemon writes
    # into its own session, and what logoscore's spec asserted).
    assert list(block)[-1] == "verify_peer"


def test_write_config_uses_client_side_key_names_only(tmp_path: Path):
    # The asymmetry that bites: a listener in the DAEMON document says
    # `protocol:` / `cert:` / `key:` / `ca_file:`; an entry here says
    # `transport:` / `ca:`. Spelling one in the other's document fails the
    # parse — on the daemon side it takes the whole config down with it.
    LogosctlClient.write_config(
        tmp_path,
        {"core_service": DaemonEndpoint(
            "tcp_ssl", "h", 1, "json", verify_peer=False, ca="/ca.pem")},
    )
    block = _client_cfg(tmp_path)["daemon"]["core_service"]
    assert set(block) & {"protocol", "cert", "key", "ca_file"} == set()


def test_write_config_drops_verify_peer_and_ca_for_plain_tcp(tmp_path: Path):
    # Both only apply to tcp_ssl — stray values on a tcp endpoint must not
    # leak into the config.
    endpoints = {
        "core_service": DaemonEndpoint(
            "tcp", "h", 1, verify_peer=True, ca="/ca.pem"),
    }
    LogosctlClient.write_config(tmp_path, endpoints)
    block = _client_cfg(tmp_path)["daemon"]["core_service"]
    assert "verify_peer" not in block
    assert "ca" not in block


def test_write_config_writes_token_file(tmp_path: Path):
    LogosctlClient.write_config(
        tmp_path,
        {"core_service": DaemonEndpoint("tcp", "h", 1)},
        token="raw-secret",
    )
    auto = tmp_path / "client" / "auto.json"
    assert json.loads(auto.read_text()) == {"token": "raw-secret"}
    assert _client_cfg(tmp_path)["token_file"] == "auto.json"


def test_write_config_instance_id_present_when_set_even_if_empty(tmp_path: Path):
    LogosctlClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 1)},
        instance_id="")
    assert _client_cfg(tmp_path)["instance_id"] == ""


def test_write_config_instance_id_absent_when_none(tmp_path: Path):
    LogosctlClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 1)})
    assert "instance_id" not in _client_cfg(tmp_path)


def test_write_config_merge_preserves_existing_keys(tmp_path: Path):
    client_dir = tmp_path / "client"
    client_dir.mkdir(parents=True)
    (client_dir / "config.yaml").write_text(_dumped({
        "version": 2,
        "token_file": "auto.json",
        "instance_id": "iid",
        "daemon": {"old": {"transport": "local"}},
    }))

    LogosctlClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 9)},
        merge=True)

    cfg = _client_cfg(tmp_path)
    assert cfg["instance_id"] == "iid"       # untouched pre-existing key
    assert cfg["version"] == 2
    assert cfg["daemon"] == {                 # daemon block fully replaced
        "core_service": {"transport": "tcp", "host": "h",
                         "port": 9, "codec": "json"},
    }


def test_write_config_merge_onto_daemon_written_yaml_rebuilds(tmp_path: Path):
    # A config.yaml the DAEMON wrote is canonical block YAML, and this
    # package has no YAML reader — so merging onto one starts clean rather
    # than failing. Worth pinning: it means a caller that wants a key
    # preserved across a daemon reboot has to re-supply it.
    client_dir = tmp_path / "client"
    client_dir.mkdir(parents=True)
    (client_dir / "config.yaml").write_text(
        "version: 2\ntoken_file: auto.json\ndaemon:\n"
        "  core_service:\n    transport: local\n"
    )

    LogosctlClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 9)},
        merge=True)

    cfg = _client_cfg(tmp_path)
    assert set(cfg) == {"version", "token_file", "daemon"}
    assert cfg["daemon"]["core_service"]["port"] == 9


def test_write_config_token_honors_existing_token_file(tmp_path: Path):
    # A merged config with a custom token_file must get the token written
    # to THAT file, not a hardcoded auto.json — else config + token diverge.
    client_dir = tmp_path / "client"
    client_dir.mkdir(parents=True)
    (client_dir / "config.yaml").write_text(_dumped({
        "version": 2, "token_file": "custom.json",
        "daemon": {"core_service": {"transport": "local"}},
    }))
    LogosctlClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 1)},
        token="tok", merge=True)

    cfg = _client_cfg(tmp_path)
    assert cfg["token_file"] == "custom.json"
    assert json.loads((client_dir / "custom.json").read_text()) == {"token": "tok"}
    assert not (client_dir / "auto.json").exists()


@pytest.mark.parametrize("bad", ["../evil.json", "sub/dir.json", "/abs.json"])
def test_write_config_token_file_traversal_falls_back(tmp_path: Path, bad: str):
    client_dir = tmp_path / "client"
    client_dir.mkdir(parents=True)
    (client_dir / "config.yaml").write_text(_dumped({
        "version": 2, "token_file": bad,
        "daemon": {"core_service": {"transport": "local"}},
    }))
    LogosctlClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 1)},
        token="tok", merge=True)

    cfg = _client_cfg(tmp_path)
    # The CLI rejects a token_file with a separator and fails closed, so a
    # name we can't honor would leave an unreadable credential.
    assert cfg["token_file"] == "auto.json"
    assert json.loads((client_dir / "auto.json").read_text()) == {"token": "tok"}


# ── The daemon config document ───────────────────────────────────────────────
#
# Everything logoscore passed as daemon flags is a key in this document,
# and `daemon start` acts on whatever is already on disk. A key that comes
# out wrong doesn't fail the run — it boots a daemon quietly missing the
# caller's modules dirs or listeners — so the emitted bytes are pinned.


def _submitted_document(
    monkeypatch: pytest.MonkeyPatch, config_dir: Path, **kwargs: Any,
) -> tuple[str, _Recorder]:
    """Run just the config-install phase and return what it submitted."""
    rec = _Recorder(monkeypatch)
    daemon = LogosctlDaemon(config_dir=config_dir, **kwargs)
    daemon._install_daemon_config()
    return (config_dir / "daemon.yaml").read_text(), rec


def _top_level_keys(doc: str) -> list[str]:
    return [line.split(":", 1)[0]
            for line in doc.splitlines()
            if line and not line[0].isspace() and not line.startswith("-")]


def test_daemon_document_tcp_is_pinned(monkeypatch, tmp_path: Path):
    doc, _ = _submitted_document(
        monkeypatch, tmp_path,
        modules_dir="/abs/mods", persistence_path="/abs/pers",
        transports=["tcp"], tcp_port=6000, tcp_cap_port=6001,
    )
    assert doc == (
        "modules_dirs:\n"
        "  - /abs/mods\n"
        "persistence_path: /abs/pers\n"
        "modules:\n"
        "  core_service:\n"
        "    - protocol: tcp\n"
        '      host: "127.0.0.1"\n'
        "      port: 6000\n"
        "      codec: json\n"
        "  capability_module:\n"
        "    - protocol: tcp\n"
        '      host: "127.0.0.1"\n'
        "      port: 6001\n"
        "      codec: json\n"
    )


def test_daemon_document_tcp_ssl_is_pinned(monkeypatch, tmp_path: Path):
    doc, _ = _submitted_document(
        monkeypatch, tmp_path,
        modules_dir="/abs/mods",
        transports=["tcp_ssl"], tcp_ssl_port=6443, tcp_ssl_cap_port=6444,
        ssl_cert="/abs/tls/cert.pem", ssl_key="/abs/tls/key.pem",
        ssl_ca="/abs/tls/ca.pem",
    )
    assert doc == (
        "modules_dirs:\n"
        "  - /abs/mods\n"
        "modules:\n"
        "  core_service:\n"
        "    - protocol: tcp_ssl\n"
        '      host: "127.0.0.1"\n'
        "      port: 6443\n"
        "      codec: json\n"
        "      cert: /abs/tls/cert.pem\n"
        "      key: /abs/tls/key.pem\n"
        "      ca_file: /abs/tls/ca.pem\n"
        "  capability_module:\n"
        "    - protocol: tcp_ssl\n"
        '      host: "127.0.0.1"\n'
        "      port: 6444\n"
        "      codec: json\n"
        "      cert: /abs/tls/cert.pem\n"
        "      key: /abs/tls/key.pem\n"
        "      ca_file: /abs/tls/ca.pem\n"
    )


def test_daemon_document_never_uses_client_key_names(monkeypatch, tmp_path: Path):
    doc, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        transports=["tcp_ssl"], ssl_cert="/c.pem", ssl_key="/k.pem",
        ssl_ca="/ca.pem",
    )
    assert "transport:" not in doc   # the daemon spelling is `protocol:`
    assert "ca:" not in doc          # the daemon spelling is `ca_file:`


def test_daemon_document_omits_the_top_level_ssl_block(monkeypatch, tmp_path: Path):
    # `ssl: {cert, key, ca}` is on the allowlist and parsed into
    # DaemonConfig — and then read by nobody. A listener configured through
    # it binds with no certificate and every handshake dies with "no shared
    # cipher", so the cert/key belong on the listener and nowhere else.
    doc, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        transports=["tcp_ssl"], ssl_cert="/c.pem", ssl_key="/k.pem",
    )
    assert "ssl" not in _top_level_keys(doc)


def test_daemon_document_local_only_has_no_modules_block(monkeypatch, tmp_path: Path):
    # A `local` listener is prepended to every module unconditionally, so
    # a local-only daemon needs no `modules` key at all.
    doc, _ = _submitted_document(monkeypatch, tmp_path, modules_dir="/abs/mods")
    assert _top_level_keys(doc) == ["modules_dirs"]


def test_daemon_document_local_transport_is_a_noop(monkeypatch, tmp_path: Path):
    with_local, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        transports=["local", "tcp"], tcp_port=6000, tcp_cap_port=6001)
    without, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        transports=["tcp"], tcp_port=6000, tcp_cap_port=6001)
    assert with_local == without


def test_daemon_document_absolutises_modules_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    # A relative `modules_dirs` entry resolves against the DAEMON's cwd,
    # which is not the caller's — so `-m ./modules` only kept meaning by
    # being absolutised here.
    monkeypatch.chdir(tmp_path)
    doc, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir=["rel/mods", "/abs/mods"])
    entries = [line.strip("- ").strip()
               for line in doc.splitlines() if line.startswith("  - ")]
    assert all(Path(e).is_absolute() for e in entries)
    assert entries[0].endswith("rel/mods") and entries[0] != "rel/mods"


def test_daemon_document_sets_insecure_tcp_for_non_loopback(monkeypatch, tmp_path):
    # The daemon refuses a plaintext tcp listener off loopback unless the
    # exposure is declared intentional, and refuses it BEFORE logging is
    # up — so the diagnostic lands on the parent's stderr, not in the log.
    doc, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        transports=["tcp"], tcp_host="0.0.0.0")
    assert "insecure_tcp: true" in doc


def test_daemon_document_omits_insecure_tcp_on_loopback(monkeypatch, tmp_path):
    doc, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        transports=["tcp"], tcp_host="127.0.0.1")
    assert "insecure_tcp" not in doc


def test_daemon_document_carries_extra_config_last(monkeypatch, tmp_path: Path):
    # `extra_config` is where the half of logoscore's `extra_args` that
    # carried daemon settings went: with the flags gone, an argv escape
    # hatch can't express any of them. Merged last, so it can also override
    # what the wrapper computed.
    doc, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        extra_config={"access_group": "staff",
                      "logging": {"console": False},
                      "persistence_path": "/override"},
    )
    assert "access_group: staff" in doc
    assert "logging:\n  console: false" in doc
    assert "persistence_path: /override" in doc


def test_daemon_document_quotes_scalars_yaml_would_retype(monkeypatch, tmp_path):
    # yaml-cpp types scalars aggressively: bare `1.10` decodes as 1.1 and
    # `no` as false. A host or a policy string that came back retyped would
    # fail at bind time with no hint of why.
    doc, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        transports=["tcp"], tcp_host="127.0.0.1",
        extra_config={"access_group": "no"},
    )
    assert 'host: "127.0.0.1"' in doc
    assert 'access_group: "no"' in doc


def test_daemon_document_escapes_embedded_json(monkeypatch, tmp_path: Path):
    # `access_policy` is a JSON *string*, so its quotes and newlines have
    # to survive the YAML round-trip — a raw newline would fold to a space.
    policy = '{\n  "default": "deny"\n}'
    doc, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        extra_config={"access_policy": policy})
    assert (r'access_policy: "{\n  \"default\": \"deny\"\n}"') in doc


def test_tcp_ssl_without_cert_and_key_fails_before_any_cli_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    rec = _Recorder(monkeypatch)
    daemon = LogosctlDaemon(
        "/abs/mods", config_dir=tmp_path, transports=["tcp_ssl"])
    with pytest.raises(LogosctlError, match="ssl_cert/ssl_key"):
        daemon._install_daemon_config()
    assert rec.cmds == []


@pytest.mark.parametrize("bad_extra,needle", [
    ({"modules_dirs": "/single/path"}, "modules_dirs"),
    ({"modules_dirs": ["/ok", 7]}, "list of str"),
    ({"persistence_path": 42}, "persistence_path"),
    ({"access_policy": {"default": "deny"}}, "json.dumps"),
    ({"insecure_tcp": "true"}, "insecure_tcp"),
])
def test_mistyped_extra_config_is_caught_before_submission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    bad_extra: dict, needle: str,
):
    # nlohmann's `value(key, default)` THROWS on a type mismatch, and
    # neither the loader nor `config set` catches it — the wrong type
    # aborts the daemon process instead of producing an INVALID_CONFIG.
    # So the check has to happen here, where the diagnostic can name the
    # key, and before anything is written.
    rec = _Recorder(monkeypatch)
    daemon = LogosctlDaemon(
        "/abs/mods", config_dir=tmp_path, extra_config=bad_extra)
    with pytest.raises(LogosctlError, match=needle):
        daemon._install_daemon_config()
    assert rec.cmds == []
    assert not (tmp_path / "daemon.yaml").exists()


# ── Installing it: `daemon config set`, then `daemon start` ──────────────────


def test_config_set_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _, rec = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods")
    assert rec.cmds == [[
        "logosctl", "--config-dir", str(tmp_path),
        "daemon", "config", "set", str(tmp_path / "daemon.yaml"),
    ]]


def test_config_set_puts_config_dir_before_the_subcommand(monkeypatch, tmp_path):
    # `--config-dir` is app-level. `daemon` has fallthrough so it would
    # survive trailing here, but the rule is uniform across the wrapper and
    # a client subcommand would swallow it as a positional.
    _, rec = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods")
    cmd = rec.cmds[0]
    assert cmd.index("--config-dir") < cmd.index("daemon")


class _FakeDaemonProcess:
    """Stands in for the Popen'd `logosctl daemon start`."""

    pid = 4242

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


def test_start_installs_the_config_before_booting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """The two-phase contract: configuration is never passed alongside
    another command, so it has to be on disk before `daemon start` runs.
    Reversing the two would boot a daemon with the previous session's
    config (or none) and no error anywhere.

    The run and the spawn therefore go into ONE ordered log: asserting the
    two lists separately would hold just as well with the phases swapped,
    which is precisely the failure this test exists to catch."""
    events: list[tuple[str, list[str]]] = []
    # True/False per spawn: did the document exist on disk at the moment the
    # daemon was launched? This is the invariant itself, not a proxy for it.
    document_on_disk_at_spawn: list[bool] = []

    rec = _Recorder(monkeypatch)
    recorded_run = subprocess.run  # _Recorder's stand-in, now wrapped

    def _run(cmd, **kwargs):
        events.append(("run", list(cmd)))
        return recorded_run(cmd, **kwargs)

    def _popen(cmd, **kwargs):
        events.append(("spawn", list(cmd)))
        document_on_disk_at_spawn.append((tmp_path / "daemon.yaml").exists())
        return _FakeDaemonProcess()

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(LogosctlDaemon, "_wait_for_ready", lambda self: None)

    daemon = LogosctlDaemon(
        "/abs/mods", config_dir=tmp_path, transports=["tcp"])
    daemon.start()

    # `daemon config set` strictly before `daemon start`, in one sequence.
    # And `daemon start` carries nothing but the session — every knob the
    # caller set travelled in the document.
    assert events == [
        ("run", ["logosctl", "--config-dir", str(tmp_path),
                 "daemon", "config", "set", str(tmp_path / "daemon.yaml")]),
        ("spawn", ["logosctl", "--config-dir", str(tmp_path),
                   "daemon", "start"]),
    ]
    assert document_on_disk_at_spawn == [True]
    assert (tmp_path / "daemon.yaml").exists()
    # Cross-check that the wrapper's own view of the run agrees with the
    # ordered log — the two recorders must not disagree about the argv.
    assert rec.cmds == [cmd for kind, cmd in events if kind == "run"]


def test_start_passes_no_deleted_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    # Each of these is a CLI11 parse error now: the daemon never starts,
    # and under `--detach` the message arrives via startup.err rather than
    # the log. Cheaper to assert they're gone.
    spawned: list[list[str]] = []
    rec = _Recorder(monkeypatch)
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda cmd, **kw: (spawned.append(cmd), _FakeDaemonProcess())[1])
    monkeypatch.setattr(LogosctlDaemon, "_wait_for_ready", lambda self: None)

    LogosctlDaemon(
        "/abs/mods", config_dir=tmp_path, persistence_path="/abs/pers",
        transports=["tcp"], tcp_host="0.0.0.0",
    ).start()

    argv = " ".join(rec.cmds[0] + spawned[0])
    for flag in (
        "-m", "--modules-dir", "--persistence-path", "--module-transport",
        "--insecure-tcp", "--access-policy", "--access-group",
        "--persist-config", "--client-",
    ):
        assert f" {flag}" not in f" {argv}"


def test_rejected_config_raises_with_the_cli_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """An unknown top-level key is a hard error naming the key — the CLI's
    `message` is the whole diagnostic, which is why this path doesn't go
    through `run_json` (whose exception carries only the exit code and the
    machine-readable `code`)."""
    rec = _Recorder(
        monkeypatch, returncode=1,
        stdout=json.dumps({
            "status": "error", "code": "INVALID_CONFIG",
            "message": "unknown key 'modulesdir' in daemon config",
        }))
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda cmd, **kw: (spawned.append(cmd), _FakeDaemonProcess())[1])

    daemon = LogosctlDaemon(
        "/abs/mods", config_dir=tmp_path,
        extra_config={"modulesdir": "/oops"})
    with pytest.raises(LogosctlError) as excinfo:
        daemon.start()

    msg = str(excinfo.value)
    assert "unknown key 'modulesdir'" in msg
    # The document is written before the schema is re-validated, so the
    # session is left holding a config the daemon would refuse — say so.
    assert str(daemon.daemon_config_file) in msg
    # And the daemon must not have been started on top of it.
    assert spawned == []
    assert len(rec.cmds) == 1


def test_unknown_extra_config_key_reaches_the_cli(monkeypatch, tmp_path: Path):
    # The corollary of the test above: the wrapper does not filter keys
    # against its own idea of the allowlist. Dropping an unknown key here
    # would turn a named error into a silent no-op.
    doc, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        extra_config={"modulesdir": "/oops"})
    assert "modulesdir: /oops" in doc


# ── connect(): the dial spec is a file, not an environment ───────────────────


def test_connect_sets_config_dir_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
):
    rec = _Recorder(monkeypatch)
    client = LogosctlClient.connect(
        {
            "core_service": DaemonEndpoint("tcp", "remote", 6000),
            "capability_module": DaemonEndpoint("tcp", "remote", 6001),
        },
        token="tok",
    )
    client.status()

    env = rec.calls[0]["env"]
    assert env["LOGOSCTL_CONFIG_DIR"] == str(client.config_dir)
    # There is no env fallback to leak into: `RpcClient::connect()` reads
    # client/config.yaml verbatim. The dial spec had better be on disk.
    assert (client.config_dir / "client" / "config.yaml").exists()
    assert (client.config_dir / "client" / "auto.json").exists()
    assert _client_cfg(client.config_dir)["daemon"]["capability_module"][
        "port"] == 6001


def test_connect_temp_dir_cleaned_up_by_finalizer(
    monkeypatch: pytest.MonkeyPatch,
):
    _Recorder(monkeypatch)
    client = LogosctlClient.connect(
        {"core_service": DaemonEndpoint("tcp", "h", 1)})
    cfg_dir = client.config_dir
    assert cfg_dir.exists()
    assert client._config_dir_finalizer.alive

    client._config_dir_finalizer()  # simulate GC
    assert not cfg_dir.exists()


def test_connect_explicit_config_dir_is_not_owned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _Recorder(monkeypatch)
    client = LogosctlClient.connect(
        {"core_service": DaemonEndpoint("tcp", "h", 1)},
        config_dir=tmp_path,
    )
    assert client.config_dir == tmp_path
    # Caller-supplied dir → no finalizer registered, dir survives.
    assert not hasattr(client, "_config_dir_finalizer")
    assert (tmp_path / "client" / "config.yaml").exists()


# ── LogosctlDaemon.client(): retargeting is a file rewrite ───────────────────
#
# logoscore merged dial overrides into client/config.json in place. Here
# the daemon's own file is canonical block YAML we can't parse, so the
# overrides rebuild it from state.json — which is also the only place the
# per-module ports exist when the caller let the daemon allocate them.


def _resolved_modules(proto: str, core_port: int, cap_port: int) -> dict:
    """`resolved.modules` as the daemon writes it post-bind. `local` is
    always element 0; `cert`/`key` are deliberately stripped."""
    def entry(port: int) -> dict:
        return {"transports": [
            {"protocol": "local"},
            {"protocol": proto, "host": "127.0.0.1",
             "port": port, "codec": "json"},
        ]}

    return {"core_service": entry(core_port),
            "capability_module": entry(cap_port)}


def _seed_session(
    config_dir: Path, modules: dict, *, instance_id: str = "iid",
    token: str = "t",
) -> None:
    daemon_dir = config_dir / "daemon"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    (daemon_dir / "state.json").write_text(json.dumps({
        "version": 2, "instance_id": instance_id, "pid": 4242,
        "config_source": "config.yaml",
        "resolved": {"modules": modules},
    }))
    client_dir = config_dir / "client"
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "auto.json").write_text(_dumped({"token": token}))


def _started_daemon(tmp_path: Path, **kwargs: Any) -> LogosctlDaemon:
    daemon = LogosctlDaemon("/abs/mods", config_dir=tmp_path, **kwargs)
    daemon._process = object()  # bypass the "daemon not running" guard
    return daemon


def test_daemon_client_carries_only_config_dir_and_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    rec = _Recorder(monkeypatch)
    _seed_session(tmp_path, _resolved_modules("tcp", 6000, 6001))
    _started_daemon(tmp_path, transports=["tcp"]).client().status()

    env = rec.calls[0]["env"]
    assert env["LOGOSCTL_CONFIG_DIR"] == str(tmp_path)
    assert env["LOGOSCTL_TOKEN"] == "t"
    assert {k for k, v in env.items() if os.environ.get(k) != v} == {
        "LOGOSCTL_CONFIG_DIR", "LOGOSCTL_TOKEN"}


def test_daemon_client_without_overrides_leaves_the_daemons_file_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    # The daemon writes a working client/config.yaml into its own session
    # at every boot; rewriting it unasked would replace a file the daemon
    # is entitled to own.
    _Recorder(monkeypatch)
    _seed_session(tmp_path, _resolved_modules("tcp", 6000, 6001))
    _started_daemon(tmp_path, transports=["tcp"]).client()
    assert not (tmp_path / "client" / "config.yaml").exists()


def test_daemon_client_overrides_preserve_distinct_ports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _Recorder(monkeypatch)
    _seed_session(tmp_path, _resolved_modules("tcp", 6000, 6001))
    _started_daemon(tmp_path, transports=["tcp"]).client(
        tcp_host="example.com", codec="cbor")

    daemon_block = _client_cfg(tmp_path)["daemon"]
    assert daemon_block["core_service"]["host"] == "example.com"
    assert daemon_block["capability_module"]["host"] == "example.com"
    assert daemon_block["core_service"]["codec"] == "cbor"
    assert daemon_block["capability_module"]["codec"] == "cbor"
    # Each module keeps its OWN port — they come from the daemon's resolved
    # state, and two listeners can never share one.
    assert daemon_block["core_service"]["port"] == 6000
    assert daemon_block["capability_module"]["port"] == 6001


def test_daemon_client_override_carries_the_instance_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    # A `local` dial resolves its socket name through the instance id
    # (`local:logos_<module>_<id>`), and a rebuilt config that dropped it
    # would dial a name nothing is listening on.
    _Recorder(monkeypatch)
    _seed_session(tmp_path, _resolved_modules("tcp", 6000, 6001),
                  instance_id="0123456789ab")
    _started_daemon(tmp_path, transports=["tcp"]).client(tcp_host="h")
    assert _client_cfg(tmp_path)["instance_id"] == "0123456789ab"


def test_daemon_client_local_override_keeps_minimal_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _Recorder(monkeypatch)
    _seed_session(tmp_path, _resolved_modules("tcp", 6000, 6001))
    _started_daemon(tmp_path, transports=["tcp"]).client(transport="local")

    daemon_block = _client_cfg(tmp_path)["daemon"]
    # No host or port on a local entry — they'd be meaningless noise. The
    # codec IS emitted (logoscore's in-place edit left it off; a rebuilt
    # endpoint always carries one, and the CLI defaults it to json anyway).
    for module in ("core_service", "capability_module"):
        assert daemon_block[module] == {"transport": "local", "codec": "json"}


def test_daemon_client_tcp_ssl_emits_ca_and_flips_verify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _Recorder(monkeypatch)
    _seed_session(tmp_path, _resolved_modules("tcp_ssl", 6443, 6444))
    daemon = _started_daemon(
        tmp_path, transports=["tcp_ssl"], ssl_cert="/c.pem", ssl_key="/k.pem",
        ssl_ca="/abs/ca.pem", verify_peer=True)

    daemon.client(tcp_host="127.0.0.1")
    block = _client_cfg(tmp_path)["daemon"]
    for module in ("core_service", "capability_module"):
        assert block[module]["verify_peer"] is True
        # `ca` here, `ca_file` on the daemon side — the same file, two
        # spellings. Dropping it while verify_peer stays true fails the
        # handshake closed.
        assert block[module]["ca"] == "/abs/ca.pem"

    daemon.client(no_verify_peer=True)
    block = _client_cfg(tmp_path)["daemon"]
    for module in ("core_service", "capability_module"):
        assert block[module]["verify_peer"] is False
        assert block[module]["ca"] == "/abs/ca.pem"


def test_daemon_client_rejects_tcp_port_kwarg(tmp_path: Path):
    _seed_session(tmp_path, _resolved_modules("tcp", 6000, 6001))
    daemon = _started_daemon(tmp_path, transports=["tcp"])
    # There is no per-call port override: each module's port comes from the
    # daemon's own state, and one uniform value could only clobber
    # capability_module onto core_service's port.
    with pytest.raises(TypeError):
        daemon.client(tcp_port=6000)


def test_daemon_client_before_start_raises(tmp_path: Path):
    daemon = LogosctlDaemon("/abs/mods", config_dir=tmp_path)
    with pytest.raises(LogosctlError):
        daemon.client()


def test_endpoints_read_ephemeral_ports_back_from_state(tmp_path: Path):
    # With `port: 0` the daemon allocates, and state.json is the only
    # record of what it picked.
    _seed_session(tmp_path, _resolved_modules("tcp", 51823, 51824))
    endpoints = _started_daemon(tmp_path, transports=["tcp"]).endpoints()
    assert endpoints["core_service"].port == 51823
    assert endpoints["capability_module"].port == 51824


def test_endpoints_dial_loopback_for_a_wildcard_bind(tmp_path: Path):
    # Bind hosts are recorded verbatim, and 0.0.0.0 is not a dialable
    # address on every platform.
    modules = _resolved_modules("tcp", 6000, 6001)
    for entry in modules.values():
        entry["transports"][1]["host"] = "0.0.0.0"
    _seed_session(tmp_path, modules)
    endpoints = _started_daemon(tmp_path, transports=["tcp"]).endpoints()
    assert endpoints["core_service"].host == "127.0.0.1"


def test_endpoints_missing_protocol_is_a_named_error(tmp_path: Path):
    _seed_session(tmp_path, _resolved_modules("tcp", 6000, 6001))
    daemon = _started_daemon(tmp_path, transports=["tcp"])
    with pytest.raises(LogosctlError, match="tcp_ssl"):
        daemon.endpoints("tcp_ssl")


def test_remote_client_writes_its_own_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """The shape logoscore expressed with LOGOSCORE_CLIENT_* env vars: a
    client outside the daemon's session. It needs its own config.yaml plus
    a credential — and it must NOT live in the daemon's config dir, which
    the daemon rewrites at every boot."""
    rec = _Recorder(monkeypatch)
    session = tmp_path / "session"
    session.mkdir()
    elsewhere = tmp_path / "elsewhere"
    _seed_session(session, _resolved_modules("tcp", 6000, 6001), token="raw")

    daemon = LogosctlDaemon("/abs/mods", config_dir=session, transports=["tcp"])
    daemon._process = object()
    client = daemon.remote_client(elsewhere)
    client.status()

    cfg = _client_cfg(elsewhere)
    assert cfg["daemon"]["core_service"]["port"] == 6000
    assert cfg["daemon"]["capability_module"]["port"] == 6001
    assert cfg["instance_id"] == "iid"
    # The daemon's boot token, copied in — the documented provisioning move.
    assert json.loads(
        (elsewhere / "client" / "auto.json").read_text()) == {"token": "raw"}
    assert rec.calls[0]["env"]["LOGOSCTL_CONFIG_DIR"] == str(elsewhere)
    # And the daemon's own session is untouched.
    assert not (session / "client" / "config.yaml").exists()


def test_remote_client_without_a_token_fails_loudly(tmp_path: Path):
    _seed_session(tmp_path, _resolved_modules("tcp", 6000, 6001))
    (tmp_path / "client" / "auto.json").unlink()
    daemon = _started_daemon(tmp_path, transports=["tcp"])
    with pytest.raises(LogosctlError, match="auto.json"):
        daemon.remote_client()


# ── LogosctlDockerDaemon.client(): per-module forwarded ports ────────────────
#
# The capability_module rides its OWN forwarded host port. There is no
# uniform port override that could express that (and logoscore's single
# LOGOSCORE_CLIENT_TCP_PORT would have collapsed the two onto one), so
# these pin that client() is config-file-driven end to end.


def test_docker_client_keeps_distinct_capability_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    mods = tmp_path / "mods"
    mods.mkdir()
    rec = _Recorder(monkeypatch)
    daemon = LogosctlDockerDaemon(image="img", modules_dir=mods)
    try:
        # Fake a started container with distinct forwarded ports.
        daemon._container_id = "fake"
        daemon._host_port = 7000
        daemon._host_cap_port = 7001
        client = daemon.client()

        cfg = _client_cfg(daemon._host_client_dir)
        assert cfg["daemon"]["core_service"]["port"] == 7000
        # The whole point: capability_module keeps 7001, NOT clobbered.
        assert cfg["daemon"]["capability_module"]["port"] == 7001

        client.status()
        assert rec.calls[0]["env"]["LOGOSCTL_CONFIG_DIR"] == str(
            daemon._host_client_dir)
    finally:
        daemon._container_id = None
        daemon.stop()


def test_docker_client_tcp_host_baked_into_both_modules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    mods = tmp_path / "mods"
    mods.mkdir()
    _Recorder(monkeypatch)
    daemon = LogosctlDockerDaemon(image="img", modules_dir=mods)
    try:
        daemon._container_id = "fake"
        daemon._host_port = 7000
        daemon._host_cap_port = 7001
        daemon.client(tcp_host="remote.example.com")
        block = _client_cfg(daemon._host_client_dir)["daemon"]
        assert block["core_service"]["host"] == "remote.example.com"
        assert block["capability_module"]["host"] == "remote.example.com"
    finally:
        daemon._container_id = None
        daemon.stop()


def test_docker_client_tcp_ssl_verify_peer_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    mods = tmp_path / "mods"
    mods.mkdir()
    cert = tmp_path / "c.pem"
    cert.write_text("x")
    key = tmp_path / "k.pem"
    key.write_text("y")
    _Recorder(monkeypatch)
    daemon = LogosctlDockerDaemon(
        image="img", modules_dir=mods, transport="tcp_ssl",
        ssl_cert=cert, ssl_key=key, ssl_ca=cert, verify_peer=True)
    try:
        daemon._container_id = "fake"
        daemon._host_port = 7000
        daemon._host_cap_port = 7001

        # Default no_verify_peer=None → skip verification on disk.
        daemon.client()
        cfg = _client_cfg(daemon._host_client_dir)
        assert cfg["daemon"]["core_service"]["verify_peer"] is False

        # no_verify_peer=False → honour the constructor's verify_peer (True).
        daemon.client(no_verify_peer=False)
        block = _client_cfg(daemon._host_client_dir)["daemon"]
        assert block["core_service"]["verify_peer"] is True
        assert block["capability_module"]["verify_peer"] is True
        # The CA is a HOST path — the client reads it, and the client runs
        # on this side of the container boundary, so it is never mounted.
        assert block["core_service"]["ca"] == str(cert)
    finally:
        daemon._container_id = None
        daemon.stop()


def test_docker_client_endpoints_raise_before_start(tmp_path: Path):
    mods = tmp_path / "mods"
    mods.mkdir()
    daemon = LogosctlDockerDaemon(image="img", modules_dir=mods)
    # Ports are assigned in start(); building endpoints before that must
    # fail loudly rather than emit a portless config.
    with pytest.raises(LogosctlError):
        daemon._client_endpoints("localhost", "json", None)


def test_docker_build_host_client_config_raises_when_token_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    mods = tmp_path / "mods"
    mods.mkdir()
    daemon = LogosctlDockerDaemon(
        image="img", modules_dir=mods, startup_timeout=0.1)
    try:
        daemon._container_id = "fake"
        daemon._host_port = 7000
        daemon._host_cap_port = 7001
        # Token never readable → fail fast instead of writing a config that
        # references a missing auto.json.
        monkeypatch.setattr(daemon, "read_container_file", lambda _p: None)
        with pytest.raises(LogosctlError):
            daemon._build_host_client_config()
    finally:
        daemon._container_id = None
        # _host_client_dir is a real tmpdir; clean it up.
        shutil.rmtree(daemon._host_client_dir, ignore_errors=True)
        shutil.rmtree(daemon._config_dir, ignore_errors=True)
        shutil.rmtree(daemon._persistence_dir, ignore_errors=True)
