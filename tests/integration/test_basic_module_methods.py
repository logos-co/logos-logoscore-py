"""Integration tests calling every Q_INVOKABLE on `test_basic_module`.

Covers the full matrix of argument types (string, int, bool, QStringList,
QByteArray, QUrl) and return types (void, bool, int, QString, LogosResult,
QVariant, QJsonArray, QStringList) exposed by `test-basic-module` in the
`logos-test-modules` repo. See:
  repos/logos-test-modules/test-basic-module/src/test_basic_module_plugin.h
  repos/logos-test-modules/test-basic-module/src/test_basic_module_plugin.cpp

The data-driven method cases come from `_basic_module_cases.BASIC_MODULE_CASES`
so the docker smoke test (which also replays the full matrix over TCP in
both JSON and CBOR) can't drift from the expectations here.

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
def client(logoscore_bin, test_modules_dir, transport):
    kwargs = {}
    if transport != "local":
        kwargs["transports"] = [transport]
        if transport == "tcp":
            # Scoped per-test-module so this fixture doesn't collide with
            # per-test `tcp_port`. Pick at daemon startup.
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                kwargs["tcp_port"] = s.getsockname()[1]
    with LogoscoreDaemon(
        modules_dir=test_modules_dir, binary=logoscore_bin, **kwargs,
    ) as daemon:
        c = daemon.client(transport=transport)
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
# The current logoscore CLI does not serialise `LogosResult` return values to
# JSON — they come through as `null`. The method still dispatches correctly
# (no exception, daemon logs show the call), so we just assert the RPC
# succeeds. If/when the CLI learns to unpack LogosResult, tighten these.

@pytest.mark.parametrize(
    "method,args",
    [
        ("successResult", ()),
        ("errorResult", ()),
        ("resultWithMap", ()),
        ("resultWithList", ()),
        ("validateInput", ("hello",)),
        ("validateInput", ("",)),
    ],
)
def test_logos_result_dispatches(client, method, args):
    # The method dispatches cleanly; the return shape is a CLI limitation.
    assert client.call(MODULE, method, *args) is None


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
