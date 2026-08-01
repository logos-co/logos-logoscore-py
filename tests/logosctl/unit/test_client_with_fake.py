"""Unit tests for LogosctlClient with subprocess.run monkeypatched.

These tests verify the wrapper's argv construction, env propagation, JSON
parsing, and exit-code → exception mapping — without needing a real
`logosctl` binary.

DELIBERATE DUPLICATE of tests/unit/test_client_with_fake.py — do not
refactor the two into one suite. They assert different command lines
(`module ls` vs `list-modules`), different env vars, and a different
transport story; sharing them would mean parametrizing over the very
differences that are the point. Retiring logoscore should be a delete of
src/logoscore/ + tests/unit/, not an unpick.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from logosctl import LogosctlClient, issue_token, list_tokens, revoke_token
from logosctl.errors import (
    DaemonNotRunningError,
    MethodError,
    ModuleError,
)


class FakeProc:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Recorder:
    """Monkeypatches subprocess.run to capture calls and inject results."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next: FakeProc | None = None
        monkeypatch.setattr(subprocess, "run", self._run)

    def respond(
        self, *, returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        self._next = FakeProc(returncode=returncode, stdout=stdout, stderr=stderr)

    def _run(self, cmd, **kwargs) -> FakeProc:
        self.calls.append({"cmd": cmd, **kwargs})
        result = self._next or FakeProc()
        self._next = None
        return result


@pytest.fixture(autouse=True)
def _no_forward_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_proc.run_json` injects `--verbose` when LOGOSCTL_PY_FORWARD_OUTPUT
    # is truthy, which shifts every argv index below. A developer debugging
    # a daemon hang has that set in their shell; the argv contract shouldn't
    # appear broken because of it.
    monkeypatch.delenv("LOGOSCTL_PY_FORWARD_OUTPUT", raising=False)


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    return Recorder(monkeypatch)


def test_status_parses_json(rec: Recorder):
    rec.respond(stdout=json.dumps({"daemon": {"status": "running"}}))
    client = LogosctlClient()
    out = client.status()
    assert out == {"daemon": {"status": "running"}}
    assert rec.calls[0]["cmd"] == ["logosctl", "--json", "status"]


def test_config_dir_and_token_propagated(rec: Recorder):
    rec.respond(stdout="{}")
    client = LogosctlClient(config_dir=Path("/tmp/xcfg"), token="tok-123")
    client.status()
    env = rec.calls[0]["env"]
    assert env["LOGOSCTL_CONFIG_DIR"] == "/tmp/xcfg"
    assert env["LOGOSCTL_TOKEN"] == "tok-123"


def test_config_dir_travels_as_env_not_flag(rec: Recorder):
    # `--config-dir` is an app-level option, and client subcommands use
    # allow_extras(): placed after one it reaches the command as two
    # positional arguments. The env var has no position to get wrong, so
    # the wrapper never emits the flag for a client command.
    rec.respond(stdout="{}")
    LogosctlClient(config_dir=Path("/tmp/xcfg")).call("m", "meth")
    assert "--config-dir" not in rec.calls[0]["cmd"]


def test_env_delta_is_exactly_the_two_variables(rec: Recorder):
    # LOGOSCTL_CONFIG_DIR and LOGOSCTL_TOKEN are the only variables the
    # binary reads. The LOGOSCORE_CLIENT_* family that used to retarget a
    # single call has no counterpart — which dial spec a client uses is a
    # document now — so anything else here would be dead weight the reader
    # would have to chase.
    rec.respond(stdout="{}")
    LogosctlClient(config_dir=Path("/tmp/xcfg"), token="t").status()
    env = rec.calls[0]["env"]
    delta = {k for k, v in env.items() if os.environ.get(k) != v}
    assert delta == {"LOGOSCTL_CONFIG_DIR", "LOGOSCTL_TOKEN"}


def test_no_config_dir_leaves_env_untouched(rec: Recorder):
    rec.respond(stdout="{}")
    LogosctlClient().status()
    env = rec.calls[0]["env"]
    assert {k for k, v in env.items() if os.environ.get(k) != v} == set()


@pytest.mark.parametrize("kwarg", [
    "transport", "tcp_host", "tcp_port", "no_verify_peer", "codec",
])
def test_deleted_transport_kwargs_are_rejected(kwarg: str):
    # logoscore turned these into LOGOSCORE_CLIENT_* env vars that the CLI
    # merged over the on-disk spec per call. `RpcClient::connect()` now
    # reads client/config.yaml verbatim with no merge layer, so there is
    # nothing they could set — they raise rather than being silently
    # ignored, which is the failure mode that would cost an afternoon.
    with pytest.raises(TypeError):
        LogosctlClient(**{kwarg: "x"})


def test_list_modules_loaded_flag(rec: Recorder):
    rec.respond(stdout=json.dumps([{"name": "chat"}]))
    client = LogosctlClient()
    out = client.list_modules(loaded=True)
    assert out == [{"name": "chat"}]
    assert rec.calls[0]["cmd"] == [
        "logosctl", "--json", "module", "ls", "--loaded",
    ]


def test_call_unwraps_result(rec: Recorder):
    rec.respond(stdout=json.dumps({"status": "success", "result": 42}))
    client = LogosctlClient()
    assert client.call("m", "meth", "a", 1, True) == 42
    assert rec.calls[0]["cmd"] == [
        "logosctl", "--json", "call", "m", "meth", "a", "1", "true",
    ]


def test_call_path_arg_becomes_at_file(rec: Recorder):
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    client = LogosctlClient()
    client.call("m", "loadConfig", Path("/etc/x.json"))
    assert rec.calls[0]["cmd"] == [
        "logosctl", "--json", "call", "m", "loadConfig", "@/etc/x.json",
    ]


def _b64url(b: bytes) -> str:
    """The canonical unpadded urlsafe base64 the client wraps bytes in."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def test_call_bytes_arg_uses_canonical_tag(rec: Recorder):
    # bytes cross the wire as `json:{"_bytes": "<b64url>"}` — NUL/high-byte
    # safe, unlike a raw latin-1 arg (which UTF-8-mangles bytes >= 0x80).
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    client = LogosctlClient()
    client.call("m", "echoBytes", b"\x01\x02\xff")
    arg = rec.calls[0]["cmd"][5]
    assert arg.startswith("json:")
    assert json.loads(arg[len("json:"):]) == {"_bytes": _b64url(b"\x01\x02\xff")}


def test_call_container_args_use_json_prefix(rec: Recorder):
    # list/dict params go behind the CLI's `json:` prefix as natural JSON.
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    client = LogosctlClient()
    client.call("m", "echoThings", [1, 2, 3], {"k": "v"})
    list_arg, map_arg = rec.calls[0]["cmd"][5], rec.calls[0]["cmd"][6]
    assert list_arg.startswith("json:") and json.loads(list_arg[5:]) == [1, 2, 3]
    assert map_arg.startswith("json:") and json.loads(map_arg[5:]) == {"k": "v"}


def test_call_multi_arg_mixed_types_argv(rec: Recorder):
    """A multi-argument call mixing every `_arg_to_str` branch — locks in
    both argument arity (>2 positional args) and the per-arg wire encoding.
    `test_fullapi_cpp`'s methods are all 0/1-arg, so this unit test is where
    multi-argument argv construction is pinned.

    The encoding is byte-identical to logoscore's on purpose: both binaries
    compile the same `call_command.cpp`, so the argument grammar is one
    contract, not two."""
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    client = LogosctlClient()
    client.call("m", "many", "s", 7, True, b"\x00\xff", [1, "x"], {"k": 1})
    cmd = rec.calls[0]["cmd"]
    assert cmd[:8] == [
        "logosctl", "--json", "call", "m", "many", "s", "7", "true",
    ]
    assert json.loads(cmd[8][5:]) == {"_bytes": _b64url(b"\x00\xff")}
    assert json.loads(cmd[9][5:]) == [1, "x"]
    assert json.loads(cmd[10][5:]) == {"k": 1}


def test_global_flags_precede_the_subcommand(rec: Recorder):
    # A trailing `--json` does parse — main.cpp lifts the global flags back
    # out of a client subcommand's leftovers — but that same extractor is
    # what would eat a method argument that happens to BE `--json`. Emitting
    # up front removes the class of bug, so the placement is a contract.
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    LogosctlClient().call("m", "meth", "arg")
    cmd = rec.calls[0]["cmd"]
    assert cmd[1] == "--json"
    assert "--json" not in cmd[2:]


def test_literal_flag_argument_needs_str_prefix(rec: Recorder):
    # The residual hazard `call()`'s docstring names: the extractor runs
    # over remaining() unconditionally, so a bare `--json` argument is
    # still swallowed. `str:` is the documented escape and must survive
    # the wrapper untouched.
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    LogosctlClient().call("m", "meth", "str:--json")
    assert rec.calls[0]["cmd"] == [
        "logosctl", "--json", "call", "m", "meth", "str:--json",
    ]


def test_call_error_envelope_raises_method_error(rec: Recorder):
    # Envelope says error but exit code is 0 — this path exists for CLIs that
    # don't always map error envelopes to non-zero exit codes.
    rec.respond(
        stdout=json.dumps({"status": "error", "code": "BAD", "message": "no"})
    )
    client = LogosctlClient()
    with pytest.raises(MethodError) as excinfo:
        client.call("m", "meth")
    assert excinfo.value.code == "BAD"


def test_nonzero_exit_code_2_raises_daemon_not_running(rec: Recorder):
    rec.respond(returncode=2, stderr="no daemon")
    client = LogosctlClient()
    with pytest.raises(DaemonNotRunningError):
        client.status()


def test_nonzero_exit_code_3_raises_module_error(rec: Recorder):
    rec.respond(returncode=3, stderr="not found")
    client = LogosctlClient()
    with pytest.raises(ModuleError):
        client.load_module("missing")


def test_nonzero_exit_code_4_raises_method_error(rec: Recorder):
    rec.respond(returncode=4, stderr="timeout")
    client = LogosctlClient()
    with pytest.raises(MethodError):
        client.call("m", "meth")


def test_error_envelope_code_carried_onto_the_exception(rec: Recorder):
    # The envelope is printed on stdout even when the exit code is
    # non-zero, and it is the only machine-readable half of the failure.
    rec.respond(
        returncode=3,
        stdout=json.dumps({"status": "error", "code": "MODULE_NOT_FOUND",
                           "message": "no such module"}),
        stderr="",
    )
    with pytest.raises(ModuleError) as excinfo:
        LogosctlClient().load_module("missing")
    assert excinfo.value.code == "MODULE_NOT_FOUND"


def test_stop_subcommand(rec: Recorder):
    rec.respond(stdout=json.dumps({"status": "ok"}))
    client = LogosctlClient()
    client.stop()
    assert rec.calls[0]["cmd"] == ["logosctl", "--json", "daemon", "stop"]


def test_stats_subcommand(rec: Recorder):
    # `stats` is a top-level alias, but the group is what --help documents.
    rec.respond(stdout="{}")
    LogosctlClient().stats()
    assert rec.calls[0]["cmd"] == ["logosctl", "--json", "module", "stats"]


def test_all_module_management_commands(rec: Recorder):
    client = LogosctlClient()
    for method, args, expected_subcmd in [
        ("load_module", ("chat",), ["module", "load", "chat"]),
        ("unload_module", ("chat",), ["module", "unload", "chat"]),
        ("reload_module", ("chat",), ["module", "reload", "chat"]),
        ("module_info", ("chat",), ["module", "show", "chat"]),
    ]:
        rec.respond(stdout="{}")
        getattr(client, method)(*args)
        assert rec.calls[-1]["cmd"] == ["logosctl", "--json", *expected_subcmd]


def test_load_module_has_no_no_deps_kwarg():
    # `module load` declares only the positional name in this build — a
    # `--no-deps` would hit CLI11's catch and print a usage error. The
    # wrapper doesn't offer the kwarg, so the mistake is caught in Python.
    with pytest.raises(TypeError):
        LogosctlClient().load_module("chat", no_deps=True)


# ── token subcommands ────────────────────────────────────────────────────────
#
# Every one of these was respelled in the port — `issue-token` → `token
# issue`, `revoke-token` → `token revoke`, `list-tokens` → `token ls` — so
# argv is the whole contract. A wrong spelling is a CLI11 usage error, and
# the integration tests that exercise `issue_token`/`revoke_token` need a
# real daemon, so this is the only place the grammar is pinned cheaply.


def test_issue_token_argv(rec: Recorder):
    # The name is a `--name` OPTION on issue (and a positional on revoke,
    # below) — the asymmetry is the CLI's, and passing it the other way
    # round fails the parse.
    rec.respond(stdout=json.dumps(
        {"name": "alice", "token": "raw-secret", "file": "/cfg/alice.json"}))
    out = issue_token("alice")
    assert out["token"] == "raw-secret"
    assert rec.calls[0]["cmd"] == [
        "logosctl", "--json", "token", "issue", "--name", "alice",
    ]


def test_issue_token_optional_flags_argv(rec: Recorder):
    rec.respond(stdout=json.dumps({"name": "bob", "token": "t"}))
    issue_token("bob", replace=True, local_only=True, expires="30d")
    assert rec.calls[0]["cmd"] == [
        "logosctl", "--json", "token", "issue", "--name", "bob",
        "--replace", "--local-only", "--expires", "30d",
    ]


def test_issue_token_omits_unset_flags(rec: Recorder):
    # `--local-only` in particular must not appear by accident: the daemon
    # rejects a local-only token presented over tcp/tcp_ssl, so a stray one
    # here would authenticate in the local tests and fail on the wire.
    rec.respond(stdout=json.dumps({"name": "carol", "token": "t"}))
    issue_token("carol")
    for flag in ("--replace", "--local-only", "--expires"):
        assert flag not in rec.calls[0]["cmd"]


def test_revoke_token_argv(rec: Recorder):
    rec.respond(stdout=json.dumps({"status": "ok"}))
    revoke_token("bob")
    assert rec.calls[0]["cmd"] == [
        "logosctl", "--json", "token", "revoke", "bob",
    ]


def test_list_tokens_argv_and_session_env(rec: Recorder):
    listing = [{"name": "alice", "issued_at": "2026-01-01T00:00:00Z",
                "expires_at": None, "local_only": False,
                "raw_file_present": True}]
    rec.respond(stdout=json.dumps(listing))
    assert list_tokens(config_dir=Path("/tmp/xcfg")) == listing
    assert rec.calls[0]["cmd"] == ["logosctl", "--json", "token", "ls"]
    # Same session-selection rule as the client commands: the app-level
    # `--config-dir` would land after the subcommand and be swallowed as a
    # positional, so it travels as an env var. And a token command is
    # daemon-less — it needs no credential, so LOGOSCTL_TOKEN stays unset.
    assert "--config-dir" not in rec.calls[0]["cmd"]
    env = rec.calls[0]["env"]
    assert env["LOGOSCTL_CONFIG_DIR"] == "/tmp/xcfg"
    assert {k for k, v in env.items() if os.environ.get(k) != v} == {
        "LOGOSCTL_CONFIG_DIR"}


def test_list_tokens_non_list_response_becomes_empty(rec: Recorder):
    # `token ls` on a session with no tokens.json prints an object envelope
    # rather than an array; callers iterate the return value, so it is
    # normalised to a list instead of handing them something unindexable.
    rec.respond(stdout=json.dumps({"status": "ok", "tokens": []}))
    assert list_tokens() == []


def test_watch_argv(rec: Recorder, monkeypatch: pytest.MonkeyPatch):
    # `on_event` spawns a long-lived `watch` through events.Subscription,
    # which builds its own argv (and its own env) rather than going through
    # run_json — so the global-flag placement has to be pinned there too.
    spawned: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            spawned["cmd"] = cmd
            spawned["env"] = kwargs["env"]
            self.stdout = iter(())
            self.stderr = None

        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    sub = LogosctlClient(config_dir=Path("/tmp/xcfg"), token="t").on_event(
        "chat", "message", lambda _e: None)
    sub.cancel()

    assert spawned["cmd"] == [
        "logosctl", "--json", "watch", "chat", "--event", "message",
    ]
    assert spawned["env"]["LOGOSCTL_CONFIG_DIR"] == "/tmp/xcfg"
    assert spawned["env"]["LOGOSCTL_TOKEN"] == "t"
