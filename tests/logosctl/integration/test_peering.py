"""Two daemons on this machine: the exporter serves `test_fullapi_cpp`, and the
importer calls it through an import as if it were local.

The shared full_api tables replay through the import, so every type crosses
the facade and the `tls_tcp` session both ways, as a call and as an event.
Then the exporter's policy and a restart of the exporter are exercised.

Peering is between the two daemons, not this client, so only `--transport
local` runs it. It needs the plain build of the module
(LOGOSCTL_PLAIN_MODULES_DIR): a Qt-hosted module cannot be exported.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from logosctl import LogosctlError, PeeredDaemons

from ..._fullapi_module_cases import FULLAPI_EVENT_CASES, FULLAPI_METHOD_CASES

MODULE = "test_fullapi_cpp"


@pytest.fixture(scope="module")
def peered(logosctl_bin, logosctl_plain_modules_dir, transport):
    if transport != "local":
        pytest.skip("peering does not depend on how the client reaches a daemon")
    with PeeredDaemons(logosctl_plain_modules_dir, [MODULE], binary=logosctl_bin) as pair:
        yield pair


@pytest.fixture(scope="module")
def importer(peered):
    return peered.importer_client()


def _call_until(client, predicate, *args, deadline_s: float = 20.0):
    """Call MODULE.echoString until `predicate(outcome)` holds, where outcome is
    the value or the LogosctlError. Policy changes reach live routes a moment
    after `policy set` returns."""
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            outcome = client.call(MODULE, "echoString", *args, timeout=20.0)
        except LogosctlError as e:
            outcome = e
        if predicate(outcome):
            return outcome
        assert time.monotonic() < deadline, f"still {outcome!r} after {deadline_s}s"
        time.sleep(0.5)


def test_the_import_carries_the_exporters_interface(peered, importer):
    """The importer has no copy of the module; its facade serves the
    exporter's methods and events."""
    def surface(info):
        return [sorted(json.dumps(x, sort_keys=True) for x in info[k])
                for k in ("methods", "events")]

    assert peered.import_state(MODULE)["state"] == "ready"
    assert surface(importer.module_info(MODULE)) == \
        surface(peered.exporter_client().module_info(MODULE))


def test_the_exporter_answers(peered, importer):
    assert importer.call(MODULE, "echoString", "across") == "across"
    served = [r for r in peered.served_routes() if r["target"] == MODULE]
    assert any(r["peer"] == peered.importer_id and r["consumer"] == "@op:auto"
               for r in served), served


@pytest.mark.parametrize(
    "method,args,expected", FULLAPI_METHOD_CASES,
    ids=[f"{m}{args!r}" for m, args, _ in FULLAPI_METHOD_CASES],
)
def test_method_through_the_import(importer, method, args, expected):
    result = importer.call(MODULE, method, *args)
    if expected is not None:
        assert result == expected


@pytest.mark.parametrize(
    "event,fire,value", FULLAPI_EVENT_CASES,
    ids=[f"{c[0]}-{i}" for i, c in enumerate(FULLAPI_EVENT_CASES)],
)
def test_event_through_the_import(importer, event, fire, value):
    """The exporter emits, the import re-emits on the importer. Re-fire until
    the watch is live: the subscription starts in a subprocess."""
    received: list[dict] = []
    got = threading.Event()

    def on_event(e: dict) -> None:
        received.append(e)
        got.set()

    with importer.on_event(MODULE, event, on_event):
        deadline = time.monotonic() + 20.0
        while not got.is_set():
            assert importer.call(MODULE, fire, value) is True
            if got.wait(timeout=1.0):
                break
            assert time.monotonic() < deadline, f"{event} never reached the importer"
    evt = received[0]
    assert evt["event"] == event and evt["module"] == MODULE
    payload = evt["data"]["arg0"]
    if isinstance(value, float) or (isinstance(value, list) and value
                                    and isinstance(value[0], float)):
        assert payload == pytest.approx(value)
    else:
        assert payload == value


def test_the_exporters_policy_decides(peered, importer):
    """An empty policy revokes the importer's routes, and the import says so
    at once; granting it again brings the import back without re-importing."""
    granted = {f"{peered.importer_id}/*": ["*"]}
    peered.set_policy({})
    try:
        refused = _call_until(importer, lambda o: isinstance(o, LogosctlError), "denied")
        assert refused.detail_code == "dispatch_failed"
        assert not any(r["peer"] == peered.importer_id for r in peered.served_routes())
        # Well inside the facade's 15 s health check.
        state = peered.wait_for_import(MODULE, "error", timeout=5.0)
        assert "grants no route" in state["reason"], state
    finally:
        peered.set_policy(granted)
    peered.wait_for_import(MODULE)
    assert importer.call(MODULE, "echoString", "granted") == "granted"


def test_the_import_survives_an_exporter_restart(peered, importer):
    port = peered.exporter_client().peer("status")["control"]["port"]
    peered.stop_exporter()
    with pytest.raises(LogosctlError):
        importer.call(MODULE, "echoString", "while down", timeout=20.0)
    peered.wait_for_import(MODULE, "error", "connecting")

    peered.start_exporter()
    assert peered.exporter_client().peer("status")["control"]["port"] == port
    peered.wait_for_import(MODULE)
    assert importer.call(MODULE, "echoString", "back") == "back"
