"""Shared expectations for the `test_fullapi_cpp` full type surface.

Extracted so the same matrix can be replayed against different daemons
(local socket, TCP, TCP+SSL, in a docker container, over JSON or CBOR).
The full-api module declares one echo method per supported type **and one
typed event per event-legal type** — so these tables are the single place
that pins the whole parameter/return/event surface, and the docker smoke
suite replays them over both wire codecs.

Structure:
  FULLAPI_METHOD_CASES — `(method, args, expected)`; `expected=None` means
    "must dispatch cleanly, value unchecked".
  FULLAPI_EVENT_CASES  — `(event, fire_method, value)`; firing
    `fire<X>Event(value)` emits `<X>Event` whose `data.arg0 == value`.

Kept parallel to the inline assertions in
`tests/integration/test_fullapi_module_cpp.py` so the two drift together;
`FULLAPI_EVENT_CASES` is imported directly by that file (single source).

`bytes` args/returns cross the wire as the canonical `{"_bytes":"<b64url>"}`
tag (see `LogoscoreClient._arg_to_str` / `_proc.decode_bytes_tags`), so
every byte value round-trips. Float cases use exactly-representable values
so a plain `==` holds across both codecs (no approximate compare needed).

See: repos/logos-test-modules/test-fullapi-module-cpp/src/test_fullapi_cpp_impl.h
"""
from __future__ import annotations

# ── Method call matrix (param / return, every supported type) ───────────────
FULLAPI_METHOD_CASES: list[tuple[str, tuple, object]] = [
    # identity
    ("whoAmI",        (),                        "test_fullapi_cpp"),

    # tstr
    ("echoString",    ("round-trip",),           "round-trip"),

    # int / uint
    ("echoInt",       (42,),                     42),
    ("echoInt",       (-7,),                     -7),
    ("echoUint",      (7,),                      7),

    # 64-bit boundaries. These matter far more than the small values above:
    # this table is what the tcp / tcp_ssl / json / cbor matrices replay, and it
    # had NO value outside int32 range, so the plain wire's uint64 handling was
    # entirely untested. A uint64 above int64max used to arrive as -1 there
    # (RpcValue had no unsigned alternative), and int64::min/max exercise the
    # signed edges the double-degradation bugs kept landing on.
    ("echoUint",      (2**64 - 1,),              2**64 - 1),
    ("echoUint",      (2**63,),                  2**63),
    ("echoInt",       (2**63 - 1,),              2**63 - 1),
    ("echoInt",       (-(2**63),),               -(2**63)),
    # 2^53+1 is the smallest integer a double cannot represent — it separates
    # "degraded through a float" from "wrapped as an integer".
    ("echoInt",       (2**53 + 1,),              2**53 + 1),

    # float64 — values MUST stay exactly-representable (dyadic) so a plain
    # `==` holds across json + cbor; the docker matrix compares without approx.
    ("echoDouble",    (2.5,),                    2.5),
    ("echoDouble",    (-0.5,),                   -0.5),

    # bool
    ("echoBool",      (True,),                   True),
    ("echoBool",      (False,),                  False),

    # bstr — canonical {"_bytes"} tag, NUL/high-byte safe
    ("echoBytes",     (b"hello",),               b"hello"),
    ("echoBytes",     (b"\x01\x02\x03\xff",),    b"\x01\x02\x03\xff"),

    # any — scalars, container, and map all pass through unchanged
    ("echoAny",       ("hi",),                   "hi"),
    ("echoAny",       (42,),                     42),
    ("echoAny",       (True,),                   True),
    ("echoAny",       ([1, 2, 3],),              [1, 2, 3]),
    ("echoAny",       ({"k": "v"},),             {"k": "v"}),

    # typed arrays
    ("echoStringList", (["a", "b", "c"],),       ["a", "b", "c"]),
    ("echoIntList",    ([1, 2, 3],),             [1, 2, 3]),
    ("echoIntList",    ([-1, 0, 5],),            [-1, 0, 5]),
    ("echoIntList",    ([],),                    []),
    ("echoUintList",   ([4, 5, 6],),             [4, 5, 6]),
    ("echoDoubleList", ([1.5, 2.5, -0.5],),      [1.5, 2.5, -0.5]),
    ("echoBoolList",   ([True, False, True],),   [True, False, True]),

    # [any] (LogosList) — heterogeneous incl. a nested float (dyadic),
    # nested map, and nested list (exercises float-inside-[any] over cbor)
    ("echoList",       ([1, "two", 3.5, {"k": 1}, [9]],), [1, "two", 3.5, {"k": 1}, [9]]),

    # {tstr:any} (LogosMap) — mixed value types incl. a nested float + nesting
    ("echoMap",        ({"s": "v", "n": 42, "f": 1.5, "b": True, "nested": {"x": [1, 2]}},),
                       {"s": "v", "n": 42, "f": 1.5, "b": True, "nested": {"x": [1, 2]}}),

    # result — {"success", "value", "error"} (absent side is null)
    ("makeResult",     (True,),
                       {"success": True, "value": {"ok": True, "provider": "test_fullapi_cpp"}, "error": None}),
    ("makeResult",     (False,),
                       {"success": False, "value": None, "error": "deliberate error for testing"}),

    # void — CLI reports `true` as the success sentinel
    ("doVoid",         (),                       True),
]


# ── Typed-event matrix (one event per event-legal type) ─────────────────────
# Each event is fired by its bool-returning `fire<X>Event(v)` shim; the
# provider emits `<X>Event` carrying `v` as its single `data.arg0` payload.
FULLAPI_EVENT_CASES: list[tuple[str, str, object]] = [
    ("stringEvent",     "fireStringEvent",     "hello"),
    ("bytesEvent",      "fireBytesEvent",      b"\x01\x02\x03\xff"),
    ("intEvent",        "fireIntEvent",        -42),
    ("uintEvent",       "fireUintEvent",       7),
    # The event path's own 64-bit boundaries — the method cases above do not
    # cover them, and the two paths have diverged before (see M6: exact as a
    # method return, degraded to a double as an event).
    ("uintEvent",       "fireUintEvent",       2**64 - 1),
    ("intEvent",        "fireIntEvent",        -(2**63)),
    ("intEvent",        "fireIntEvent",        2**53 + 1),
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
