"""Unit tests for how a logosctl session gets configured.

DELIBERATE DUPLICATE of tests/unit/test_client_config.py in name only — do
not refactor the two together. logoscore configures a daemon with flags
(`-m`, `--persistence-path`). logosctl has none of those: every one of them
is a key in a YAML document installed with `logosctl daemon config set` /
written to `client/config.yaml` before the daemon boots. So this file tests
a different mechanism against the same guarantees, and the two suites can
only be merged by parametrizing over exactly the difference that matters.
Retiring logoscore should be a delete of src/logoscore/ + tests/unit/, not
an unpick.

Nothing here needs a real `logosctl` binary or docker: the CLI is faked
out, and what is asserted is the document that would reach it and the argv
that would install it.
"""
from __future__ import annotations

import json
import os
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
    runtime_control_config,
)
from logosctl.errors import ModuleError

LOCAL = {"core_service": DaemonEndpoint(), "capability_module": DaemonEndpoint()}


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
# `client/config.yaml` is the only way to say which local daemon a client
# talks to — `RpcClient::connect()` reads it verbatim, with no merge layer
# and no environment override. These tests pin the bytes.


def test_write_config_local_minimal(tmp_path: Path):
    LogosctlClient.write_config(tmp_path, LOCAL)

    expected = {
        "version": 2,
        "token_file": "auto.json",
        "daemon": {
            "core_service": {"transport": "local"},
            "capability_module": {"transport": "local"},
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
    LogosctlClient.write_config(tmp_path, LOCAL)
    assert _client_cfg(tmp_path)["token_file"] == "auto.json"


def test_write_config_writes_token_file(tmp_path: Path):
    LogosctlClient.write_config(tmp_path, LOCAL, token="raw-secret")
    auto = tmp_path / "client" / "auto.json"
    assert json.loads(auto.read_text()) == {"token": "raw-secret"}
    assert _client_cfg(tmp_path)["token_file"] == "auto.json"


def test_write_config_instance_id_present_when_set_even_if_empty(tmp_path: Path):
    LogosctlClient.write_config(tmp_path, LOCAL, instance_id="")
    assert _client_cfg(tmp_path)["instance_id"] == ""


def test_write_config_instance_id_absent_when_none(tmp_path: Path):
    LogosctlClient.write_config(tmp_path, LOCAL)
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
        tmp_path, {"core_service": DaemonEndpoint()}, merge=True)

    cfg = _client_cfg(tmp_path)
    assert cfg["instance_id"] == "iid"       # untouched pre-existing key
    assert cfg["version"] == 2
    assert cfg["daemon"] == {"core_service": {"transport": "local"}}


def test_write_config_merge_onto_daemon_written_yaml_rebuilds(tmp_path: Path):
    # A config.yaml the DAEMON wrote is canonical block YAML, and this
    # package has no YAML reader — so merging onto one starts clean rather
    # than failing. Worth pinning: it means a caller that wants a key
    # preserved across a daemon reboot has to re-supply it.
    client_dir = tmp_path / "client"
    client_dir.mkdir(parents=True)
    (client_dir / "config.yaml").write_text(
        "version: 2\ntoken_file: auto.json\ninstance_id: iid\ndaemon:\n"
        "  core_service:\n    transport: local\n"
    )

    LogosctlClient.write_config(tmp_path, LOCAL, merge=True)

    assert set(_client_cfg(tmp_path)) == {"version", "token_file", "daemon"}


def test_write_config_token_honors_existing_token_file(tmp_path: Path):
    # A merged config with a custom token_file must get the token written
    # to THAT file, not a hardcoded auto.json — else config + token diverge.
    client_dir = tmp_path / "client"
    client_dir.mkdir(parents=True)
    (client_dir / "config.yaml").write_text(_dumped({
        "version": 2, "token_file": "custom.json",
        "daemon": {"core_service": {"transport": "local"}},
    }))
    LogosctlClient.write_config(tmp_path, LOCAL, token="tok", merge=True)

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
    LogosctlClient.write_config(tmp_path, LOCAL, token="tok", merge=True)

    cfg = _client_cfg(tmp_path)
    # The CLI rejects a token_file with a separator and fails closed, so a
    # name we can't honor would leave an unreadable credential.
    assert cfg["token_file"] == "auto.json"
    assert json.loads((client_dir / "auto.json").read_text()) == {"token": "tok"}


# ── The daemon config document ───────────────────────────────────────────────
#
# Everything logoscore passes as daemon flags is a key in this document,
# and `daemon start` acts on whatever is already on disk. A key that comes
# out wrong doesn't fail the run — it boots a daemon quietly missing the
# caller's modules dirs — so the emitted bytes are pinned.


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


def test_daemon_document_is_pinned(monkeypatch, tmp_path: Path):
    doc, _ = _submitted_document(
        monkeypatch, tmp_path,
        modules_dir="/abs/mods", persistence_path="/abs/pers")
    assert doc == (
        "modules_dirs:\n"
        "  - /abs/mods\n"
        "persistence_path: /abs/pers\n"
    )


def test_daemon_document_has_no_listeners(monkeypatch, tmp_path: Path):
    # A `local` listener is prepended to every module unconditionally, the
    # only one a client dials, so the document needs no `modules` key.
    doc, _ = _submitted_document(monkeypatch, tmp_path, modules_dir="/abs/mods")
    assert _top_level_keys(doc) == ["modules_dirs"]


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


def test_daemon_document_carries_a_runtime_control_section(monkeypatch, tmp_path):
    # yaml-cpp types scalars aggressively: bare `1.10` decodes as 1.1 and
    # `no` as false. A host or a policy string that came back retyped would
    # fail at bind time with no hint of why.
    doc, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        extra_config={"peering": runtime_control_config("node", port=7443),
                      "access_group": "no"},
    )
    assert (
        "peering:\n"
        "  name: node\n"
        "  control:\n"
        "    enabled: true\n"
        '    host: "127.0.0.1"\n'
        "    port: 7443\n"
        "  runtime_control: true\n"
    ) in doc
    assert 'access_group: "no"' in doc


def test_daemon_document_escapes_embedded_json(monkeypatch, tmp_path: Path):
    # `access_policy` is a JSON *string*, so its quotes and newlines have
    # to survive the YAML round-trip — a raw newline would fold to a space.
    policy = '{\n  "default": "deny"\n}'
    doc, _ = _submitted_document(
        monkeypatch, tmp_path, modules_dir="/abs/mods",
        extra_config={"access_policy": policy})
    assert (r'access_policy: "{\n  \"default\": \"deny\"\n}"') in doc


@pytest.mark.parametrize("bad_extra,needle", [
    ({"modules_dirs": "/single/path"}, "modules_dirs"),
    ({"modules_dirs": ["/ok", 7]}, "list of str"),
    ({"persistence_path": 42}, "persistence_path"),
    ({"access_policy": {"default": "deny"}}, "json.dumps"),
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

    daemon = LogosctlDaemon("/abs/mods", config_dir=tmp_path)
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
    ).start()

    argv = " ".join(rec.cmds[0] + spawned[0])
    for flag in (
        "-m", "--modules-dir", "--persistence-path", "--access-policy",
        "--access-group", "--persist-config", "--client-",
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
    client = LogosctlClient.connect(LOCAL, token="tok", instance_id="iid")
    client.status()

    env = rec.calls[0]["env"]
    assert env["LOGOSCTL_CONFIG_DIR"] == str(client.config_dir)
    # There is no env fallback to leak into: `RpcClient::connect()` reads
    # client/config.yaml verbatim. The dial spec had better be on disk.
    assert _client_cfg(client.config_dir)["instance_id"] == "iid"
    assert (client.config_dir / "client" / "auto.json").exists()


def test_connect_temp_dir_cleaned_up_by_finalizer(
    monkeypatch: pytest.MonkeyPatch,
):
    _Recorder(monkeypatch)
    client = LogosctlClient.connect(LOCAL)
    cfg_dir = client.config_dir
    assert cfg_dir.exists()
    assert client._config_dir_finalizer.alive

    client._config_dir_finalizer()  # simulate GC
    assert not cfg_dir.exists()


def test_connect_explicit_config_dir_is_not_owned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _Recorder(monkeypatch)
    client = LogosctlClient.connect(LOCAL, config_dir=tmp_path)
    assert client.config_dir == tmp_path
    # Caller-supplied dir → no finalizer registered, dir survives.
    assert not hasattr(client, "_config_dir_finalizer")
    assert (tmp_path / "client" / "config.yaml").exists()


# ── LogosctlDaemon: its client, and clients in other config dirs ─────────────


def _resolved_local() -> dict:
    """`resolved.modules` as the daemon writes it post-bind."""
    entry = {"transports": [{"protocol": "local"}]}
    return {"core_service": entry, "capability_module": entry}


def _seed_session(
    config_dir: Path, modules: dict | None = None, *, instance_id: str = "iid",
    token: str = "t",
) -> None:
    daemon_dir = config_dir / "daemon"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    (daemon_dir / "state.json").write_text(json.dumps({
        "version": 2, "instance_id": instance_id, "pid": 4242,
        "config_source": "config.yaml",
        "resolved": {"modules": _resolved_local() if modules is None else modules},
    }))
    client_dir = config_dir / "client"
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "auto.json").write_text(_dumped({"token": token}))


def _started_daemon(tmp_path: Path) -> LogosctlDaemon:
    daemon = LogosctlDaemon("/abs/mods", config_dir=tmp_path)
    daemon._process = object()  # bypass the "daemon not running" guard
    return daemon


def test_daemon_client_carries_only_config_dir_and_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    rec = _Recorder(monkeypatch)
    _seed_session(tmp_path)
    _started_daemon(tmp_path).client().status()

    env = rec.calls[0]["env"]
    assert env["LOGOSCTL_CONFIG_DIR"] == str(tmp_path)
    assert env["LOGOSCTL_TOKEN"] == "t"
    assert {k for k, v in env.items() if os.environ.get(k) != v} == {
        "LOGOSCTL_CONFIG_DIR", "LOGOSCTL_TOKEN"}


def test_daemon_client_leaves_the_daemons_file_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    # The daemon writes a working client/config.yaml into its own session
    # at every boot; rewriting it would replace a file the daemon is
    # entitled to own.
    _Recorder(monkeypatch)
    _seed_session(tmp_path)
    _started_daemon(tmp_path).client()
    assert not (tmp_path / "client" / "config.yaml").exists()


def test_daemon_client_before_start_raises(tmp_path: Path):
    daemon = LogosctlDaemon("/abs/mods", config_dir=tmp_path)
    with pytest.raises(LogosctlError):
        daemon.client()


def test_endpoints_are_the_local_socket(tmp_path: Path):
    _seed_session(tmp_path)
    assert _started_daemon(tmp_path).endpoints() == LOCAL


def test_endpoints_without_a_local_listener_is_a_named_error(tmp_path: Path):
    _seed_session(tmp_path, {"core_service": {"transports": []}})
    with pytest.raises(LogosctlError, match="core_service"):
        _started_daemon(tmp_path).endpoints()


def test_remote_client_writes_its_own_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """A client outside the daemon's session needs its own config.yaml plus
    a credential — and it must NOT live in the daemon's config dir, which
    the daemon rewrites at every boot."""
    rec = _Recorder(monkeypatch)
    session = tmp_path / "session"
    session.mkdir()
    elsewhere = tmp_path / "elsewhere"
    _seed_session(session, token="raw")

    client = _started_daemon(session).remote_client(elsewhere)
    client.status()

    cfg = _client_cfg(elsewhere)
    assert cfg["daemon"] == {"core_service": {"transport": "local"},
                             "capability_module": {"transport": "local"}}
    # The local socket is named after the daemon's instance id.
    assert cfg["instance_id"] == "iid"
    assert json.loads(
        (elsewhere / "client" / "auto.json").read_text()) == {"token": "raw"}
    assert rec.calls[0]["env"]["LOGOSCTL_CONFIG_DIR"] == str(elsewhere)
    # And the daemon's own session is untouched.
    assert not (session / "client" / "config.yaml").exists()


def test_remote_client_without_a_token_fails_loudly(tmp_path: Path):
    _seed_session(tmp_path)
    (tmp_path / "client" / "auto.json").unlink()
    with pytest.raises(LogosctlError, match="auto.json"):
        _started_daemon(tmp_path).remote_client()


def test_set_remote_policy_installs_a_file_the_daemon_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    rec = _Recorder(monkeypatch, stdout=json.dumps({"ok": True}))
    _seed_session(tmp_path)
    policy = {"11111111-1111-1111-1111-111111111111/logosctl": {"core_service": ["getStatus"]}}
    _started_daemon(tmp_path).set_remote_policy(policy)

    path = tmp_path / "remote-policy.json"
    assert json.loads(path.read_text()) == policy
    assert rec.cmds[0][-4:] == ["peer", "policy", "set", str(path)]


def test_a_refused_remote_policy_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _Recorder(monkeypatch, stdout=json.dumps({"ok": False}))
    _seed_session(tmp_path)
    with pytest.raises(LogosctlError, match="remote policy"):
        _started_daemon(tmp_path).set_remote_policy({})


# ── LogosctlDockerDaemon: operated over Remote Runtime Control ───────────────


class _FakeRuntimeControl:
    """Stands in for RuntimeControl in start(): records what it was asked."""

    instances: list["_FakeRuntimeControl"] = []
    fail_pairing = ""

    def __init__(self, daemon, *, binary):
        self.daemon, self.binary = daemon, binary
        self.calls: list[tuple] = []
        _FakeRuntimeControl.instances.append(self)

    def pair(self):
        self.calls.append(("pair",))
        if _FakeRuntimeControl.fail_pairing:
            raise LogosctlError(_FakeRuntimeControl.fail_pairing)

    def grant(self, grants=None):
        self.calls.append(("grant", grants))

    def client(self, *, timeout=30.0):
        return LogosctlClient(self.binary, remote="the-daemon", timeout=timeout)

    def close(self):
        self.calls.append(("close",))


@pytest.fixture
def docker_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    mods = tmp_path / "mods"
    mods.mkdir()
    _FakeRuntimeControl.instances = []
    _FakeRuntimeControl.fail_pairing = ""
    monkeypatch.setattr("logosctl.docker_daemon.RuntimeControl", _FakeRuntimeControl)
    monkeypatch.setattr(LogosctlDockerDaemon, "_wait_for_conn_file", lambda self: True)
    daemon = LogosctlDockerDaemon(
        image="img", modules_dir=mods, binary="host-logosctl",
        config_dir=tmp_path / "cfg", persistence_dir=tmp_path / "pers",
        control_port=7443, container_name="c1")
    yield daemon
    daemon._container_id = None
    daemon.stop()


def test_docker_document_turns_runtime_control_on(docker_daemon):
    doc = docker_daemon._daemon_config_document()
    # Paths are the container's; the image's own modules sit beside its binary.
    assert doc["modules_dirs"] == ["/user-modules"]
    assert doc["persistence_path"] == "/persistence"
    assert doc["peering"] == runtime_control_config("node", host="127.0.0.1", port=7443)
    assert set(doc) == {"modules_dirs", "persistence_path", "peering"}


def test_docker_start_runs_on_the_host_network_then_pairs(
    monkeypatch: pytest.MonkeyPatch, docker_daemon,
):
    rec = _Recorder(monkeypatch, stdout="cid\n")
    docker_daemon.start()

    config_set, run = rec.cmds[0], rec.cmds[1]
    assert config_set[-4:] == ["daemon", "config", "set", "/config/daemon.yaml"]
    assert run[:6] == ["docker", "run", "-d", "--name", "c1", "--network"]
    assert run[6] == "host"
    # Nothing to forward: the host's loopback is the container's.
    assert "-p" not in run
    assert run[-3:] == ["img", "daemon", "start"]
    rc = _FakeRuntimeControl.instances[0]
    assert rc.daemon is docker_daemon and rc.binary == "host-logosctl"
    assert rc.calls == [("pair",), ("grant", None)]
    assert docker_daemon.client().remote == "the-daemon"


def test_docker_start_tears_the_container_down_when_pairing_fails(
    monkeypatch: pytest.MonkeyPatch, docker_daemon,
):
    rec = _Recorder(monkeypatch, stdout="cid\n")
    _FakeRuntimeControl.fail_pairing = "pairing refused"
    with pytest.raises(LogosctlError, match="pairing refused"):
        docker_daemon.start()
    assert ["docker", "rm", "-f", "cid"] in rec.cmds
    assert _FakeRuntimeControl.instances[0].calls[-1] == ("close",)


def test_docker_an_unreachable_daemon_points_at_host_networking(
    monkeypatch: pytest.MonkeyPatch, docker_daemon,
):
    # What Docker Desktop without host networking looks like from the host.
    _Recorder(monkeypatch, stdout="cid\n")
    _FakeRuntimeControl.fail_pairing = "PAIRING_FAILED: UNREACHABLE: Connection refused"
    with pytest.raises(LogosctlError, match="host networking"):
        docker_daemon.start()


def test_docker_peer_runs_the_images_logosctl_inside_the_container(
    monkeypatch: pytest.MonkeyPatch, docker_daemon,
):
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        out = (json.dumps(["/opt/logosctl/bin/logosctl"]) if "inspect" in cmd
               else json.dumps({"pending": []}))
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(subprocess, "run", run)
    docker_daemon._container_id = "cid"
    assert docker_daemon.peer("pending") == {"pending": []}
    # The entrypoint, not /proc/1/exe: under emulation that is the emulator.
    assert calls[0][:3] == ["docker", "container", "inspect"]
    assert calls[1] == ["docker", "exec", "cid", "/opt/logosctl/bin/logosctl",
                        "--config-dir", "/config", "--json", "peer", "pending"]
    docker_daemon.peer("pending")
    assert len(calls) == 3  # looked up once


def test_docker_peer_failure_carries_the_envelope_code(
    monkeypatch: pytest.MonkeyPatch, docker_daemon,
):
    _Recorder(monkeypatch, returncode=3, stdout=json.dumps(
        {"status": "error", "code": "PEERING_REFUSED", "message": "no"}))
    docker_daemon._container_id = "cid"
    docker_daemon._container_binary = "/opt/logosctl/bin/logosctl"
    with pytest.raises(ModuleError) as excinfo:
        docker_daemon.peer("accept", "x")
    assert excinfo.value.code == "PEERING_REFUSED"


def test_docker_policy_goes_through_the_config_bind_mount(
    monkeypatch: pytest.MonkeyPatch, docker_daemon,
):
    rec = _Recorder(monkeypatch, stdout=json.dumps({"ok": True}))
    docker_daemon._container_id = "cid"
    docker_daemon._container_binary = "/opt/logosctl/bin/logosctl"
    docker_daemon.set_remote_policy({"k/logosctl": ["*"]})
    host_file = docker_daemon.config_dir / "remote-policy.json"
    assert json.loads(host_file.read_text()) == {"k/logosctl": ["*"]}
    assert rec.cmds[0][-4:] == ["peer", "policy", "set", "/config/remote-policy.json"]


def test_docker_stop_empties_what_the_container_wrote_through_a_container(
    monkeypatch: pytest.MonkeyPatch, docker_daemon, tmp_path: Path,
):
    rec = _Recorder(monkeypatch, stdout="cid\n")
    docker_daemon._owns_config_dir = docker_daemon._owns_persistence_dir = True
    docker_daemon.start()
    docker_daemon.stop()
    cleanup = rec.cmds[-1]
    assert cleanup[:3] == ["docker", "run", "--rm"]
    assert f"{tmp_path / 'cfg'}:/owned/0" in cleanup
    assert f"{tmp_path / 'pers'}:/owned/1" in cleanup
    assert not (tmp_path / "cfg").exists() and not (tmp_path / "pers").exists()


def test_docker_stop_leaves_a_callers_dirs_and_an_unstarted_daemon_alone(
    monkeypatch: pytest.MonkeyPatch, docker_daemon, tmp_path: Path,
):
    rec = _Recorder(monkeypatch, stdout="cid\n")
    docker_daemon.start()   # the fixture's dirs are the caller's
    docker_daemon.stop()
    assert not any(c[:3] == ["docker", "run", "--rm"] and "/owned/0" in " ".join(c)
                   for c in rec.cmds)
    assert (tmp_path / "cfg").exists()

    mods = tmp_path / "mods2"
    mods.mkdir()
    never = LogosctlDockerDaemon(image="img", modules_dir=mods)
    never.stop()
    assert rec.cmds[-1][:3] != ["docker", "run", "--rm"]


def test_docker_client_before_start_raises(docker_daemon):
    with pytest.raises(LogosctlError):
        docker_daemon.client()
