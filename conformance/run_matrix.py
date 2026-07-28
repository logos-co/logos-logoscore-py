#!/usr/bin/env python3
"""Run the LIDL conformance matrix and report it as coordinates.

The matrix answers one question per cell: does a value of LIDL type T, in
position P, survive provider R -> consumer K intact? A red cell is printed as
`[uint]/method_return/test_fullapi_rust/py` — never as an aggregate token, so a
failure names itself and one provider cannot satisfy an assertion on behalf of
the other.

Three things keep it honest:

  * every case runs against BOTH providers, and the two results are compared to
    each other independently of `expect` — the cheapest possible detector for
    the next divergence, and it needs nobody to know the right answer first;
  * a known-broken cell is an `xfail` REGISTRY entry, and a cell that starts
    passing is an `xpass`, which fails the run so the registry gets updated;
  * coverage is computed from the contract: every (type, position) the .lidl
    declares must have at least one case, or the run fails. That is the
    structural guard against a 31-method contract with 6 assertions.

Usage:
    run_matrix.py --cpp-modules DIR --rust-modules DIR [--logoscore BIN]
                  [--contract full_api.lidl] [--jsonl out.jsonl] [--quiet]

This is the `py` driver. The case table and the xfail registry live with the
PROVIDERS, in logos-test-modules/conformance/ — the driver lives here because
it uses this package's client, and logoscore-py already depends on
logos-test-modules (the reverse would be a cycle). Other consumers (the C++,
Rust and QML proxies) replay the same cases.json.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def norm_type(t: str) -> str:
    """One spelling per type. `{tstr: any}` in the contract and `{tstr:any}` in
    the table are the same cell; without this every map type reads as
    uncovered."""
    return re.sub(r"\s+", "", t)

# ── canonical tagged bytes ──────────────────────────────────────────────────
# The one wire form for a byte string, at every depth. The table carries it
# literally so the file stays language-neutral; a driver converts to and from
# its native byte type at the boundary.


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


ALL_BYTES = bytes(range(256))


def materialize(v, raw: bool = False):
    """Table value -> native Python value (tagged bytes become `bytes`).

    `raw=True` passes the literal JSON through untouched. That is the only way
    to express an adversarial case like "a USER map that happens to have a
    single `_bytes` key must come back as a MAP": if the driver materialized
    both the argument and the expectation into bytes, the cell would compare
    bytes to bytes and pass no matter what the system did.
    """
    if raw:
        return v
    if isinstance(v, dict):
        if set(v) == {"_bytes"} and isinstance(v["_bytes"], str):
            raw = v["_bytes"]
            return ALL_BYTES if raw == "__ALL_BYTES__" else b64url_decode(raw)
        return {k: materialize(x) for k, x in v.items()}
    if isinstance(v, list):
        return [materialize(x) for x in v]
    return v


def jsonable(v):
    """Native value -> something json.dumps can print, for the report."""
    if isinstance(v, (bytes, bytearray)):
        return {"_bytes": b64url_encode(bytes(v))}
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [jsonable(x) for x in v]
    return v


def same(a, b) -> bool:
    """Value equality that does NOT let 1 == True or 1 == 1.0 pass.

    A matrix whose comparison is `==` cannot see an int degrading to a float,
    which is most of what it exists to catch.
    """
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (bytes, bytearray)) or isinstance(b, (bytes, bytearray)):
        return bytes(a) == bytes(b) if type(a) is type(b) else False
    if isinstance(a, float) != isinstance(b, float):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


# ── the contract, for coverage ──────────────────────────────────────────────
# A small reader rather than a dependency on the C++ front end: the matrix must
# be able to run anywhere the providers run.

_METHOD_RE = re.compile(r"^\s*method\s+(\w+)\s*\((.*?)\)\s*->\s*(.+?)\s*$")
_EVENT_RE = re.compile(r"^\s*event\s+(\w+)\s*\((.*?)\)\s*$")


def _split_params(text: str) -> list[str]:
    """Split a parameter list on top-level commas ({tstr: any} has one too)."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [p.strip() for p in out if p.strip()]


def contract_cells(lidl_path: Path) -> set[tuple[str, str]]:
    """Every (type, position) the contract declares."""
    cells: set[tuple[str, str]] = set()
    for line in lidl_path.read_text().splitlines():
        m = _METHOD_RE.match(line)
        if m:
            _, params, ret = m.groups()
            parts = _split_params(params)
            for k, p in enumerate(parts):
                if ":" in p:
                    # An argument in a multi-parameter method is a DIFFERENT
                    # cell from a sole argument: the sole case cannot catch a
                    # generator that mixes up positional slots.
                    pos = "method_arg" if len(parts) == 1 else f"method_arg@{k}"
                    cells.add((norm_type(p.split(":", 1)[1]), pos))
            cells.add((norm_type(ret), "method_return"))
            continue
        m = _EVENT_RE.match(line)
        if m:
            parts = _split_params(m.group(2))
            for k, p in enumerate(parts):
                if ":" in p:
                    pos = "event_param" if len(parts) == 1 else f"event_param@{k}"
                    cells.add((norm_type(p.split(":", 1)[1]), pos))
    return cells


# ── the driver ──────────────────────────────────────────────────────────────


DISPATCH_ERROR_KEYS = {"code", "message"}


class Result:
    __slots__ = ("value", "error")

    def __init__(self, value=None, error=None):
        # A provider that rejects an argument answers with the structured
        # dispatch error as its RESULT ({"code": "dispatch_failed", ...}), not
        # with a transport error — so a driver that only watches for raised
        # exceptions records a rejection as a successful call returning a dict.
        if error is None and isinstance(value, dict) \
                and DISPATCH_ERROR_KEYS <= set(value) and "status" not in value:
            self.value, self.error = None, str(value.get("code"))
            return
        self.value, self.error = value, error

    def as_report(self):
        return {"__error__": self.error} if self.error else jsonable(self.value)


def expectation(case: dict, provider: str):
    by = case.get("expect_by_provider")
    if by is not None:
        if provider not in by:
            return None, False
        return by[provider], True
    if "expect" in case:
        return case["expect"], True
    return None, False


def matches(got: Result, want, raw: bool = False) -> bool:
    if isinstance(want, dict) and set(want) == {"__error__"}:
        return got.error is not None and want["__error__"] in got.error
    if got.error is not None:
        return False
    return same(got.value, materialize(want, raw))


def run_methods(client, module: str, cases: list, timeout: float):
    from logoscore.errors import LogoscoreError, MethodError

    out: dict[str, Result] = {}
    for case in cases:
        raw = case.get("raw", False)
        args = [materialize(a, raw) for a in case.get("args", [])]
        t = case.get("timeout_ms", timeout * 1000) / 1000.0
        try:
            out[case["id"]] = Result(value=client.call(module, case["method"], *args, timeout=t))
        except MethodError as e:
            out[case["id"]] = Result(error=e.code or "MethodError")
        except LogoscoreError as e:
            out[case["id"]] = Result(error=type(e).__name__)
        except Exception as e:
            # An adversarial payload can HANG the call rather than fail it (the
            # pending-call sentinel does exactly that), which surfaces as a
            # subprocess timeout. A cell that wedges the driver would take the
            # whole matrix with it, so record it as the failure it is.
            out[case["id"]] = Result(error=type(e).__name__)
    return out


def run_events(client, module: str, events: list, timeout: float):
    from logoscore.errors import LogoscoreError, MethodError

    out: dict[str, Result] = {}
    for ev in events:
        raw = ev.get("raw", False)
        values = ([materialize(v, raw) for v in ev["values"]] if "values" in ev
                  else [materialize(ev["value"], raw)])
        try:
            out[ev["id"]] = Result(value=capture_event(
                client, module, ev["event"], ev["fire"], values, timeout))
        except MethodError as e:
            out[ev["id"]] = Result(error=e.code or "MethodError")
        except LogoscoreError as e:
            out[ev["id"]] = Result(error=type(e).__name__)
        except Exception as e:
            out[ev["id"]] = Result(error=type(e).__name__)
    return out


def capture_event(client, module: str, event: str, fire: str, values: list, timeout: float):
    """Subscribe, fire, wait — re-firing until the event lands.

    The watcher subscribes on a background subprocess, so "watch is live" and
    "we fire" race. A single fire that lands before the subscription is up is
    lost and no later wait recovers it; the `fire<X>Event` triggers are
    idempotent emits, so re-firing on a short cadence closes the race without
    hard-coding a settle time. (Same approach as the logoscore-py suite, which
    is where this race was worked out.)
    """
    import threading
    import time

    received: list = []
    got = threading.Event()

    def on_event(e: dict) -> None:
        received.append(e)
        got.set()

    with client.on_event(module, event, on_event):
        deadline = time.monotonic() + timeout
        while True:
            client.call(module, fire, *values, timeout=timeout)
            if got.wait(timeout=1.0):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"event {event} not received within {timeout}s")

    payload = received[0]
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict) and "arg0" in data:
        # A multi-parameter event arrives as {arg0, arg1, ...}. Return the
        # ordered list so argument POSITION is part of what is compared — a
        # single-arg event still reduces to its one value.
        ordered = [data[f"arg{i}"] for i in range(len(data)) if f"arg{i}" in data]
        return ordered[0] if len(ordered) == 1 else ordered
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    # The two full_api providers keep dedicated flags (the flake check and every
    # existing invocation use them); `--modules NAME=DIR` is the general form, so
    # a table with a different provider set — full_api_ext has ONE provider —
    # runs through the same driver instead of a second one.
    ap.add_argument("--cpp-modules")
    ap.add_argument("--rust-modules")
    ap.add_argument("--modules", action="append", default=[],
                    metavar="NAME=DIR", help="provider module dir; repeatable")
    ap.add_argument("--logoscore", default=os.environ.get("LOGOSCORE_BIN", "logoscore"))
    # The table lives in logos-test-modules/conformance/; the flake points at
    # it through the store path of that input.
    conformance = os.environ.get("LOGOS_CONFORMANCE_DIR", str(HERE))
    ap.add_argument("--cases", default=str(Path(conformance) / "cases.json"))
    ap.add_argument("--known", default=str(Path(conformance) / "known.json"))
    ap.add_argument("--contract", default=None,
                    help="full_api.lidl; coverage is checked against it when given")
    ap.add_argument("--jsonl", default=None, help="write one JSON object per cell")
    ap.add_argument("--consumer", default="py", help="label for this driver in the report")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    from logoscore import LogoscoreDaemon

    table = json.loads(Path(args.cases).read_text())
    known = json.loads(Path(args.known).read_text())
    cases, events = table["cases"], table["events"]

    xfail: dict[tuple[str, str], str] = {}
    for entry in known["xfail"]:
        # `providers` is the entry-wide set; `per_case_providers` narrows an
        # individual case to the providers that actually fail it. Without that
        # narrowing a defect present on one provider gets registered for both,
        # and the OTHER provider — which passes — is then reported as xpass,
        # i.e. the registry manufactures a failure.
        narrowed = entry.get("per_case_providers", {})
        for cid in entry["cases"]:
            for prov in narrowed.get(cid, entry["providers"]):
                xfail[(cid, prov)] = entry["id"]

    modules: dict[str, str] = {}
    if args.cpp_modules:
        modules["test_fullapi_cpp"] = args.cpp_modules
    if args.rust_modules:
        modules["test_fullapi_rust"] = args.rust_modules
    for spec in args.modules:
        name, _, path = spec.partition("=")
        if not path:
            print(f"--modules expects NAME=DIR, got {spec!r}")
            return 2
        modules[name] = path
    # The table names the providers it describes; run the intersection so a
    # driver invoked with more module dirs than the table needs does not invent
    # cells, and one invoked with fewer fails loudly below rather than silently
    # reporting a smaller matrix.
    declared = table.get("providers")
    if declared:
        missing = [p for p in declared if p not in modules]
        if missing:
            print(f"table declares providers with no module dir given: {missing}")
            return 2
        modules = {k: v for k, v in modules.items() if k in declared}
    if not modules:
        print("no providers to run")
        return 2

    measured: dict[str, dict[str, Result]] = {}
    for module, mods in modules.items():
        measured[module] = {}
        # One daemon per PHASE. Every event subscription spawns a `logoscore
        # watch` subprocess; sharing a daemon with the ~80-call method phase
        # wedged it partway through the events (the last five failed
        # contiguously with RPC_FAILED under the nix sandbox, and every one of
        # them passes on a fresh daemon). A phase must not be able to poison
        # the next one — otherwise a red cell means "something earlier used up
        # a resource", which is exactly the kind of unreliable signal this
        # whole exercise exists to remove.
        for phase, runner, work in (("methods", run_methods, cases),
                                    ("events", run_events, events)):
            with LogoscoreDaemon(modules_dir=mods, binary=args.logoscore) as daemon:
                client = daemon.client()
                client.load_module(module)
                measured[module].update(runner(client, module, work, args.timeout))

    rows, counts = [], {}
    by_case = {c["id"]: c for c in cases} | {e["id"]: e for e in events}

    for cid, case in by_case.items():
        for module in modules:
            got = measured[module].get(cid, Result(error="not-run"))
            want, have_want = (
                expectation(case, module) if "method" in case
                else (case["values"] if "values" in case else case["value"], True)
            )
            if not have_want:
                status = "skip"
            elif matches(got, want, case.get("raw", False)):
                status = "xpass" if (cid, module) in xfail else "pass"
            else:
                status = "xfail" if (cid, module) in xfail else "fail"

            counts[status] = counts.get(status, 0) + 1
            row = {
                "type": case["type"],
                "position": case["position"],
                "provider": module,
                "consumer": args.consumer,
                "case": cid,
                "status": status,
                "actual": got.as_report(),
            }
            if have_want:
                row["expected"] = jsonable(materialize(want, case.get("raw", False))) if not (
                    isinstance(want, dict) and set(want) == {"__error__"}
                ) else want
            if (cid, module) in xfail:
                row["known"] = xfail[(cid, module)]
            rows.append(row)

    # Differential: providers of the SAME contract must answer identically, so
    # any case without a declared per-provider expectation is compared across
    # them independently of `expect`. This catches a divergence nobody predicted
    # (it is how `void` was found). With a single provider there is nothing to
    # compare, and the table simply has no differential rows.
    names = list(modules)
    for cid, case in (by_case.items() if len(names) >= 2 else []):
        if "expect_by_provider" in case:
            continue  # the divergence IS the expectation; already reported above
        a = measured[names[0]].get(cid, Result(error="not-run"))
        b = measured[names[1]].get(cid, Result(error="not-run"))
        agree = (a.error == b.error) and (a.error is not None or same(a.value, b.value))
        # Registered on EITHER side: a defect that only one provider surfaces
        # (because the other's typed decode masks it) still makes the pair
        # disagree, and that disagreement is the same known cell.
        registered = next((xfail[(cid, n)] for n in names if (cid, n) in xfail), None)
        status = "pass" if agree else ("xfail" if registered else "fail")
        counts["differential-" + status] = counts.get("differential-" + status, 0) + 1
        if not agree:
            rows.append({
                "type": case["type"], "position": case["position"],
                "provider": f"{names[0]}-vs-{names[1]}", "consumer": args.consumer, "case": cid,
                "status": status, "expected": a.as_report(), "actual": b.as_report(),
                "known": registered,
                "note": "providers disagree and the contract declares no divergence",
            })

    # Coverage: every (type, position) the contract declares needs a case.
    uncovered = []
    if args.contract:
        covered = set()
        for c in by_case.values():
            # A multi-argument case covers specific (type, position) PAIRS —
            # `int` in slot 0, `bstr` in slot 2 — not the cross-product of its
            # type list and its position list, which would claim cells it never
            # exercises. Such a case declares them explicitly.
            if "cells" in c:
                for ty, pos in c["cells"]:
                    covered.add((norm_type(ty), pos))
                continue
            for pos in c["position"].split(","):
                covered.add((norm_type(c["type"]), pos))
        for ty, pos in sorted(contract_cells(Path(args.contract))):
            if (ty, pos) not in covered:
                uncovered.append((ty, pos))
                rows.append({
                    "type": ty, "position": pos, "provider": "-", "consumer": args.consumer,
                    "case": "-", "status": "uncovered",
                    "note": "declared by the contract, exercised by no case",
                })

    if args.jsonl:
        Path(args.jsonl).write_text("".join(json.dumps(r) + "\n" for r in rows))

    failures = [r for r in rows if r["status"] in ("fail", "xpass", "uncovered")]
    if not args.quiet:
        print(f"\nLIDL conformance matrix — consumer={args.consumer}")
        print(f"  {len(by_case)} cases x {len(modules)} providers")
        for k in sorted(counts):
            print(f"  {k:<22} {counts[k]}")
        if uncovered:
            print(f"\n  UNCOVERED cells declared by the contract ({len(uncovered)}):")
            for ty, pos in uncovered:
                print(f"    {ty}/{pos}")
        if failures:
            print(f"\n  FAILURES ({len(failures)}):")
            for r in failures:
                coord = f"{r['type']}/{r['position']}/{r['provider']}/{r['consumer']}"
                print(f"    {r['status']:<9} {coord}")
                print(f"              case={r['case']}")
                if "expected" in r:
                    print(f"              expected={json.dumps(r['expected'])[:110]}")
                print(f"              actual  ={json.dumps(r.get('actual'))[:110]}")
                if r["status"] == "xpass":
                    print(f"              ^ known-broken cell {r.get('known')} now PASSES — "
                          f"remove it from known.json")
        else:
            print("\n  all cells pass (or are registered xfail)")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
