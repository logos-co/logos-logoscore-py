"""Unit tests for LogoscoreClient with subprocess.run monkeypatched.

These tests verify the wrapper's argv construction, env propagation, JSON
parsing, and exit-code → exception mapping — without needing a real
`logoscore` binary.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from logoscore import LogoscoreClient
from logoscore.errors import (
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


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    return Recorder(monkeypatch)


def test_status_parses_json(rec: Recorder):
    rec.respond(stdout=json.dumps({"daemon": {"status": "running"}}))
    client = LogoscoreClient()
    out = client.status()
    assert out == {"daemon": {"status": "running"}}
    assert rec.calls[0]["cmd"] == ["logoscore", "status", "--json"]


def test_config_dir_and_token_propagated(rec: Recorder):
    rec.respond(stdout="{}")
    client = LogoscoreClient(config_dir=Path("/tmp/xcfg"), token="tok-123")
    client.status()
    env = rec.calls[0]["env"]
    assert env["LOGOSCORE_CONFIG_DIR"] == "/tmp/xcfg"
    assert env["LOGOSCORE_TOKEN"] == "tok-123"


def test_list_modules_loaded_flag(rec: Recorder):
    rec.respond(stdout=json.dumps([{"name": "chat"}]))
    client = LogoscoreClient()
    out = client.list_modules(loaded=True)
    assert out == [{"name": "chat"}]
    assert rec.calls[0]["cmd"] == ["logoscore", "list-modules", "--loaded", "--json"]


def test_call_unwraps_result(rec: Recorder):
    rec.respond(stdout=json.dumps({"status": "success", "result": 42}))
    client = LogoscoreClient()
    assert client.call("m", "meth", "a", 1, True) == 42
    assert rec.calls[0]["cmd"] == [
        "logoscore", "call", "m", "meth", "a", "1", "true", "--json",
    ]


def test_call_path_arg_becomes_at_file(rec: Recorder):
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    client = LogoscoreClient()
    client.call("m", "loadConfig", Path("/etc/x.json"))
    assert rec.calls[0]["cmd"] == [
        "logoscore", "call", "m", "loadConfig", "@/etc/x.json", "--json",
    ]


def test_call_error_envelope_raises_method_error(rec: Recorder):
    # Envelope says error but exit code is 0 — this path exists for CLIs that
    # don't always map error envelopes to non-zero exit codes.
    rec.respond(
        stdout=json.dumps({"status": "error", "code": "BAD", "message": "no"})
    )
    client = LogoscoreClient()
    with pytest.raises(MethodError) as excinfo:
        client.call("m", "meth")
    assert excinfo.value.code == "BAD"


def test_nonzero_exit_code_2_raises_daemon_not_running(rec: Recorder):
    rec.respond(returncode=2, stderr="no daemon")
    client = LogoscoreClient()
    with pytest.raises(DaemonNotRunningError):
        client.status()


def test_nonzero_exit_code_3_raises_module_error(rec: Recorder):
    rec.respond(returncode=3, stderr="not found")
    client = LogoscoreClient()
    with pytest.raises(ModuleError):
        client.load_module("missing")


def test_nonzero_exit_code_4_raises_method_error(rec: Recorder):
    rec.respond(returncode=4, stderr="timeout")
    client = LogoscoreClient()
    with pytest.raises(MethodError):
        client.call("m", "meth")


def test_stop_subcommand(rec: Recorder):
    rec.respond(stdout=json.dumps({"status": "ok"}))
    client = LogoscoreClient()
    client.stop()
    assert rec.calls[0]["cmd"] == ["logoscore", "stop", "--json"]


def test_all_module_management_commands(rec: Recorder):
    client = LogoscoreClient()
    for method, args, expected_subcmd in [
        ("load_module", ("chat",), ["load-module", "chat"]),
        ("unload_module", ("chat",), ["unload-module", "chat"]),
        ("reload_module", ("chat",), ["reload-module", "chat"]),
        ("module_info", ("chat",), ["module-info", "chat"]),
    ]:
        rec.respond(stdout="{}")
        getattr(client, method)(*args)
        assert rec.calls[-1]["cmd"] == ["logoscore", *expected_subcmd, "--json"]
