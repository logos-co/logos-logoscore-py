"""The inline full_api table and the shared conformance table must agree.

`tests/_fullapi_module_cases.py` and
`logos-test-modules/conformance/cases.json` describe the same surface: one is
this suite's parametrization, the other is the language-neutral table every
consumer driver replays. Two hand-maintained copies of the same expectations
drift, and the drift is silent — the whole point of the shared table is that
adding a type costs one row and no per-consumer work.

This does not merge them (the inline table is what the integration suite
parametrizes on, and rewriting a passing suite to prove a point is a bad
trade). It makes drift a RED TEST instead of a surprise: every method/event the
inline table pins must exist in the shared table with the same expectation.

Skips when the shared table is not resolvable — the flake points at it via the
logos-test-modules input, but a plain `pytest` run on a bare checkout has no
such path and should still run the rest of the unit suite.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from .._fullapi_module_cases import FULLAPI_EVENT_CASES, FULLAPI_METHOD_CASES

_SEARCH = [
    os.environ.get("LOGOS_CONFORMANCE_DIR"),
    # sibling checkout, i.e. a workspace
    str(Path(__file__).resolve().parents[3] / "logos-test-modules" / "conformance"),
]


def _load_shared() -> dict:
    for base in _SEARCH:
        if not base:
            continue
        p = Path(base) / "cases.json"
        if p.is_file():
            return json.loads(p.read_text())
    pytest.skip("shared conformance table not resolvable from here")


def _materialize(v):
    """Tagged bytes -> bytes, so the two tables' values compare directly."""
    if isinstance(v, dict) and set(v) == {"_bytes"} and isinstance(v["_bytes"], str):
        raw = v["_bytes"]
        if raw == "__ALL_BYTES__":
            return bytes(range(256))
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    if isinstance(v, dict):
        return {k: _materialize(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_materialize(x) for x in v]
    return v


PROVIDER = "test_fullapi_cpp"


def test_every_inline_method_case_exists_in_the_shared_table():
    shared = _load_shared()
    by_call: dict[tuple, list] = {}
    for c in shared["cases"]:
        if c.get("raw"):
            continue  # adversarial cases deliberately bypass materialization
        key = (c["method"], json.dumps(_jsonify(_materialize(c.get("args", [])))))
        expect = c["expect"] if "expect" in c else c.get("expect_by_provider", {}).get(PROVIDER)
        by_call.setdefault(key, []).append(_materialize(expect))

    missing = []
    for method, args, expected in FULLAPI_METHOD_CASES:
        if expected is None:
            continue  # "dispatches cleanly, value unchecked" pins nothing
        key = (method, json.dumps(_jsonify(list(args))))
        if key not in by_call:
            missing.append(f"{method}{args!r} — no matching case in cases.json")
        elif not any(_same(e, expected) for e in by_call[key]):
            missing.append(
                f"{method}{args!r} — inline expects {expected!r}, "
                f"shared table has {by_call[key]!r}")
    assert not missing, "inline and shared tables disagree:\n  " + "\n  ".join(missing)


def test_every_inline_event_case_exists_in_the_shared_table():
    shared = _load_shared()
    have = {(e["event"], e["fire"], json.dumps(_jsonify(_materialize(e["value"]))))
            for e in shared["events"]}
    missing = [
        f"{event}/{fire}({value!r})"
        for event, fire, value in FULLAPI_EVENT_CASES
        if (event, fire, json.dumps(_jsonify(value))) not in have
    ]
    assert not missing, (
        "events pinned inline but absent from the shared table:\n  " + "\n  ".join(missing))


def _jsonify(v):
    if isinstance(v, (bytes, bytearray)):
        return {"__b__": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, dict):
        return {k: _jsonify(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonify(x) for x in v]
    return v


def _same(a, b) -> bool:
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b
