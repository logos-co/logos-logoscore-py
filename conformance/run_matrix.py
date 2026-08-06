#!/usr/bin/env python3
"""Run the LIDL conformance matrix and report it as coordinates.

The matrix answers one question per cell: does a value of LIDL type T, in
position P, survive provider R -> consumer K intact? A red cell is printed as
`[uint]/method_return/test_fullapi_rust/py` — never as an aggregate token, so a
failure names itself and one provider cannot satisfy an assertion on behalf of
the other.

Four things keep it honest:

  * every case runs against BOTH providers, and the two results are compared to
    each other independently of `expect` — the cheapest possible detector for
    the next divergence, and it needs nobody to know the right answer first;
  * every case also runs against every CONSUMER, and those are compared to each
    other the same way. The same argument applies on this axis and it is not
    hypothetical: the `_bytes` collision (known.json M3) is invisible to a
    python consumer, which is precisely the consumer that can decline to decode
    the tag;
  * a known-broken cell is an `xfail` REGISTRY entry, and a cell that starts
    passing is an `xpass`, which fails the run so the registry gets updated;
  * coverage is computed from the contract: every (type, position) the .lidl
    declares must have at least one case, or the run fails. That is the
    structural guard against a 31-method contract with 6 assertions.

Usage:
    run_matrix.py --cpp-modules DIR --rust-modules DIR [--logoscore BIN]
                  [--proxy-consumer LABEL=MODULE=DIR[=sync|async]]
                  [--contract full_api.lidl] [--jsonl out.jsonl] [--quiet]
                  [--report out.html] [--report-text] [--md known.md]

Reporting lives in `matrix_report.py` and measures nothing: it is handed the
rows this driver already computed. The terminal summary is useful with no flags
at all — a TYPE x POSITION grid, the differential including its agreements, and
what is NOT covered — and every coordinate line the previous report printed is
still printed, unchanged, because CI greps them.

The case table and the xfail registry live with the PROVIDERS, in
logos-test-modules/conformance/ — the driver lives here because it uses this
package's client, and logoscore-py already depends on logos-test-modules (the
reverse would be a cycle).

THE CONSUMER AXIS. `py` is the direct consumer: this package's client talks to
the provider. A `--proxy-consumer` point routes the SAME case table through an
intermediate module, so the value is decoded and re-encoded by that module's
generated wrappers before python ever sees it. `test_fullapi_qtproxy` is the
Qt-typed one (`type: core`, no `interface` key => apiStyle=qt), which is the only
surface the Qt wrappers are on.

Two caveats that must not be forgotten when reading a proxy cell:

  * python is still terminal. A proxy cell measures a ROUND TRIP through the
    other consumer, so a transformation that is its own inverse — a map that
    becomes bytes and re-encodes to the same map — stays invisible. What such a
    cell does catch is a LOSSY step, and a collision is lossy the moment the
    receiving slot is typed (see `echoMap` vs `echoAny` for `{"_bytes": ...}`).
  * a proxy adds a hop, so a red proxy cell is not automatically the consumer's
    fault. That is what the consumer differential is for: it names the pair.

CALL MODE is part of the consumer identity, not a variant of it. The generated
wrappers convert differently on the two paths — `_result.toULongLong()` in the
sync table, `qvariant_cast<qulonglong>(v)` (with a DEFAULT substituted on an
invalid QVariant) in the async one — so `qtproxy-sync` and `qtproxy-async` are
two distinct consumer surfaces running two distinct bodies of generated code.
Every method cell replays through both. Events do not have the axis: a
subscription is a callback either way.

Transport is likewise fixed: the daemon is constructed with no `transports=`, so
every cell here is measured over LocalSocket/QtRO. The plain (tcp/tcp_ssl) wire
has its own uint64 defect that this matrix therefore cannot reach — see
known.json.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import matrix_report  # noqa: E402  (the view; it measures nothing)


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
    __slots__ = ("value", "error", "status")

    def __init__(self, value=None, error=None):
        self.status = None
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


# ── the consumer axis ───────────────────────────────────────────────────────


class Consumer:
    """One point on the consumer axis.

    `py` calls the provider directly. A proxy point calls an intermediate module
    that forwards to the provider, so the value is decoded and re-encoded by that
    module's generated wrappers on the way through.
    """

    __slots__ = ("label", "module", "dirs", "call_mode", "probe_method")

    def __init__(self, label, module=None, dirs=(), call_mode=None,
                 probe_method="echoInt"):
        self.label = label
        self.module = module            # None => call the provider directly
        self.dirs = list(dirs)          # extra module dirs this consumer needs
        self.call_mode = call_mode      # None | "sync" | "async"
        self.probe_method = probe_method  # a call whose mode can be read back

    @property
    def is_proxy(self) -> bool:
        return self.module is not None

    def target(self, provider: str) -> str:
        return self.module or provider

    def modules_dirs(self, provider_dirs: dict) -> list:
        # A proxy declares BOTH providers as dependencies, so loading it needs
        # every provider dir present regardless of which one it is bound to.
        if not self.is_proxy:
            return list(provider_dirs.values())
        return [*self.dirs, *provider_dirs.values()]

    def prepare(self, client, provider: str, timeout: float) -> None:
        """Load and point this consumer at `provider`.

        Every setting is READ BACK. A `useProvider` that silently no-ops would
        make both providers measure identically and the provider differential
        would go permanently green; a `useCallMode` that silently no-ops would
        make the async half of the matrix a duplicate of the sync half. Neither
        failure announces itself, so neither is left to trust.
        """
        client.load_module(self.target(provider))
        if not self.is_proxy:
            return
        client.call(self.module, "useProvider", provider, timeout=timeout)
        got = client.call(self.module, "currentProvider", timeout=timeout)
        if got != provider:
            raise RuntimeError(
                f"{self.label}: useProvider({provider}) did not take (currentProvider={got!r})")
        if self.call_mode is None:
            return
        client.call(self.module, "useCallMode", self.call_mode, timeout=timeout)
        got = client.call(self.module, "currentCallMode", timeout=timeout)
        if got != self.call_mode:
            raise RuntimeError(
                f"{self.label}: useCallMode({self.call_mode}) did not take (currentCallMode={got!r})")
        # ...and prove the selected TABLE actually ran, not just that the flag is
        # set: one known-good call, then the status the module recorded for it.
        # `echoInt` is this contract's probe; a proxy for a different contract
        # would need its own, which is why the failure below names the method
        # rather than reporting a bare call error.
        try:
            client.call(self.module, self.probe_method, 1, timeout=timeout)
        except Exception as e:
            raise RuntimeError(
                f"{self.label}: call-mode probe {self.probe_method}(1) failed ({e}); "
                f"a proxy consumer with a call mode needs a probe method this "
                f"contract actually has") from None
        status = client.call(self.module, "lastCallStatus", timeout=timeout)
        if status != f"ok-{self.call_mode}":
            raise RuntimeError(
                f"{self.label}: a call in {self.call_mode} mode reported {status!r}, "
                f"so the {self.call_mode} wrapper table is not the one being measured")

    def call_status(self, client, timeout: float):
        """What the proxy recorded about the call just made, or None."""
        if not (self.is_proxy and self.call_mode):
            return None
        try:
            return client.call(self.module, "lastCallStatus", timeout=timeout)
        except Exception:
            return "unavailable"


def parse_proxy_consumer(spec: str) -> Consumer:
    """LABEL=MODULE=DIR[=MODE]"""
    parts = spec.split("=")
    if len(parts) not in (3, 4):
        raise ValueError(f"--proxy-consumer expects LABEL=MODULE=DIR[=MODE], got {spec!r}")
    label, module, path = parts[0], parts[1], parts[2]
    mode = parts[3] if len(parts) == 4 else None
    if mode is not None and mode not in ("sync", "async"):
        raise ValueError(f"--proxy-consumer mode must be sync or async, got {mode!r}")
    return Consumer(label, module=module, dirs=[path], call_mode=mode)


# ── the table ───────────────────────────────────────────────────────────────

# Every key a case or an event may carry. An unknown one is a HARD ERROR rather
# than something to ignore, because ignoring it is silent and wrong in the worst
# direction: a case whose expectation is spelled with a key this driver does not
# read has no expectation at all, takes `have_want = False`, and is filed
# `status: "skip"` — which is not in the failing-status set. It would sit in the
# table looking deliberate, count as coverage, and assert nothing.
#
# That is not hypothetical. cases.json's own schema comment documented
# `expect_error` for years; no such key was ever implemented, and a case written
# the way the comment described would have been skipped in silence.
#
# These sets are what this driver and matrix_report.py READ — not what the
# shipped tables happen to contain. The distinction is the whole correctness
# argument: derived from the data, the lists would silently omit any key that is
# supported but currently unused, and this guard would then reject a legitimate
# case. `raw` on an EVENT is exactly that — run_events() honours it, no event
# uses it today. A key belongs here when some code path reads it, and the test
# suite pins the two shipped tables against these sets so the reverse mistake
# (rejecting real cases) fails loudly too.
CASE_KEYS = {
    "id", "type", "position", "method", "args", "cells", "tags", "why",
    "raw", "timeout_ms", "expect", "expect_by_provider",
}
EVENT_KEYS = {
    "id", "type", "position", "event", "fire", "value", "values", "cells",
    "tags", "why", "raw",
}
TABLE_KEYS = {"schema", "contract", "comment", "providers", "cases", "events"}


def validate_table(table: dict, path: str) -> None:
    """Reject a table carrying keys this driver does not read.

    Checked for the WHOLE table before anything runs, so the error names every
    offending key at once instead of one per re-run.
    """
    bad = [f"(table): unknown key {k!r}" for k in sorted(set(table) - TABLE_KEYS)]
    for kind, allowed in (("cases", CASE_KEYS), ("events", EVENT_KEYS)):
        for entry in table.get(kind) or []:
            for k in sorted(set(entry) - allowed):
                bad.append(f"{kind}[{entry.get('id', '?')!r}]: unknown key {k!r}")
    if bad:
        raise SystemExit(
            f"{path}: {len(bad)} unknown key(s) — this driver would ignore them, "
            f"and an ignored expectation is a case that silently asserts "
            f"nothing:\n  " + "\n  ".join(bad)
            + "\n\nA rejection is spelled `\"expect\": {\"__error__\": \"<code>\"}`."
        )


# ── the registries ──────────────────────────────────────────────────────────


def load_xfail(known: dict) -> dict:
    """(case, provider, consumer) -> entry id.

    `consumers` is REQUIRED, and deliberately has no default. The entry-wide
    `providers` key already taught this lesson once: a defect present on one
    provider was registered against both, and the passing one was then reported
    as `xpass` — the registry manufacturing a failure. A defaulted `consumers`
    would repeat it one axis over, and silently, because the surface an entry was
    measured on is exactly the thing nobody remembers a year later.
    """
    xfail: dict[tuple[str, str, str], str] = {}
    for entry in known["xfail"]:
        for key in ("cases", "providers", "consumers"):
            if key not in entry:
                raise SystemExit(
                    f"known.json xfail entry {entry.get('id')!r} has no `{key}`; "
                    f"an xfail must state which surfaces it was measured on")
        by_prov = entry.get("per_case_providers", {})
        by_cons = entry.get("per_case_consumers", {})
        for cid in entry["cases"]:
            for prov in by_prov.get(cid, entry["providers"]):
                for cons in by_cons.get(cid, entry["consumers"]):
                    xfail[(cid, prov, cons)] = entry["id"]
    return xfail


class SkipRegistry:
    """`known.json`'s `skip[]` — cells a surface cannot express.

    This list existed from the start and NOTHING read it. With one consumer that
    was invisible; a second consumer forces it closed, because "unsupported by
    this surface" and "broken on this surface" are different claims and only one
    of them should be silent.
    """

    def __init__(self, known: dict):
        self.entries = []
        for i, entry in enumerate(known.get("skip", [])):
            if "consumers" not in entry:
                raise SystemExit(
                    f"known.json skip entry #{i} has no `consumers`; a skip must "
                    f"name the surface that cannot express the cell")
            self.entries.append({
                "patterns": entry["cases"],
                "providers": entry.get("providers"),   # None => every provider
                "consumers": entry["consumers"],
                "reason": entry.get("reason", "unspecified"),
            })

    def reason(self, cid: str, provider: str, consumer: str):
        for e in self.entries:
            if consumer not in e["consumers"]:
                continue
            if e["providers"] is not None and provider not in e["providers"]:
                continue
            if any(fnmatch.fnmatch(cid, p) for p in e["patterns"]):
                return e["reason"]
        return None

    def dead_patterns(self, case_ids) -> list:
        """Patterns matching no case in the table — a typo, not a skip."""
        dead = []
        for e in self.entries:
            for p in e["patterns"]:
                if not any(fnmatch.fnmatch(c, p) for c in case_ids):
                    dead.append(p)
        return dead


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


def run_methods(client, consumer: "Consumer", provider: str, cases: list, timeout: float):
    from logoscore.errors import LogoscoreError, MethodError

    module = consumer.target(provider)
    out: dict[str, Result] = {}
    for case in cases:
        raw = case.get("raw", False)
        args = [materialize(a, raw) for a in case.get("args", [])]
        t = case.get("timeout_ms", timeout * 1000) / 1000.0
        try:
            # decode_bytes is tied to `raw` for the same reason the ARGUMENT is:
            # a `raw` case is one where the tagged-bytes form must be observed as
            # data, not materialized. The client decodes tags unconditionally by
            # default, so without this the _bytes-collision cells measured the
            # CLIENT undoing the collision rather than the system producing it —
            # and no change to any C++ repo could ever have moved them.
            r = Result(value=client.call(
                module, case["method"], *args, timeout=t, decode_bytes=not raw))
        except MethodError as e:
            r = Result(error=e.code or "MethodError")
        except LogoscoreError as e:
            r = Result(error=type(e).__name__)
        except Exception as e:
            # An adversarial payload can HANG the call rather than fail it (the
            # pending-call sentinel does exactly that), which surfaces as a
            # subprocess timeout. A cell that wedges the driver would take the
            # whole matrix with it, so record it as the failure it is.
            r = Result(error=type(e).__name__)
        # Which generated table actually served this cell. Recorded per cell,
        # not once per phase: the async wrapper substitutes a DEFAULT (0, an
        # empty list) when no value arrives, so "the callback answered 0" and
        # "the callback never ran" produce the same cell value and are told
        # apart only here.
        r.status = consumer.call_status(client, timeout)
        out[case["id"]] = r
    return out


def run_events(client, consumer: "Consumer", provider: str, events: list, timeout: float):
    from logoscore.errors import LogoscoreError, MethodError

    module = consumer.target(provider)
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
    # a table naming a different provider set — full_api_ext names its own two —
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
    ap.add_argument("--consumer", default="py",
                    help="label for the DIRECT consumer (this package's client)")
    ap.add_argument("--proxy-consumer", action="append", default=[],
                    metavar="LABEL=MODULE=DIR[=MODE]",
                    help="replay the table through a forwarding module; repeatable. "
                         "MODE (sync|async) selects the generated wrapper table.")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--quiet", action="store_true")
    # Reporting. `--report` is the artifact (self-contained HTML, no CDN, same
    # Pages precedent as the doctest harness); `--report-text` is the same
    # drill-down for anyone without a browser; `--md` regenerates the one
    # registry table the conformance README maintains by hand.
    ap.add_argument("--report", default=None, metavar="FILE.html",
                    help="write the full HTML report")
    ap.add_argument("--report-text", action="store_true",
                    help="also print the per-type, per-case drill-down")
    ap.add_argument("--md", default=None, metavar="FILE.md",
                    help="write the README's known-broken table")
    # The report payload on its own. `matrix_report.py --merge` reads either
    # this or the HTML page it is embedded in, so a merged page can be built
    # without a browser-shaped artifact in the middle. Nothing else reads it,
    # and it is NOT the machine-readable artifact — that is `--jsonl`, whose
    # contents this flag does not touch.
    ap.add_argument("--payload", default=None, metavar="FILE.json",
                    help="write the report payload as JSON (for --merge)")
    ap.add_argument("--no-color", action="store_true",
                    help="never colourise the terminal report")
    args = ap.parse_args()

    from logoscore import LogoscoreDaemon

    table = json.loads(Path(args.cases).read_text())
    known = json.loads(Path(args.known).read_text())
    validate_table(table, args.cases)
    cases, events = table["cases"], table["events"]

    xfail = load_xfail(known)
    skips = SkipRegistry(known)

    consumers = [Consumer(args.consumer)]
    for spec in args.proxy_consumer:
        try:
            consumers.append(parse_proxy_consumer(spec))
        except ValueError as e:
            print(e)
            return 2
    if len({c.label for c in consumers}) != len(consumers):
        print("duplicate consumer labels")
        return 2

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

    by_case = {c["id"]: c for c in cases} | {e["id"]: e for e in events}
    setup_errors = []

    # measured[(consumer, provider)][case] = Result
    measured: dict[tuple[str, str], dict[str, Result]] = {}
    for consumer in consumers:
        for provider in modules:
            key = (consumer.label, provider)
            measured[key] = {}
            # One daemon per PHASE. Every event subscription spawns a `logoscore
            # watch` subprocess; sharing a daemon with the ~80-call method phase
            # wedged it partway through the events (the last five failed
            # contiguously with RPC_FAILED under the nix sandbox, and every one of
            # them passes on a fresh daemon). A phase must not be able to poison
            # the next one — otherwise a red cell means "something earlier used up
            # a resource", which is exactly the kind of unreliable signal this
            # whole exercise exists to remove.
            #
            # A daemon is per (consumer, provider) too, so a proxy's bound target
            # and call mode are set on a fresh process rather than inherited from
            # the previous cell block.
            for runner, work in ((run_methods, cases), (run_events, events)):
                with LogoscoreDaemon(
                        modules_dir=consumer.modules_dirs(modules),
                        binary=args.logoscore) as daemon:
                    client = daemon.client()
                    try:
                        consumer.prepare(client, provider, args.timeout)
                    except Exception as e:
                        # A consumer that cannot be pointed at a provider must not
                        # quietly contribute a block of `not-run` cells that read
                        # as ordinary failures.
                        setup_errors.append(f"{consumer.label}/{provider}: {e}")
                        break
                    measured[key].update(
                        runner(client, consumer, provider, work, args.timeout))

    rows, counts = [], {}

    def bump(k):
        counts[k] = counts.get(k, 0) + 1

    for cid, case in by_case.items():
        for consumer in consumers:
            for module in modules:
                got = measured[(consumer.label, module)].get(cid, Result(error="not-run"))
                want, have_want = (
                    expectation(case, module) if "method" in case
                    else (case["values"] if "values" in case else case["value"], True)
                )
                ok = have_want and matches(got, want, case.get("raw", False))
                registered = xfail.get((cid, module, consumer.label))
                skipped = skips.reason(cid, module, consumer.label)

                if skipped:
                    # A skip claims the surface CANNOT express the cell. If it
                    # can, the claim is false and has to be retired — the same
                    # forcing function `xpass` applies to xfail entries.
                    status = "skip-passes" if ok else "skip-registered"
                elif not have_want:
                    status = "skip"
                elif ok:
                    status = "xpass" if registered else "pass"
                else:
                    status = "xfail" if registered else "fail"

                bump(status)
                row = {
                    "type": case["type"],
                    "position": case["position"],
                    "provider": module,
                    "consumer": consumer.label,
                    "case": cid,
                    "status": status,
                    "actual": got.as_report(),
                }
                if have_want:
                    row["expected"] = jsonable(materialize(want, case.get("raw", False))) if not (
                        isinstance(want, dict) and set(want) == {"__error__"}
                    ) else want
                if registered:
                    row["known"] = registered
                if skipped:
                    row["skip_reason"] = skipped
                if got.status:
                    row["call_status"] = got.status
                rows.append(row)

    # Every PROVIDER comparison is RECORDED, not only the ones that disagree.
    # The agreements are the evidence; leaving them out is why the differential
    # — the most valuable signal here — is invisible in a green run. `diffs`
    # feeds the report only: it changes no count, no row and no exit code. Each
    # entry carries the consumer it was measured on, because the same pair of
    # providers can agree on one surface and diverge on another (Q1b is exactly
    # that), and a differential flattened across surfaces would hide it.
    diffs: list[dict] = []

    def differential(kind, cid, case, label_a, res_a, label_b, res_b, note):
        agree = (res_a.error == res_b.error) and (
            res_a.error is not None or same(res_a.value, res_b.value))
        registered = next(
            (xfail[k] for k in ((cid, *label_a), (cid, *label_b)) if k in xfail), None)
        status = "pass" if agree else ("xfail" if registered else "fail")
        bump(f"differential-{kind}-{status}")
        if kind == "provider":
            diffs.append({"case": cid, "consumer": label_a[1], "declared": False,
                          "agree": agree, "status": status, "known": registered,
                          **({} if agree else {"values": {
                              label_a[0]: res_a.as_report(),
                              label_b[0]: res_b.as_report()}})})
        if not agree:
            rows.append({
                "type": case["type"], "position": case["position"],
                "provider": label_a[0] if kind == "consumer" else f"{label_a[0]}-vs-{label_b[0]}",
                "consumer": f"{label_a[1]}-vs-{label_b[1]}" if kind == "consumer" else label_a[1],
                "case": cid, "status": status,
                "expected": res_a.as_report(), "actual": res_b.as_report(),
                "known": registered, "differential": kind, "note": note,
            })

    def comparable(cid, provider, consumer_label):
        """Skipped cells are not comparable: the surface declined the cell, so a
        disagreement with a surface that answered it is not a finding."""
        return skips.reason(cid, provider, consumer_label) is None

    # Provider differential, per consumer: providers of the SAME contract must
    # answer identically, so any case without a declared per-provider expectation
    # is compared across them independently of `expect`. This catches a divergence
    # nobody predicted (it is how `void` was found).
    names = list(modules)
    if len(names) >= 2:
        for cid, case in by_case.items():
            for consumer in consumers:
                if not all(comparable(cid, n, consumer.label) for n in names[:2]):
                    continue
                res_a, res_b = (measured[(consumer.label, n)].get(
                    cid, Result(error="not-run")) for n in names[:2])
                if "expect_by_provider" in case:
                    # The divergence IS the expectation, and the driver does not
                    # judge it — already reported above, cell by cell. Recorded
                    # for the report alone, because a declared divergence that
                    # quietly stopped diverging is worth seeing and nothing else
                    # would say so. No count: this is not a comparison the run
                    # can fail.
                    diffs.append({
                        "case": cid, "consumer": consumer.label, "declared": True,
                        "agree": (res_a.error == res_b.error) and (
                            res_a.error is not None or same(res_a.value, res_b.value)),
                        "values": {names[0]: res_a.as_report(),
                                   names[1]: res_b.as_report()}})
                    continue
                differential(
                    "provider", cid, case,
                    (names[0], consumer.label), res_a,
                    (names[1], consumer.label), res_b,
                    "providers disagree and the contract declares no divergence")

    # Consumer differential, per provider: the same value read by two consumer
    # surfaces. Every unordered pair, not just each-against-py — the sync/async
    # pair shares a module and differs only in which generated table ran, and
    # that comparison is the one nothing had ever made.
    for i, ca in enumerate(consumers):
        for cb in consumers[i + 1:]:
            for cid, case in by_case.items():
                for module in names:
                    if not (comparable(cid, module, ca.label)
                            and comparable(cid, module, cb.label)):
                        continue
                    differential(
                        "consumer", cid, case,
                        (module, ca.label), measured[(ca.label, module)].get(
                            cid, Result(error="not-run")),
                        (module, cb.label), measured[(cb.label, module)].get(
                            cid, Result(error="not-run")),
                        "consumer surfaces disagree on the same provider's answer")

    # Coverage: every (type, position) the contract declares needs a case.
    uncovered = []
    declared_cells = None
    if args.contract:
        declared_cells = contract_cells(Path(args.contract))
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
        for ty, pos in sorted(declared_cells):
            if (ty, pos) not in covered:
                uncovered.append((ty, pos))
                rows.append({
                    "type": ty, "position": pos, "provider": "-", "consumer": "-",
                    "case": "-", "status": "uncovered",
                    "note": "declared by the contract, exercised by no case",
                })

    # A skip pattern that matches no case in the table is a typo, and a typo'd
    # skip is worse than no skip: it reads as coverage while excluding nothing.
    for pattern in skips.dead_patterns(by_case):
        rows.append({
            "type": "-", "position": "-", "provider": "-", "consumer": "-",
            "case": pattern, "status": "dead-skip",
            "note": "known.json skip pattern matches no case in the table",
        })

    for msg in setup_errors:
        rows.append({
            "type": "-", "position": "-", "provider": "-", "consumer": "-",
            "case": "-", "status": "setup-failed", "note": msg,
        })

    if args.jsonl:
        Path(args.jsonl).write_text("".join(json.dumps(r) + "\n" for r in rows))

    # The report is built from `rows` — the same list `--jsonl` writes — so the
    # human view and the machine artifact cannot drift apart. It is a pure
    # re-arrangement: nothing below measures anything or changes a verdict.
    payload = matrix_report.build_payload(
        consumer=consumers[0].label, providers=list(modules), provider_dirs=modules,
        by_case=by_case, rows=rows, diffs=diffs, counts=counts, known=known,
        declared_cells=declared_cells, table=table, norm_type=norm_type,
        paths={"cases": args.cases, "known": args.known,
               "contract": args.contract, "logoscore": args.logoscore})
    color = False if args.no_color else None
    if args.report:
        Path(args.report).write_text(matrix_report.render_html(payload))
    if args.payload:
        Path(args.payload).write_text(json.dumps(payload, ensure_ascii=False))
    if args.md:
        Path(args.md).write_text(matrix_report.render_markdown(payload))

    failures = [r for r in rows if r["status"] in (
        "fail", "xpass", "uncovered", "dead-skip", "skip-passes", "setup-failed")]
    if not args.quiet:
        labels = ", ".join(c.label for c in consumers)
        print(f"\nLIDL conformance matrix — consumers: {labels}")
        print(f"  {len(by_case)} cases x {len(modules)} providers x {len(consumers)} consumers")
        for k in sorted(counts):
            print(f"  {k:<28} {counts[k]}")
        for line in matrix_report.render_sections(payload, color=color):
            print(line)
        if args.report_text:
            for line in matrix_report.render_drilldown(payload, color=color):
                print(line)
        if uncovered:
            print(f"\n  UNCOVERED cells declared by the contract ({len(uncovered)}):")
            for ty, pos in uncovered:
                print(f"    {ty}/{pos}")

        # The consumer deltas, grouped by PAIR. Read on their own they are the
        # answer to "which cells does this surface get differently", which is
        # what a new consumer is added to find out.
        deltas = [r for r in rows if r.get("differential") == "consumer"]
        if deltas:
            pairs = sorted({r["consumer"] for r in deltas})
            for pair in pairs:
                group = [r for r in deltas if r["consumer"] == pair]
                a, b = pair.split("-vs-")
                print(f"\n  CONSUMER DELTA {pair} ({len(group)}):")
                for r in group:
                    print(f"    {r['status']:<9} {r['type']}/{r['position']}/{r['provider']}")
                    print(f"              case={r['case']}")
                    print(f"              {a:<16}={json.dumps(r['expected'])[:100]}")
                    print(f"              {b:<16}={json.dumps(r['actual'])[:100]}")

        if failures:
            print(f"\n  FAILURES ({len(failures)}):")
            for r in failures:
                coord = f"{r['type']}/{r['position']}/{r['provider']}/{r['consumer']}"
                print(f"    {r['status']:<9} {coord}")
                print(f"              case={r['case']}")
                if "note" in r and r["status"] in ("dead-skip", "setup-failed"):
                    print(f"              {r['note']}")
                if "expected" in r:
                    print(f"              expected={json.dumps(r['expected'])[:110]}")
                if "actual" in r:
                    print(f"              actual  ={json.dumps(r.get('actual'))[:110]}")
                if r.get("call_status"):
                    print(f"              call_status={r['call_status']}")
                if r["status"] == "xpass":
                    print(f"              ^ known-broken cell {r.get('known')} now PASSES — "
                          f"remove it from known.json")
                if r["status"] == "skip-passes":
                    print(f"              ^ registered as unsupported by this surface "
                          f"({r.get('skip_reason')}), but it works — retire the skip")
        else:
            print("\n  all cells pass (or are registered xfail/skip)")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
