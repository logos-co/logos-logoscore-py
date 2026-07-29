"""Integration tests exercising the **full supported type surface** of a
universal Logos module — parameters, return values, and events — against
`test_fullapi_cpp`.

`test_fullapi_cpp` is the C++ provider of the shared `full_api` contract
(`logos-test-modules/test-fullapi-module-cpp`). It declares one echo method
**and one typed event per event-legal type**, so this file is the end-to-end
proof that every type the generator + protocol support round-trips through
the logoscore client — both as a call argument/return and as an event
payload.

Type surface covered (each as param, return, and — where legal — event):

  LIDL          Python arg          Python return / event payload
  ------------  ------------------  ------------------------------
  tstr          str                 str
  bstr          bytes               bytes            (canonical {"_bytes"} tag)
  int           int                 int
  uint          int                 int
  float64       float               float
  bool          bool                bool
  any           str/int/float/      same Python value
                bool/list/dict
  [tstr]        list[str]           list[str]
  [int]/[uint]  list[int]           list[int]
  [float64]     list[float]         list[float]
  [bool]        list[bool]          list[bool]
  [any]         list                list            (heterogeneous)
  {tstr:any}    dict                dict
  result        —                   {"success","value","error"}
  void          —                   True             (CLI success sentinel)

Container args + non-scalar `any` reach the daemon via the CLI's `json:`
prefix, and `bytes` via the canonical `{"_bytes": "<b64url>"}` tag — both
handled transparently by `LogoscoreClient` (see `_arg_to_str`). Byte-array
**event** payloads decode back to `bytes` in the event pump, symmetric
with `call`.

See:
  repos/logos-test-modules/test-fullapi-module-cpp/src/test_fullapi_cpp_impl.h

Skipped unless LOGOSCORE_BIN and LOGOSCORE_TEST_MODULES_DIR are set — the
Nix `integration` check wires both up (and bundles this module into the
test modules dir).
"""
from __future__ import annotations

import threading
import time

import pytest

from logoscore import LogoscoreDaemon

from .._fullapi_module_cases import FULLAPI_EVENT_CASES

MODULE = "test_fullapi_cpp"


@pytest.fixture(scope="module")
def client(logoscore_bin, test_modules_dir, transport, request):
    """Build a daemon + client wired to whatever transport the suite is
    parametrised on. Kept inline (rather than moved to a conftest helper)
    so each test file can be read end-to-end without jumping between
    files — mirrors the fixture in `test_end_to_end.py`."""
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
            client_kwargs["no_verify_peer"] = True
    with LogoscoreDaemon(
        modules_dir=test_modules_dir, binary=logoscore_bin, **kwargs,
    ) as daemon:
        c = daemon.client(**client_kwargs)
        c.load_module(MODULE)
        yield c


# ── Scalar params / returns ──────────────────────────────────────────────────

def test_who_am_i(client):
    assert client.call(MODULE, "whoAmI") == "test_fullapi_cpp"


def test_echo_string(client):
    assert client.call(MODULE, "echoString", "round-trip") == "round-trip"


# 64-bit boundaries belong here specifically. This module is replayed by the
# local, tcp and tcp_ssl checks, and the highest value it used to carry was
# 2^53-1 (int) / 2^32-1 (uint) — so the plain wire's 64-bit handling was never
# exercised at all. It was broken: RpcValue had no unsigned alternative, and a
# uint above int64max arrived as -1.
# 2^53+1 is the smallest integer a double cannot hold, which separates
# "degraded through a float" from "wrapped as an integer".
@pytest.mark.parametrize(
    "n", [0, 42, -7, 9007199254740991, 2**53 + 1, 2**63 - 1, -(2**63)]
)
def test_echo_int(client, n):
    assert client.call(MODULE, "echoInt", n) == n


@pytest.mark.parametrize("n", [0, 7, 4294967295, 2**53 + 1, 2**63, 2**64 - 1])
def test_echo_uint(client, n):
    assert client.call(MODULE, "echoUint", n) == n


@pytest.mark.parametrize("x", [0.0, 2.5, -3.25, 1e-6])
def test_echo_double(client, x):
    assert client.call(MODULE, "echoDouble", x) == pytest.approx(x)


@pytest.mark.parametrize("b", [True, False])
def test_echo_bool(client, b):
    assert client.call(MODULE, "echoBool", b) is b


@pytest.mark.parametrize(
    "payload",
    [
        b"",                        # empty
        b"hello",                   # ascii
        b"\x01\x02\x03",            # low bytes
        b"\x00\x10\xff\x80",        # NUL + high bytes — canonical tag is lossless
        bytes(range(256)),          # full byte range
    ],
    ids=["empty", "ascii", "low", "nul-high", "full-range"],
)
def test_echo_bytes(client, payload):
    # bytes cross the wire as the canonical {"_bytes": "<b64url>"} tag, so
    # every byte value (incl. NUL and >= 0x80) round-trips losslessly — a
    # raw latin-1 arg would UTF-8-mangle the high bytes.
    assert client.call(MODULE, "echoBytes", payload) == payload


@pytest.mark.parametrize(
    "value",
    ["hello", 42, 3.5, True, [1, 2, 3], {"k": "v", "n": 1}],
    ids=["str", "int", "float", "bool", "list", "map"],
)
def test_echo_any(client, value):
    got = client.call(MODULE, "echoAny", value)
    if isinstance(value, float):
        assert got == pytest.approx(value)
    else:
        assert got == value


# ── Container params / returns ───────────────────────────────────────────────

def test_echo_string_list(client):
    assert client.call(MODULE, "echoStringList", ["a", "b", "c"]) == ["a", "b", "c"]


@pytest.mark.parametrize("xs", [[], [1, 2, 3], [-1, 0, 5]])
def test_echo_int_list(client, xs):
    assert client.call(MODULE, "echoIntList", xs) == xs


def test_echo_uint_list(client):
    assert client.call(MODULE, "echoUintList", [4, 5, 6]) == [4, 5, 6]


def test_echo_double_list(client):
    assert client.call(MODULE, "echoDoubleList", [1.5, 2.5, -3.0]) == pytest.approx(
        [1.5, 2.5, -3.0]
    )


def test_echo_bool_list(client):
    assert client.call(MODULE, "echoBoolList", [True, False, True]) == [True, False, True]


def test_echo_list_heterogeneous(client):
    # [any] — a LogosList carrying mixed element types (incl. a nested map).
    value = [1, "two", 3.5, True, {"k": 1}, [9, 8]]
    got = client.call(MODULE, "echoList", value)
    assert got == value


def test_echo_map(client):
    value = {"s": "v", "n": 42, "f": 1.5, "b": True, "nested": {"x": [1, 2]}}
    assert client.call(MODULE, "echoMap", value) == value


# ── result / void returns ────────────────────────────────────────────────────

def test_make_result_success(client):
    assert client.call(MODULE, "makeResult", True) == {
        "success": True,
        "value": {"ok": True, "provider": "test_fullapi_cpp"},
        "error": None,
    }


def test_make_result_error(client):
    assert client.call(MODULE, "makeResult", False) == {
        "success": False,
        "value": None,
        "error": "deliberate error for testing",
    }


def test_do_void(client):
    # void methods have no value; the CLI reports `true` as the success
    # sentinel.
    assert client.call(MODULE, "doVoid") is True


# ── Events: one typed event per event-legal type ─────────────────────────────
# Each event is fired by its bool-returning `fire<Name>Event(v)` shim (a
# bare void trigger would make the CLI exit non-zero). The provider emits
# the corresponding event with the argument as its single `arg0` payload.

def _capture_event(
    client, event: str, fire_method: str, value, overall_timeout: float = 20.0
) -> dict:
    """Subscribe, then fire the trigger and wait — re-firing until the event
    arrives or `overall_timeout` elapses.

    The watcher subscribes on a background subprocess, so there is an
    unavoidable race between "watch is live" and "we fire". A single fixed
    sleep can't cover a slow-to-subscribe watcher on CI: if the first (and
    only) fire lands before the subscription is live, the event is missed
    and no later wait can recover it. The `fire<X>Event` triggers are
    idempotent emits, so re-firing on a short cadence closes the race
    without hard-coding a settle duration."""
    received: list[dict] = []
    got = threading.Event()

    def on_event(e: dict) -> None:
        received.append(e)
        got.set()

    with client.on_event(MODULE, event, on_event):
        deadline = time.monotonic() + overall_timeout
        while True:
            assert client.call(MODULE, fire_method, value) is True
            if got.wait(timeout=1.0):
                break
            assert time.monotonic() < deadline, (
                f"{event} not received within {overall_timeout}s"
            )
    return received[0]


# The typed-event matrix is shared with the docker-smoke suite so the two
# stay in lockstep — see tests/_fullapi_module_cases.py::FULLAPI_EVENT_CASES.
@pytest.mark.parametrize(
    "event,fire,value", FULLAPI_EVENT_CASES,
    ids=[c[0] for c in FULLAPI_EVENT_CASES],
)
def test_typed_event(client, event, fire, value):
    evt = _capture_event(client, event, fire, value)
    assert evt["event"] == event
    assert evt["module"] == MODULE
    payload = evt["data"]["arg0"]
    # bytes decode back to `bytes` in the pump; floats compare approximately.
    if isinstance(value, float) or (
        isinstance(value, list) and value and isinstance(value[0], float)
    ):
        assert payload == pytest.approx(value)
    else:
        assert payload == value
