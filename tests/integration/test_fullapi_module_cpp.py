"""Integration tests exercising the **full supported type surface** of a
universal Logos module — parameters, return values, and events — against
`test_fullapi_cpp`.

`test_fullapi_cpp` is the C++ provider of the shared `full_api` contract
(`logos-test-modules/test-fullapi-module-cpp`). Unlike `test_basic_module*`,
whose events carry only scalars, it declares one echo method **and one
typed event per event-legal type**, so this file is the end-to-end proof
that every type the generator + protocol support round-trips through the
logoscore client — both as a call argument/return and as an event payload.

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

MODULE = "test_fullapi_cpp"


@pytest.fixture(scope="module")
def client(logoscore_bin, test_modules_dir, transport, request):
    """Build a daemon + client wired to whatever transport the suite is
    parametrised on. Mirror of `test_basic_module_methods.py::client` —
    kept duplicated rather than moved to a conftest helper so each test
    file can be read end-to-end without jumping between files."""
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


@pytest.mark.parametrize("n", [0, 42, -7, 9007199254740991])
def test_echo_int(client, n):
    assert client.call(MODULE, "echoInt", n) == n


@pytest.mark.parametrize("n", [0, 7, 4294967295])
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
    # sentinel (same as test_basic_module's doNothing).
    assert client.call(MODULE, "doVoid") is True


# ── Events: one typed event per event-legal type ─────────────────────────────
# Each event is fired by its bool-returning `fire<Name>Event(v)` shim (a
# bare void trigger would make the CLI exit non-zero). The provider emits
# the corresponding event with the argument as its single `arg0` payload.

def _capture_event(client, event: str, fire_method: str, value) -> dict:
    received: list[dict] = []
    got = threading.Event()

    def on_event(e: dict) -> None:
        received.append(e)
        got.set()

    with client.on_event(MODULE, event, on_event):
        time.sleep(0.5)  # let the watcher subscribe before firing
        assert client.call(MODULE, fire_method, value) is True
        assert got.wait(timeout=10.0), f"{event} not received"
    return received[0]


EVENT_CASES = [
    ("stringEvent",     "fireStringEvent",     "hello"),
    ("bytesEvent",      "fireBytesEvent",      b"\x01\x02\x03\xff"),
    ("intEvent",        "fireIntEvent",        -42),
    ("uintEvent",       "fireUintEvent",       7),
    ("doubleEvent",     "fireDoubleEvent",     2.5),
    ("boolEvent",       "fireBoolEvent",       True),
    ("anyEvent",        "fireAnyEvent",        {"k": "v", "n": 1}),
    ("stringListEvent", "fireStringListEvent", ["p", "q"]),
    ("intListEvent",    "fireIntListEvent",    [1, 2, 3]),
    ("uintListEvent",   "fireUintListEvent",   [4, 5]),
    ("doubleListEvent", "fireDoubleListEvent", [1.5, 2.5]),
    ("boolListEvent",   "fireBoolListEvent",   [True, False]),
    ("listEvent",       "fireListEvent",       [1, "two", {"k": 1}]),
    ("mapEvent",        "fireMapEvent",        {"mk": "mv"}),
]


@pytest.mark.parametrize(
    "event,fire,value", EVENT_CASES, ids=[c[0] for c in EVENT_CASES]
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
