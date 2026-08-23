"""`--proxy-consumer`'s spec, including the call-mode probe.

The probe is the only part of a consumer point that has to know which CONTRACT
is being replayed. Its job is to prove the selected generated wrapper table
actually ran — `useCallMode("async")` returning true says the flag is set, not
that the async body served anything — and it does that by making one known-good
call and reading `lastCallStatus()` back.

`echoInt:[1]` is the default and only `full_api` has it. `full_api_ext` has no
method taking a bare scalar at all, and its one zero-parameter method silently
ignores an extra argument (known-ext.json B-arity-overflow), so `whoAmI(1)`
would "work" only for as long as that defect lives. Hence the fifth field.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_DRIVER = Path(__file__).resolve().parents[2] / "conformance" / "run_matrix.py"


def _load():
    spec = importlib.util.spec_from_file_location("_run_matrix_pc", _DRIVER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_run_matrix_pc"] = mod
    spec.loader.exec_module(mod)
    return mod


run_matrix = _load()
parse = run_matrix.parse_proxy_consumer


def test_three_fields_is_a_proxy_with_no_call_mode():
    c = parse("p=mod=/dir")
    assert (c.label, c.module, c.dirs, c.call_mode) == ("p", "mod", ["/dir"], None)
    assert c.is_proxy


def test_the_default_probe_is_full_apis_and_is_unchanged():
    """Every existing invocation passes four fields; none of them may move."""
    c = parse("qtproxy-sync=test_fullapi_qtproxy=/dir=sync")
    assert c.call_mode == "sync"
    assert (c.probe_method, c.probe_args) == ("echoInt", [1])


def test_a_fifth_field_names_the_probe_and_its_arguments():
    c = parse('extqtproxy-sync=test_fullapi_ext_qtproxy=/dir=sync=echoStringMap:[{"k":"v"}]')
    assert c.probe_method == "echoStringMap"
    assert c.probe_args == [{"k": "v"}]


def test_a_probe_may_take_no_arguments():
    c = parse("p=mod=/dir=async=whoAmI")
    assert (c.probe_method, c.probe_args) == ("whoAmI", [])


def test_probe_arguments_use_the_tables_tagged_bytes_form():
    """One spelling for bytes across the table and the probe, not two."""
    c = parse('p=mod=/dir=sync=echoBlob:[{"payload":{"_bytes":"aGk"}}]')
    assert c.probe_args == [{"payload": b"hi"}]


@pytest.mark.parametrize("spec", [
    "p=mod",                                  # too few
    "p=mod=/dir=sync=probe=extra",            # too many
    "p=mod=/dir=maybe",                       # not a call mode
    "p=mod=/dir=sync=:[1]",                   # no method name
    "p=mod=/dir=sync=echoInt:notjson",        # not JSON
    'p=mod=/dir=sync=echoInt:{"a":1}',        # JSON, but not an argument LIST
])
def test_a_malformed_spec_is_refused_rather_than_defaulted(spec):
    """Defaulting is the dangerous branch: a mis-spelled probe that fell back to
    `echoInt` would silently stop proving the call mode on any contract that
    does not have it, and the run would still be green."""
    with pytest.raises(ValueError):
        parse(spec)
