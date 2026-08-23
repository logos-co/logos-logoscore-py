"""The envelope carries TWO codes, and only one of them names a failure CLASS.

    {"status":"error","code":"METHOD_FAILED",
     "error":{"code":"invalid_args","message":...,"origin":...}}

`code` is the verdict — METHOD_FAILED for a module that is not there, for a
call that timed out, for a provider that refused the argument values and for
one that refused their count. `error.code` is which of those happened. This
client read the first and dropped the second, so every caller saw one token and
no caller could act on the difference.

Measured consequence, which is why this is a test and not a preference: the
conformance matrix asserts `{"__error__": "dispatch_failed"}` on 13 cases. Those
rejections used to arrive as a successful call returning the 3-key object and
the driver folded them itself; since logos-logoscore-cli#99 the daemon folds
them and the envelope verdict is METHOD_FAILED. With the detail dropped, all 44
of those cells fail on the next relock — not because anything regressed, but
because the client stopped being able to say what happened.
"""
from __future__ import annotations

import json

import pytest

from logoscore._proc import _error_codes_from_stdout
from logoscore.client import LogoscoreClient
from logoscore.errors import MethodError, from_exit_code

from .test_client_with_fake import Recorder, rec  # noqa: F401  (fixture)

FAILED = {
    "status": "error",
    "code": "METHOD_FAILED",
    "message": "Call to m.meth failed (invalid_args: expected 1 arguments, got 0).",
    "error": {"code": "invalid_args",
              "message": "expected 1 arguments, got 0",
              "origin": "m"},
}


def test_both_codes_are_read():
    assert _error_codes_from_stdout(json.dumps(FAILED)) == (
        "METHOD_FAILED", "invalid_args")


@pytest.mark.parametrize("envelope, expected", [
    # METHOD_NOT_FOUND carries no `error` object: the daemon did not learn this
    # from the transport, it derived it from the module's own method list.
    ({"status": "error", "code": "METHOD_NOT_FOUND", "message": "x",
      "available_methods": ["a"]}, ("METHOD_NOT_FOUND", None)),
    # Every daemon older than #99 reported the verdict alone.
    ({"status": "error", "code": "METHOD_FAILED", "message": "x"},
     ("METHOD_FAILED", None)),
    # A non-string, or a non-object, must not become a code.
    ({"status": "error", "code": "METHOD_FAILED", "error": {"code": 7}},
     ("METHOD_FAILED", None)),
    ({"status": "error", "code": "METHOD_FAILED", "error": "invalid_args"},
     ("METHOD_FAILED", None)),
    # A SUCCESSFUL envelope whose RESULT happens to be a map with a `code` key
    # is data, not an error — the status gate is what keeps it that way.
    ({"status": "ok", "result": {"code": "invalid_args"}}, (None, None)),
])
def test_shapes_that_must_not_yield_a_detail(envelope, expected):
    assert _error_codes_from_stdout(json.dumps(envelope)) == expected


def test_unparseable_or_empty_stdout():
    assert _error_codes_from_stdout("") == (None, None)
    assert _error_codes_from_stdout("not json") == (None, None)


def test_detail_survives_from_exit_code():
    exc = from_exit_code(4, "boom", stderr="e", error_code="METHOD_FAILED",
                         detail_error_code="object_unavailable")
    assert exc.code == "METHOD_FAILED"
    assert exc.detail_code == "object_unavailable"


def test_detail_defaults_to_none_so_old_callers_still_construct():
    assert from_exit_code(3, "x").detail_code is None


def test_call_raises_with_the_class_on_a_nonzero_exit(rec: Recorder):  # noqa: F811
    rec.respond(returncode=4, stdout=json.dumps(FAILED))
    with pytest.raises(MethodError) as e:
        LogoscoreClient().call("m", "meth")
    assert e.value.code == "METHOD_FAILED"
    assert e.value.detail_code == "invalid_args"


def test_call_raises_with_the_class_on_an_in_band_error_envelope(rec: Recorder):  # noqa: F811
    """The exit-0 path exists too, and dropped the detail in the same way."""
    rec.respond(stdout=json.dumps(FAILED))
    with pytest.raises(MethodError) as e:
        LogoscoreClient().call("m", "meth")
    assert e.value.detail_code == "invalid_args"


def test_the_classes_a_caller_has_to_separate_are_separable():
    """The point of the whole change, as one assertion.

    Four different events, one envelope verdict. Anything reading `code` alone
    sees a single string four times.
    """
    def detail(cls):
        env = dict(FAILED, error={"code": cls, "message": "m", "origin": "o"})
        return _error_codes_from_stdout(json.dumps(env))
    seen = [detail(c) for c in
            ("object_unavailable", "timeout", "dispatch_failed", "invalid_args")]
    assert {v for v, _ in seen} == {"METHOD_FAILED"}
    assert len({d for _, d in seen}) == 4
