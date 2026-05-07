"""Integration tests calling every Q_INVOKABLE on `test_basic_module`.

Covers the full matrix of argument types (string, int, bool, QStringList,
QByteArray, QUrl) and return types (void, bool, int, QString, LogosResult,
QVariant, QJsonArray, QStringList) exposed by `test-basic-module` in the
`logos-test-modules` repo. See:
  repos/logos-test-modules/test-basic-module/src/test_basic_module_plugin.h
  repos/logos-test-modules/test-basic-module/src/test_basic_module_plugin.cpp

Assertions here are written out one test per method for readability. The
docker smoke suite (`tests/docker_smoke/test_docker_smoke.py`) replays the
same matrix over TCP in both JSON and CBOR via `tests/_basic_module_cases.py`;
keep the two in sync when adding methods to `test_basic_module`.

Skipped unless LOGOSCORE_BIN and LOGOSCORE_TEST_MODULES_DIR are set — the
Nix `integration` check wires both up.
"""
from __future__ import annotations

import threading
import time

import pytest

from logoscore import LogoscoreDaemon

MODULE = "test_basic_module"


@pytest.fixture(scope="module")
def client(logoscore_bin, test_modules_dir, transport, request):
    """Build a daemon + client wired to whatever transport the suite is
    parametrised on.

    The ports are bound at fixture entry (module scope, not per-test)
    so a single daemon serves the whole file's tests. tcp_ssl pulls the
    self-signed cert from the session-scoped fixture in tests/conftest.py
    and asks the client to skip peer verification (the cert won't match
    any real CA)."""
    import socket

    def _pick_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    kwargs = {}
    client_kwargs: dict = {"transport": transport}
    if transport != "local":
        kwargs["transports"] = [transport]
        if transport == "tcp":
            kwargs["tcp_port"] = _pick_free_port()
        elif transport == "tcp_ssl":
            cert, key = request.getfixturevalue("self_signed_cert")
            kwargs["tcp_ssl_port"] = _pick_free_port()
            kwargs["ssl_cert"] = cert
            kwargs["ssl_key"] = key
            # Self-signed cert won't validate against any CA — the
            # client has to opt out of peer verification or every call
            # fails the TLS handshake.
            client_kwargs["no_verify_peer"] = True
    with LogoscoreDaemon(
        modules_dir=test_modules_dir, binary=logoscore_bin, **kwargs,
    ) as daemon:
        c = daemon.client(**client_kwargs)
        c.load_module(MODULE)
        yield c


# ── Return type: void ────────────────────────────────────────────────────────
# The CLI returns `true` as a success indicator for Q_INVOKABLE methods that
# return `void` — there's no distinction in the JSON between "true return
# value" and "void return" (see logoscore's call_executor.cpp path).

def test_do_nothing(client):
    assert client.call(MODULE, "doNothing") is True


def test_do_nothing_with_args(client):
    assert client.call(MODULE, "doNothingWithArgs", "hello", 7) is True


# ── Return type: bool ────────────────────────────────────────────────────────

def test_return_true(client):
    assert client.call(MODULE, "returnTrue") is True


def test_return_false(client):
    assert client.call(MODULE, "returnFalse") is False


@pytest.mark.parametrize("value,expected", [(5, True), (-3, False), (0, False)])
def test_is_positive(client, value, expected):
    assert client.call(MODULE, "isPositive", value) is expected


# ── Return type: int ─────────────────────────────────────────────────────────

def test_return_int(client):
    assert client.call(MODULE, "returnInt") == 42


def test_add_ints(client):
    assert client.call(MODULE, "addInts", 2, 3) == 5


def test_string_length(client):
    assert client.call(MODULE, "stringLength", "abcdef") == 6


# ── Return type: QString ─────────────────────────────────────────────────────

def test_return_string(client):
    assert client.call(MODULE, "returnString") == "test_basic_module"


def test_echo(client):
    assert client.call(MODULE, "echo", "round-trip") == "round-trip"


def test_concat(client):
    assert client.call(MODULE, "concat", "foo", "bar") == "foobar"


# ── Return type: LogosResult ─────────────────────────────────────────────────
# Serialised on the wire as `{"success": bool, "value": <any>, "error": <any>}`
# by qvariantToRpcValue in logos-cpp-sdk (see plain/qvariant_rpc_value.cpp).

def test_success_result(client):
    assert client.call(MODULE, "successResult") == {
        "success": True, "value": "operation succeeded", "error": None,
    }


def test_error_result(client):
    assert client.call(MODULE, "errorResult") == {
        "success": False, "value": None, "error": "deliberate error for testing",
    }


def test_result_with_map(client):
    assert client.call(MODULE, "resultWithMap") == {
        "success": True,
        "value": {"name": "test", "count": 42, "active": True},
        "error": None,
    }


def test_result_with_list(client):
    assert client.call(MODULE, "resultWithList") == {
        "success": True,
        "value": [{"id": 1, "label": "first"}, {"id": 2, "label": "second"}],
        "error": None,
    }


def test_validate_input_success(client):
    assert client.call(MODULE, "validateInput", "hello") == {
        "success": True,
        "value": {"input": "hello", "length": 5},
        "error": None,
    }


def test_validate_input_error(client):
    assert client.call(MODULE, "validateInput", "") == {
        "success": False, "value": None, "error": "input cannot be empty",
    }


# ── Return type: QVariant ────────────────────────────────────────────────────

def test_return_variant_int(client):
    assert client.call(MODULE, "returnVariantInt") == 99


def test_return_variant_string(client):
    assert client.call(MODULE, "returnVariantString") == "variant_string"


def test_return_variant_map(client):
    res = client.call(MODULE, "returnVariantMap")
    assert res == {"key": "value", "number": 7}


def test_return_variant_list(client):
    assert client.call(MODULE, "returnVariantList") == ["alpha", "beta", "gamma"]


# ── Return type: QJsonArray ──────────────────────────────────────────────────

def test_return_json_array(client):
    assert client.call(MODULE, "returnJsonArray") == [1, 2, 3]


def test_make_json_array(client):
    assert client.call(MODULE, "makeJsonArray", "x", "y") == ["x", "y"]


# ── Return type: QStringList ─────────────────────────────────────────────────

def test_return_string_list(client):
    assert client.call(MODULE, "returnStringList") == ["one", "two", "three"]


def test_split_string(client):
    assert client.call(MODULE, "splitString", "a,b,c") == ["a", "b", "c"]


# ── Parameter types ──────────────────────────────────────────────────────────

def test_echo_int(client):
    assert client.call(MODULE, "echoInt", 123) == 123


@pytest.mark.parametrize("value", [True, False])
def test_echo_bool(client, value):
    assert client.call(MODULE, "echoBool", value) is value


def test_byte_array_size(client):
    # Bytes are passed to the CLI as a string; the C++ side calls
    # QByteArray::size() on it, which counts UTF-8 bytes for non-ASCII.
    assert client.call(MODULE, "byteArraySize", "12345") == 5


def test_url_to_string(client):
    url = "https://example.com/path?q=1"
    assert client.call(MODULE, "urlToString", url) == url


# ── Argument counts 0..5 ─────────────────────────────────────────────────────

def test_no_args(client):
    assert client.call(MODULE, "noArgs") == "noArgs()"


def test_one_arg(client):
    assert client.call(MODULE, "oneArg", "x") == "oneArg(x)"


def test_two_args(client):
    assert client.call(MODULE, "twoArgs", "x", 7) == "twoArgs(x, 7)"


def test_three_args(client):
    assert client.call(MODULE, "threeArgs", "x", 7, True) == "threeArgs(x, 7, true)"


def test_four_args(client):
    got = client.call(MODULE, "fourArgs", "x", 7, False, "y")
    assert got == "fourArgs(x, 7, false, y)"


def test_five_args(client):
    got = client.call(MODULE, "fiveArgs", "x", 7, True, "y", 9)
    assert got == "fiveArgs(x, 7, true, y, 9)"


# ── Async helpers ────────────────────────────────────────────────────────────

def test_echo_with_delay(client):
    start = time.monotonic()
    assert client.call(MODULE, "echoWithDelay", "pong", 200) == "pong"
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2, f"expected >=200ms, got {elapsed:.3f}s"


# ── Events ───────────────────────────────────────────────────────────────────

def test_emit_test_event_single_arg(client):
    received: list[dict] = []
    evt = threading.Event()

    def on_event(event: dict) -> None:
        received.append(event)
        evt.set()

    with client.on_event(MODULE, "testEvent", on_event):
        time.sleep(0.5)  # let the watcher subscribe
        client.call(MODULE, "emitTestEvent", "payload-123")
        assert evt.wait(timeout=10.0), "testEvent not received"

    assert received, "expected at least one event"
    e = received[0]
    flat = str(e)
    assert "payload-123" in flat


def test_emit_multi_arg_event(client):
    received: list[dict] = []
    evt = threading.Event()

    def on_event(event: dict) -> None:
        received.append(event)
        evt.set()

    with client.on_event(MODULE, "multiArgEvent", on_event):
        time.sleep(0.5)
        client.call(MODULE, "emitMultiArgEvent", "label", 42)
        assert evt.wait(timeout=10.0), "multiArgEvent not received"

    flat = str(received[0])
    assert "label" in flat
    assert "42" in flat
