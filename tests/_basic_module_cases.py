"""Shared expectations for every Q_INVOKABLE on `test_basic_module`.

Extracted so the same matrix can be run against different daemons (local
socket, TCP, TCP+SSL, in a docker container, with JSON or CBOR codec).
Structure: `(method_name, args, expected_value)`. `expected_value=None`
means "the call must dispatch cleanly, but we don't check what it
returns" — escape hatch for types the runtime can't yet serialise
deterministically. Prefer a concrete expected value when possible.
"""
from __future__ import annotations

# ── Method call matrix ─────────────────────────────────────────────────────
# Keep this parallel to the assertions in test_basic_module_methods.py so
# the two files drift together. A mismatch here shows up as a failing
# matrix entry in either the local or docker test run.

BASIC_MODULE_CASES: list[tuple[str, tuple, object]] = [
    # void returns — the CLI reports `true` as a success sentinel.
    ("doNothing",           (),                         True),
    ("doNothingWithArgs",   ("hello", 7),               True),

    # bool
    ("returnTrue",          (),                         True),
    ("returnFalse",         (),                         False),
    ("isPositive",          (5,),                       True),
    ("isPositive",          (-3,),                      False),
    ("isPositive",          (0,),                       False),

    # int
    ("returnInt",           (),                         42),
    ("addInts",             (2, 3),                     5),
    ("stringLength",        ("abcdef",),                6),

    # QString
    ("returnString",        (),                         "test_basic_module"),
    ("echo",                ("round-trip",),            "round-trip"),
    ("concat",              ("foo", "bar"),             "foobar"),

    # LogosResult — serialised on the wire as
    #   {"success": bool, "value": <any>, "error": <any>}
    # by qvariantToRpcValue in logos-cpp-sdk. `value`/`error` are whatever
    # the method stuffed into the struct; null when absent.
    ("successResult",       (),                         {"success": True,  "value": "operation succeeded",                                             "error": None}),
    ("errorResult",         (),                         {"success": False, "value": None,                                                              "error": "deliberate error for testing"}),
    ("resultWithMap",       (),                         {"success": True,  "value": {"name": "test", "count": 42, "active": True},                    "error": None}),
    ("resultWithList",      (),                         {"success": True,  "value": [{"id": 1, "label": "first"}, {"id": 2, "label": "second"}],      "error": None}),
    ("validateInput",       ("hello",),                 {"success": True,  "value": {"input": "hello", "length": 5},                                  "error": None}),
    ("validateInput",       ("",),                      {"success": False, "value": None,                                                              "error": "input cannot be empty"}),

    # QVariant
    ("returnVariantInt",    (),                         99),
    ("returnVariantString", (),                         "variant_string"),
    ("returnVariantMap",    (),                         {"key": "value", "number": 7}),
    ("returnVariantList",   (),                         ["alpha", "beta", "gamma"]),

    # QJsonArray
    ("returnJsonArray",     (),                         [1, 2, 3]),
    ("makeJsonArray",       ("x", "y"),                 ["x", "y"]),

    # QStringList
    ("returnStringList",    (),                         ["one", "two", "three"]),
    ("splitString",         ("a,b,c",),                 ["a", "b", "c"]),

    # Scalar echoes
    ("echoInt",             (123,),                     123),
    ("echoBool",            (True,),                    True),
    ("echoBool",            (False,),                   False),

    # QByteArray / QUrl (marshalled as string by the CLI)
    ("byteArraySize",       ("12345",),                 5),
    ("urlToString",         ("https://example.com/p?q=1",), "https://example.com/p?q=1"),

    # 0..5-arg combinatorics
    ("noArgs",              (),                                "noArgs()"),
    ("oneArg",              ("x",),                            "oneArg(x)"),
    ("twoArgs",             ("x", 7),                          "twoArgs(x, 7)"),
    ("threeArgs",           ("x", 7, True),                    "threeArgs(x, 7, true)"),
    ("fourArgs",            ("x", 7, False, "y"),              "fourArgs(x, 7, false, y)"),
    ("fiveArgs",            ("x", 7, True,  "y", 9),           "fiveArgs(x, 7, true, y, 9)"),
]
