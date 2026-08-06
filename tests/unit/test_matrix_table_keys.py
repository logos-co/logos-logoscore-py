"""An expectation the driver does not read must fail the run, not vanish.

`validate_table` exists because the silent path is the dangerous one. A case
carrying an expectation under a key `expectation()` never looks at has no
expectation at all: `have_want` comes back False, the cell is filed
`status: "skip"`, and `skip` is not in the failing-status set. The case sits in
the table looking deliberate, counts toward coverage, and asserts nothing.

The concrete instance: `cases.json`'s schema comment documented `expect_error`
as the way to say "the call must be rejected". No such key was ever implemented
— rejections are `{"expect": {"__error__": "dispatch_failed"}}` — so anyone
following the documentation would have written a case that measured nothing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_DRIVER = Path(__file__).resolve().parents[2] / "conformance" / "run_matrix.py"


def _load():
    spec = importlib.util.spec_from_file_location("_run_matrix", _DRIVER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_run_matrix"] = mod
    spec.loader.exec_module(mod)
    return mod


run_matrix = _load()


def _table(**over):
    t = {
        "schema": 1,
        "contract": "full_api",
        "comment": ["x"],
        "providers": ["p"],
        "cases": [{"id": "c/1", "type": "tstr", "position": "method_arg",
                   "method": "echoString", "args": ["a"], "expect": "a",
                   "tags": ["nominal"]}],
        "events": [{"id": "e/1", "type": "tstr", "position": "event_param",
                    "event": "stringEvent", "fire": "fireStringEvent",
                    "value": "hello"}],
    }
    t.update(over)
    return t


def test_a_clean_table_validates():
    run_matrix.validate_table(_table(), "cases.json")  # must not raise


def test_expect_error_is_rejected_rather_than_skipped():
    """The exact trap: a key the schema comment documented and nothing read."""
    bad = _table(cases=[{"id": "c/rejects", "type": "tstr",
                         "position": "method_arg", "method": "echoString",
                         "args": [1], "expect_error": "dispatch_failed",
                         "tags": ["hostile"]}])
    with pytest.raises(SystemExit) as e:
        run_matrix.validate_table(bad, "cases.json")
    msg = str(e.value)
    assert "expect_error" in msg
    assert "c/rejects" in msg
    # and it says what to write instead
    assert "__error__" in msg


def test_every_offender_is_named_at_once():
    bad = _table(cases=[
        {"id": "c/1", "type": "tstr", "position": "method_arg",
         "method": "m", "args": [], "expect": 1, "typo_one": 1},
        {"id": "c/2", "type": "tstr", "position": "method_arg",
         "method": "m", "args": [], "expect": 1, "typo_two": 1},
    ])
    with pytest.raises(SystemExit) as e:
        run_matrix.validate_table(bad, "cases.json")
    msg = str(e.value)
    assert "typo_one" in msg and "typo_two" in msg, "must not stop at the first"
    assert "2 unknown key(s)" in msg


def test_events_are_checked_too():
    bad = _table(events=[{"id": "e/1", "type": "tstr",
                          "position": "event_param", "event": "e",
                          "fire": "f", "payload": "hello"}])
    with pytest.raises(SystemExit) as e:
        run_matrix.validate_table(bad, "cases.json")
    assert "payload" in str(e.value)


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(SystemExit) as e:
        run_matrix.validate_table(_table(providerz=["p"]), "cases.json")
    assert "providerz" in str(e.value)


@pytest.mark.parametrize("path", ["cases.json", "ext-cases.json"])
def test_both_shipped_tables_validate(path):
    """The allowlist is derived from the real tables; it must accept them."""
    import json
    search = [
        Path(__file__).resolve().parents[3] / "logos-test-modules" / "conformance",
    ]
    import os
    if os.environ.get("LOGOS_CONFORMANCE_DIR"):
        search.insert(0, Path(os.environ["LOGOS_CONFORMANCE_DIR"]))
    for base in search:
        p = base / path
        if p.is_file():
            run_matrix.validate_table(json.loads(p.read_text()), str(p))
            return
    pytest.skip("shared conformance table not resolvable from here")
