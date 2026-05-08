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


# ── Transport-override env propagation ───────────────────────────────────────
#
# The CLI side has matching `--client-*` flags + `LOGOSCORE_CLIENT_*` env-var
# fallbacks; these tests pin the wrapper-side contract. If a kwarg is unset
# the corresponding env var must be absent (so the CLI falls through to the
# disk-side dial spec); when set the env var carries the value verbatim.

def test_no_transport_kwargs_means_no_client_env(rec: Recorder):
    rec.respond(stdout="{}")
    client = LogoscoreClient()
    client.status()
    env = rec.calls[0]["env"] or {}
    for var in (
        "LOGOSCORE_CLIENT_TRANSPORT",
        "LOGOSCORE_CLIENT_TCP_HOST",
        "LOGOSCORE_CLIENT_TCP_PORT",
        "LOGOSCORE_CLIENT_NO_VERIFY_PEER",
        "LOGOSCORE_CLIENT_CODEC",
    ):
        assert var not in env, f"unexpected {var}={env.get(var)!r}"


def test_transport_kwarg_propagates(rec: Recorder):
    rec.respond(stdout="{}")
    LogoscoreClient(transport="tcp_ssl").status()
    assert rec.calls[0]["env"]["LOGOSCORE_CLIENT_TRANSPORT"] == "tcp_ssl"


def test_tcp_host_and_port_propagate(rec: Recorder):
    rec.respond(stdout="{}")
    LogoscoreClient(tcp_host="daemon.example.com", tcp_port=51823).status()
    env = rec.calls[0]["env"]
    assert env["LOGOSCORE_CLIENT_TCP_HOST"] == "daemon.example.com"
    assert env["LOGOSCORE_CLIENT_TCP_PORT"] == "51823"


def test_no_verify_peer_propagates_only_when_true(rec: Recorder):
    # Default (no_verify_peer=False): env var absent → CLI uses disk
    # value (verify by default).
    rec.respond(stdout="{}")
    LogoscoreClient(no_verify_peer=False).status()
    env = rec.calls[0]["env"] or {}
    assert "LOGOSCORE_CLIENT_NO_VERIFY_PEER" not in env

    # Explicit True: env var set to "1" — CLI applies it as a flag.
    # This is the typical smoke-test path with a self-signed cert.
    rec.respond(stdout="{}")
    LogoscoreClient(no_verify_peer=True).status()
    assert rec.calls[1]["env"]["LOGOSCORE_CLIENT_NO_VERIFY_PEER"] == "1"


def test_codec_propagates(rec: Recorder):
    rec.respond(stdout="{}")
    LogoscoreClient(codec="cbor").status()
    assert rec.calls[0]["env"]["LOGOSCORE_CLIENT_CODEC"] == "cbor"


def test_tcp_port_zero_still_propagates(rec: Recorder):
    # Edge case: 0 is a meaningful value (auto-pick), not "unset". The
    # wrapper distinguishes None from 0 — a user passing `tcp_port=0`
    # gets the env var set to "0", not omitted.
    rec.respond(stdout="{}")
    LogoscoreClient(tcp_port=0).status()
    assert rec.calls[0]["env"]["LOGOSCORE_CLIENT_TCP_PORT"] == "0"
