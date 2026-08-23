"""The failure CLASS under the envelope verdict — the logosctl twin.

DELIBERATE DUPLICATE of tests/unit/test_error_detail_code.py, for the reason
the whole `logosctl` tree is duplicated (see test_client_with_fake.py's header).
The two clients compile against the SAME daemon envelope
(logos-logoscore-cli src/core_service/call_envelope.cpp), so a change that
teaches one of them to read `error.code` and leaves the other reading only
`code` produces two clients that disagree about what happened — which is the
exact failure mode this repo keeps a second copy of the tests to catch.
"""
from __future__ import annotations

import json

import pytest

from logosctl._proc import _error_codes_from_stdout
from logosctl import LogosctlClient
from logosctl.errors import MethodError, from_exit_code

from .test_client_with_fake import Recorder, rec  # noqa: F401  (fixture)

FAILED = {
    "status": "error",
    "code": "METHOD_FAILED",
    "message": "Call to m.meth failed (object_unavailable: not there).",
    "error": {"code": "object_unavailable", "message": "not there",
              "origin": "m"},
}


def test_both_codes_are_read():
    assert _error_codes_from_stdout(json.dumps(FAILED)) == (
        "METHOD_FAILED", "object_unavailable")


@pytest.mark.parametrize("envelope, expected", [
    ({"status": "error", "code": "METHOD_NOT_FOUND", "message": "x"},
     ("METHOD_NOT_FOUND", None)),
    ({"status": "error", "code": "METHOD_FAILED", "message": "x"},
     ("METHOD_FAILED", None)),
    ({"status": "error", "code": "METHOD_FAILED", "error": {"code": 7}},
     ("METHOD_FAILED", None)),
    ({"status": "ok", "result": {"code": "invalid_args"}}, (None, None)),
])
def test_shapes_that_must_not_yield_a_detail(envelope, expected):
    assert _error_codes_from_stdout(json.dumps(envelope)) == expected


def test_detail_survives_from_exit_code():
    exc = from_exit_code(4, "boom", error_code="METHOD_FAILED",
                         detail_error_code="timeout")
    assert (exc.code, exc.detail_code) == ("METHOD_FAILED", "timeout")
    assert from_exit_code(3, "x").detail_code is None


def test_call_raises_with_the_class(rec: Recorder):  # noqa: F811
    rec.respond(returncode=4, stdout=json.dumps(FAILED))
    with pytest.raises(MethodError) as e:
        LogosctlClient().call("m", "meth")
    assert e.value.code == "METHOD_FAILED"
    assert e.value.detail_code == "object_unavailable"
