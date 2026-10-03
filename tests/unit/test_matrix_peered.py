"""A peered cell (`run_matrix.py --peered`): the provider measured through an
import, exported by one logosctl daemon and called on another.

The facade answers every upstream failure as dispatch_failed and names the
provider's own class after `remote/` in the message, so the driver reads that
class back and the cell compares with the same provider measured locally.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from logoscore.errors import LogoscoreError
from logosctl.errors import LogosctlError, MethodError

_DRIVER = Path(__file__).resolve().parents[2] / "conformance" / "run_matrix.py"


@pytest.fixture(scope="module")
def rm():
    spec = importlib.util.spec_from_file_location("_run_matrix_peered", _DRIVER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_run_matrix_peered"] = mod
    spec.loader.exec_module(mod)
    return mod


def failed(message: str, detail: str) -> MethodError:
    return MethodError(message, code="METHOD_FAILED", detail_code=detail)


def test_a_peered_label_names_its_provider(rm):
    assert rm.base_provider("test_fullapi_cpp" + rm.PEERED) == "test_fullapi_cpp"
    assert rm.base_provider("test_fullapi_cpp") == "test_fullapi_cpp"


def test_the_providers_class_is_read_back_from_the_facade(rm):
    through = failed("Call to m.meth failed (dispatch_failed: remote/invalid_args: "
                     "expected 1 arguments, got 0).", "dispatch_failed")
    assert rm.remote_error_code_of(through) == "invalid_args"
    # No remote class, or not the facade's code: the class stays as reported.
    local = failed("Call to m.meth failed (dispatch_failed: bad value).", "dispatch_failed")
    assert rm.remote_error_code_of(local) == "dispatch_failed"
    other = failed("Call to m.meth failed (object_unavailable: remote/x: y).", "object_unavailable")
    assert rm.remote_error_code_of(other) == "object_unavailable"


def test_either_clients_errors_are_caught(rm):
    assert set(rm.client_errors()) == {LogoscoreError, LogosctlError}
