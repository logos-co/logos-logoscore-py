"""Unit tests for LogoscoreClient with subprocess.run monkeypatched.

These tests verify the wrapper's argv construction, env propagation, JSON
parsing, and exit-code → exception mapping — without needing a real
`logoscore` binary.
"""
from __future__ import annotations

import base64
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


def _b64url(b: bytes) -> str:
    """The canonical unpadded urlsafe base64 the client wraps bytes in."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def test_call_bytes_arg_uses_canonical_tag(rec: Recorder):
    # bytes cross the wire as `json:{"_bytes": "<b64url>"}` — NUL/high-byte
    # safe, unlike a raw latin-1 arg (which UTF-8-mangles bytes >= 0x80).
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    client = LogoscoreClient()
    client.call("m", "echoBytes", b"\x01\x02\xff")
    arg = rec.calls[0]["cmd"][4]
    assert arg.startswith("json:")
    assert json.loads(arg[len("json:"):]) == {"_bytes": _b64url(b"\x01\x02\xff")}


def test_call_container_args_use_json_prefix(rec: Recorder):
    # list/dict params go behind the CLI's `json:` prefix as natural JSON.
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    client = LogoscoreClient()
    client.call("m", "echoThings", [1, 2, 3], {"k": "v"})
    list_arg, map_arg = rec.calls[0]["cmd"][4], rec.calls[0]["cmd"][5]
    assert list_arg.startswith("json:") and json.loads(list_arg[5:]) == [1, 2, 3]
    assert map_arg.startswith("json:") and json.loads(map_arg[5:]) == {"k": "v"}


def test_call_none_arg_is_json_null(rec: Recorder):
    """A top-level `None` is the EMPTY inhabitant of an optional slot, and a
    positional slot spells empty as null — so it must cross argv as the CLI's
    `json:null`, not as `str(None)`.

    Measured end to end against `test_fullapi_ext_rust`'s
    `echoOptional(v: ?tstr) -> ?tstr` before this test was written: `"None"`
    is accepted and echoed back as the four-character STRING "None" (a
    present value), while `json:null` is accepted as empty — provably as
    EMPTY and not as a rejection, because `json:42` and `json:[1,2]` in the
    same slot come back `dispatch_failed / expected string at arg0` and the
    null does not.
    """
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    client = LogoscoreClient()
    client.call("m", "echoOptional", None)
    assert rec.calls[0]["cmd"] == [
        "logoscore", "call", "m", "echoOptional", "json:null", "--json",
    ]


def test_call_none_arg_keeps_arity_and_does_not_catch_the_string(rec: Recorder):
    """Two things a narrower fix gets wrong.

    ARITY: a positional empty occupies its slot — encoding it must not drop
    the argument, which would silently shift every later one left. `None` is
    sent here in the MIDDLE so a dropped arg is visible.

    THE STRING: only the `None` object is empty. The four-character string
    "None" is a present `tstr` value and must still cross as itself — a fix
    that matched on the rendered text (`if str(arg) == "None"`) would make
    the two indistinguishable, in the exact direction the bug already
    confused them.
    """
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    client = LogoscoreClient()
    client.call("m", "many", "a", None, 3, "None")
    assert rec.calls[0]["cmd"] == [
        "logoscore", "call", "m", "many", "a", "json:null", "3", "None", "--json",
    ]


def test_call_multi_arg_mixed_types_argv(rec: Recorder):
    """A multi-argument call mixing every `_arg_to_str` branch — locks in
    both argument arity (>2 positional args) and the per-arg wire encoding.
    `test_fullapi_cpp`'s methods are all 0/1-arg, so this unit test is where
    multi-argument argv construction is pinned."""
    rec.respond(stdout=json.dumps({"status": "success", "result": None}))
    client = LogoscoreClient()
    client.call("m", "many", "s", 7, True, b"\x00\xff", [1, "x"], {"k": 1})
    cmd = rec.calls[0]["cmd"]
    assert cmd[:7] == ["logoscore", "call", "m", "many", "s", "7", "true"]
    assert json.loads(cmd[7][5:]) == {"_bytes": _b64url(b"\x00\xff")}
    assert json.loads(cmd[8][5:]) == [1, "x"]
    assert json.loads(cmd[9][5:]) == {"k": 1}
    assert cmd[-1] == "--json"


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
