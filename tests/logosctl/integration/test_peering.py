"""Two daemons on this machine: the exporter serves `test_fullapi_cpp`, and the
importer calls it through an import as if it were local.

The shared full_api tables replay through the import, so every type crosses
the facade and the `tls_tcp` session both ways, as a call and as an event.
Then the exporter's policy and a restart of the exporter are exercised.
Everything runs twice: with the importer's facade in a host process of its
own, and with a single-process importer, whose runtime runs peering and the
facade itself.

It needs the plain build of the module (LOGOSCTL_PLAIN_MODULES_DIR): a
Qt-hosted module cannot be exported.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from logosctl import LogosctlError, PeeredDaemons

from ..._fullapi_module_cases import FULLAPI_EVENT_CASES, FULLAPI_METHOD_CASES

MODULE = "test_fullapi_cpp"

# The importer's placement policy, and where its facade then runs.
PLACEMENTS = {
    "apart": (None, "subprocess"),
    "single_process": ({"single_process": True}, "inproc"),
}


@pytest.fixture(scope="module", params=list(PLACEMENTS))
def peered(request, logosctl_bin, logosctl_plain_modules_dir):
    with PeeredDaemons(logosctl_plain_modules_dir, [MODULE], binary=logosctl_bin,
                       importer_placement=PLACEMENTS[request.param][0]) as pair:
        pair.placement = request.param
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


def test_the_importer_runs_the_facade_where_its_policy_says(peered, importer):
    """A single-process importer runs peering_module and the facade itself;
    otherwise each has a host process of its own."""
    where = PLACEMENTS[peered.placement][1]
    assert importer.module_info(MODULE)["placement"] == where
    assert importer.module_info("peering_module")["placement"] == where


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


def test_a_changed_import_rule_restarts_its_facade(peered, importer):
    """Narrowing who may call an import restarts its facade (in the importer's
    own process when it is single-process), and the new rule holds at once;
    widening it again lets the caller back in."""
    peered.import_module(MODULE, allowed_callers=["someone_else"], wait=False)
    try:
        # Past the restart ("not loaded" for a moment), the new facade refuses.
        _call_until(importer, lambda o: isinstance(o, LogosctlError)
                    and o.detail_code == "dispatch_failed", "narrowed")
    finally:
        peered.import_module(MODULE, wait=False)
    assert _call_until(importer, lambda o: o == "widened", "widened") == "widened"
    peered.wait_for_import(MODULE)


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


def test_an_unloaded_export_is_gone_until_it_loads_again(peered, importer):
    """Unloading the exported module tells the exporter's peering at once (it
    used to hear only of a crash, and kept offering the dead port); the import
    comes back on the next load's port."""
    exporter = peered.exporter_client()
    exporter.unload_module(MODULE)
    try:
        deadline = time.monotonic() + 5.0
        while (shared := exporter.peer("exports")[MODULE])["loaded"]:
            assert time.monotonic() < deadline, f"still exported after unload: {shared}"
            time.sleep(0.1)
        assert shared["port"] == 0, shared
    finally:
        exporter.load_module(MODULE)
    deadline = time.monotonic() + 20.0
    while not exporter.peer("exports")[MODULE]["port"]:
        assert time.monotonic() < deadline, "the reloaded export never listened"
        time.sleep(0.1)
    assert _call_until(importer, lambda o: o == "reloaded", "reloaded") == "reloaded"


CONCURRENT = "test_concurrency_cpp"


@pytest.fixture(scope="module", params=list(PLACEMENTS))
def peered_multi(request, logosctl_bin, logosctl_concurrency_modules_dir):
    with PeeredDaemons(logosctl_concurrency_modules_dir, [CONCURRENT], binary=logosctl_bin,
                       events=False, importer_placement=PLACEMENTS[request.param][0]) as pair:
        yield pair


def test_an_import_keeps_a_multi_provider_parallel(peered_multi):
    """The facade mirrors the provider's `concurrency: multi`, so calls made at
    once through the import overlap on the exporter (a single facade: 1)."""
    importer = peered_multi.importer_client()
    exporter = peered_multi.exporter_client()
    exporter.call(CONCURRENT, "reset")
    calls = [threading.Thread(target=importer.call, args=(CONCURRENT, "sleepMs", 1500),
                              kwargs={"timeout": 30.0}) for _ in range(4)]
    for t in calls:
        t.start()
    for t in calls:
        t.join()
    assert exporter.call(CONCURRENT, "peakInFlight") >= 2
