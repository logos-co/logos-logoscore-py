#!/usr/bin/env python3
"""Render the LIDL conformance matrix as something a human can read.

This module measures NOTHING. It is handed what `run_matrix.py` already
computed — the per-cell rows, the differential verdicts, the case table and the
registry — and turns it into a view. Every number here is a re-arrangement of a
number the driver produced; if the two ever disagree, the driver is right.

The view is built around the question the matrix asks, which is not "how many
cells passed" but:

    does a value of LIDL type T, in position P, survive provider R -> consumer K?

So the primary pivot is TYPE x POSITION, with (provider, consumer) as the
content of a cell. A flat table of every cell answers the question once per row
and never in aggregate; the grid answers it for the whole system on one screen,
and the per-type drill-down answers it case by case underneath.

Four things the renderer is careful about, because they are the things a report
gets wrong:

  * a cell that is green because it PASSES and a cell that is green because it
    is a registered `xfail` must never look alike. Status is carried by glyph
    AND colour AND (for a known-broken cell) the registry id printed next to
    it, never by colour alone;
  * the differential — the two providers compared to each other, independently
    of `expect` — is the matrix's most valuable signal and is invisible today
    unless it fails. Every comparison is reported, including the agreements;
  * what is NOT covered is part of the report: uncovered contract cells,
    registry `skip` entries, `coverage_limits`, and registry entries that
    matched no cell in this run;
  * the prose is hand-written and authoritative. A case's `why` and a registry
    entry's evidence are reproduced verbatim, never paraphrased and never
    replaced by a generated second opinion.
"""
from __future__ import annotations

import datetime
import html as _html
import json
import os
import sys

# ── the vocabulary ──────────────────────────────────────────────────────────
# A cell's status is one token, and the token is the same in every surface: the
# terminal glyph, the HTML class and the JSON payload all say "xfail".

GLYPH = {
    "pass": "\u00b7",       # ·  quiet — passing is the background
    "xfail": "K",           #    Known-broken; never printed without its id
    "fail": "X",
    "xpass": "^",           #    the registry is stale — the run fails
    "skip": "~",            #    the table declares no expectation for the cell
    "not-run": "?",
}

# Terminal colours, used only when stdout is a tty. Never the sole carrier of
# meaning — see the module docstring.
ANSI = {
    "pass": "\033[32m", "xfail": "\033[33m", "fail": "\033[31m",
    "xpass": "\033[35m", "skip": "\033[90m", "not-run": "\033[31m", "dim": "\033[2m", "bold": "\033[1m",
    "head": "\033[1;36m", "reset": "\033[0m",
}

# Positions, in the order a value travels — into a call, out of it, out of an
# event, and inside a container — with the per-ARGUMENT-SLOT columns after
# them rather than interleaved.
#
# Slots exist only because three cases take several arguments, so `a0 a1 a2 e0
# e1 e2` hold nine tokens between them and 87 dots. Sorted by travel order they
# sit between `arg` and `ret`, which are the two columns carrying most of the
# contract, and push them six columns apart. The scan that matters is
# arg -> ret -> evt, and it should be adjacent.
#
# Unknown positions are appended rather than dropped — nothing may be silently
# absent from the matrix, and that includes the report.
POSITION_ORDER = [
    "method_arg", "method_return", "event_param", "nested_in_container",
    "method_arg@0", "method_arg@1", "method_arg@2",
    "event_param@0", "event_param@1", "event_param@2",
]
POSITION_ABBR = {
    "method_arg": "arg", "method_arg@0": "a0", "method_arg@1": "a1",
    "method_arg@2": "a2", "method_return": "ret",
    "event_param": "evt", "event_param@0": "e0", "event_param@1": "e1",
    "event_param@2": "e2", "nested_in_container": "nest",
}

# Cases are ordered inside a type by what they are FOR, so the tag axis is the
# sort rather than a second pivot: the nominal round-trip first, then the
# boundaries, then the ways a caller can be hostile. (Event cases carry no tag
# and lead, because an event round-trip is the plainest thing a type does.)
TAG_ORDER = ["nominal", "boundary", "empty", "container", "bytes-at-depth",
             "record", "arity", "divergence", "hostile", "adversarial"]


def _tag_rank(case: dict) -> int:
    tags = case.get("tags") or []
    ranks = [TAG_ORDER.index(t) + 1 for t in tags if t in TAG_ORDER]
    return min(ranks) if ranks else 0


def _type_rank(t: str) -> tuple:
    """Scalars, then maps, then arrays — each alphabetical. A container sorts
    next to the other containers rather than next to the `[` character."""
    kind = 2 if t.startswith("[") else (1 if t.startswith("{") else 0)
    return (kind, t)


def _render(v) -> str:
    """A value as display TEXT, decided in python.

    The page must never render a measured value from the parsed payload.
    `JSON.parse` puts every number in a double, so `uint/boundary/max` — the
    case whose entire purpose is proving that 18446744073709551615 survives —
    printed 18446744073709552000, and `uint/boundary/above-2^53` printed
    ...992 for ...993. The report reproduced M1/M5/M6 while reporting on them.

    The JSON text embedded in the page has the exact digits; only the parse
    loses them. So every value a surface displays is rendered here, by the
    interpreter that holds it exactly, and the page prints the string.
    """
    return json.dumps(v, ensure_ascii=False)


def _render_call(case, kind=None) -> dict:
    """The `sent` and `want` halves of a case's one-line summary, as text."""
    if case.get("args") is not None:
        inner = _render(case["args"])[1:-1]
        sent = f"{case.get('method') or case.get('event')}({inner})"
    elif case.get("method") or case.get("event") or case.get("fire"):
        val = case.get("values", case.get("value"))
        sent = (f"{case.get('fire') or case.get('event') or case.get('method')}"
                f"{'' if val is None else ' ' + _render(val)}")
    else:
        sent = ""
    ebp = case.get("expect_by_provider")
    if ebp:
        vals = [_render(v) for v in ebp.values()]
        if len(set(vals)) == 1:
            return {"sent": sent, "want": vals[0], "want_kind": "identical"}
        if kind == "identity":
            return {"sent": sent, "want": "", "want_kind": "identity"}
        return {"sent": sent, "want": "", "want_kind": "per_provider",
                "want_pairs": [[_prov(p), _render(v)] for p, v in ebp.items()]}
    if case.get("expect") is not None:
        return {"sent": sent, "want": _render(case["expect"]), "want_kind": "one"}
    return {"sent": sent, "want": "", "want_kind": ""}


def _divergence_kind(dd: dict, providers) -> str:
    """Why a DECLARED divergence diverges: `identity`, `behaviour`, or `agree`.

    Two of the six declared divergences differ because the value carries the
    provider's own name — `result/ok`'s own `why` says exactly that, and
    `identity/whoami` returns nothing else. They are true by construction and
    always will be, so printing them at the same weight as a genuine
    behavioural split (one provider accepts a hostile value, the other rejects
    it) buries the split under the boilerplate.

    Decided from the measured values rather than a hand-kept list of case ids:
    a divergence is `identity` when every provider's answer contains its own
    name and no other provider's. A new identity case then classifies itself,
    and one that stops being about identity stops being filed as such.
    """
    if dd.get("agree"):
        return "agree"
    vals = dd.get("values") or {}
    if len(vals) < 2 or set(vals) != set(providers):
        return "behaviour"
    for name, v in vals.items():
        blob = json.dumps(v, ensure_ascii=False)
        if name not in blob or any(o in blob for o in vals if o != name):
            return "behaviour"
    return "identity"


def _positions_of(case: dict, norm_type) -> list[tuple[str, str]]:
    """The (type, position) pairs a case actually exercises.

    A multi-argument case declares them explicitly in `cells`, because the
    cross-product of its type list and its position list would claim cells it
    never touches — `arity/triple` would otherwise be filed under a type
    literally named "int,tstr,bstr".
    """
    if "cells" in case:
        return [(norm_type(t), p) for t, p in case["cells"]]
    return [(norm_type(case["type"]), p) for p in case["position"].split(",")]


# ── the payload ─────────────────────────────────────────────────────────────


def build_payload(*, consumer, providers, provider_dirs, by_case, rows, diffs,
                  counts, known, declared_cells, paths, table, norm_type):
    """Everything the views need, as plain JSON.

    `rows` is exactly what `--jsonl` writes, so the report cannot drift from the
    machine-readable artifact: both are derived from the same list.
    """
    # A differential row is identified by its own `differential` key, NOT by the
    # shape of its labels. Provider differentials spell the pair in `provider`
    # (`a-vs-b`) and a CONSUMER differential spells it in `consumer`, keeping a
    # real provider name — so a filter that recognised only the first shape let
    # consumer differentials through as if they were cells, and their `known:
    # None` then broke the per-case rollup.
    # Restricted to rows that name a real provider for the same reason: an
    # uncovered / dead-skip / setup-failed row carries "-" in both label slots
    # and is not a surface anything was measured on.
    measured_rows = [r for r in rows if not r.get("differential")]
    consumers = sorted({r["consumer"] for r in measured_rows
                        if r.get("consumer") and r["provider"] in providers}
                       ) or [consumer]

    # ── per-cell results, keyed by the coordinate they are reported under ──
    cells: dict[tuple[str, str, str], dict] = {}
    for r in measured_rows:
        if r["case"] not in by_case or r["provider"] not in providers:
            continue  # uncovered, dead-skip and setup-failed rows are not cells
        cells[(r["case"], r["provider"], r.get("consumer", consumer))] = r

    # Annotated on a COPY: `diffs` belongs to the driver, and the report is not
    # allowed to change anything the driver computed.
    diffs = [dict(d, kind=_divergence_kind(d, providers)) if d.get("declared")
             else dict(d) for d in diffs]
    for d in diffs:
        if d.get("values"):
            d["values_s"] = {p: _render(v) for p, v in d["values"].items()}
    # The provider differential runs on EVERY consumer surface, so a case has
    # one entry per surface. The per-case badge shows the primary consumer's,
    # chosen explicitly: a last-wins flatten would make the badge depend on
    # which surface happened to be measured last. The per-surface detail is in
    # the differential panel, which is where a divergence belongs.
    diff_by_case: dict[str, dict] = {}
    for d in diffs:
        if d["case"] not in diff_by_case or d.get("consumer") == consumer:
            diff_by_case[d["case"]] = d

    # ── the cases, resolved ──
    case_views = []
    for cid, case in by_case.items():
        pairs = _positions_of(case, norm_type)
        results = {}
        for prov in providers:
            for cons in consumers:
                r = cells.get((cid, prov, cons))
                if r is None:
                    continue
                entry = {"status": r["status"], "provider": prov,
                         "consumer": cons}
                if "known" in r:
                    entry["known"] = r["known"]
                # Values are carried for anything that is NOT a plain pass. A
                # green cell's value is the case's own `expect`, which is right
                # here in the table; repeating it per provider would be several
                # hundred identical "it matched" blobs.
                if r["status"] != "pass":
                    if "expected" in r:
                        entry["expected"] = r["expected"]
                        entry["expected_s"] = _render(r["expected"])
                    entry["actual"] = r.get("actual")
                    entry["actual_s"] = _render(r.get("actual"))
                results[f"{prov}|{cons}"] = entry
        statuses = [e["status"] for e in results.values()]
        case_views.append({
            "id": cid,
            "type": norm_type(case["type"]),
            "position": case["position"],
            "cells": [list(p) for p in pairs],
            "tags": case.get("tags") or [],
            "tag_rank": _tag_rank(case),
            "why": case.get("why"),
            "call": case.get("method") or case.get("event"),
            "fire": case.get("fire"),
            "args": case.get("args"),
            "value": case.get("values", case.get("value")),
            "expect": case.get("expect"),
            "expect_by_provider": case.get("expect_by_provider"),
            "render": _render_call(
                case, (diff_by_case.get(cid) or {}).get("kind")),
            "results": results,
            "rollup": _rollup(statuses),
            "known": sorted({e["known"] for e in results.values()
                             if "known" in e}),
            "differential": diff_by_case.get(cid),
        })

    # ── the grid ──
    covered: dict[tuple[str, str], list[str]] = {}
    for cv in case_views:
        for ty, pos in cv["cells"]:
            covered.setdefault((ty, pos), []).append(cv["id"])

    declared = {tuple(c) for c in (declared_cells or [])}
    axis_types = sorted({t for t, _ in covered} | {t for t, _ in declared},
                        key=_type_rank)
    seen_pos = {p for _, p in covered} | {p for _, p in declared}
    axis_positions = ([p for p in POSITION_ORDER if p in seen_pos]
                      + sorted(p for p in seen_pos if p not in POSITION_ORDER))

    by_id = {cv["id"]: cv for cv in case_views}
    grid = {}
    for ty in axis_types:
        row = {}
        for pos in axis_positions:
            ids = covered.get((ty, pos))
            if ids:
                tally: dict[str, int] = {}
                for cid in ids:
                    tally[by_id[cid]["rollup"]] = tally.get(
                        by_id[cid]["rollup"], 0) + 1
                row[pos] = {"state": "covered", "cases": ids, "tally": tally,
                            "declared": (ty, pos) in declared}
            elif (ty, pos) in declared:
                row[pos] = {"state": "uncovered", "cases": [], "tally": {},
                            "declared": True}
            else:
                row[pos] = {"state": "n/a", "cases": [], "tally": {},
                            "declared": False}
        grid[ty] = row

    uncovered = sorted((t, p) for (t, p) in declared if (t, p) not in covered)
    synthetic = sorted((t, p) for (t, p) in covered
                       if declared_cells is not None and (t, p) not in declared)

    # ── the registry, and how much of it this run actually touched ──
    used: dict[str, list[dict]] = {}
    for cv in case_views:
        for e in cv["results"].values():
            if "known" in e:
                # The STATUS is carried, not just the coordinate: an entry whose
                # cells have all gone `xpass` is a stale entry, and a bare count
                # would read exactly like a live one.
                used.setdefault(e["known"], []).append(
                    {"cell": f"{cv['id']}/{e['provider']}/{e['consumer']}",
                     "status": e["status"], "case": cv["id"]})
    # A defect only one provider surfaces makes the PAIR disagree, and the
    # driver attributes that disagreement to the same entry. Counted SEPARATELY
    # from cells: a differential verdict is about a case, not about one
    # coordinate, and folding it into the cell tally reads as an extra cell.
    diff_used: dict[str, list[str]] = {}
    for cv in case_views:
        d = cv["differential"]
        if d and d.get("known"):
            diff_used.setdefault(d["known"], []).append(cv["id"])

    registry = {k: known.get(k, []) for k in
                ("xfail", "skip", "coverage_limits", "fixed", "unmeasurable")}
    dead = [e["id"] for e in registry["xfail"]
            if not used.get(e["id"]) and not diff_used.get(e["id"])]
    # A `skip` entry naming a consumer this run does not have produces no rows
    # at all — the driver has nothing to skip. That is not the same as "nothing
    # was skipped", and it is the state the registry is in today (both entries
    # name `qml`, which no driver replays).
    inert_skips = [i for i, s in enumerate(registry["skip"])
                   if not (set(s.get("consumers", [])) & set(consumers))]

    return {
        "meta": {
            "generated": datetime.datetime.now(
                datetime.timezone.utc).replace(microsecond=0).isoformat(),
            "consumer": consumer,
            "consumers": consumers,
            "providers": list(providers),
            "provider_dirs": dict(provider_dirs),
            "paths": dict(paths),
            "contract_name": table.get("contract"),
            "case_count": len(by_case),
            "cell_count": len(cells),
        },
        "counts": dict(counts),
        "axis": {"types": axis_types, "positions": axis_positions},
        "grid": grid,
        "cases": sorted(case_views, key=lambda c: (
            _type_rank(c["type"]), c["tag_rank"], c["id"])),
        "differential": _differential_summary(diffs, providers, consumers),
        "diffs": diffs,
        "uncovered": [list(c) for c in uncovered],
        "synthetic": [list(c) for c in synthetic],
        "registry": registry,
        "registry_used": used,
        "registry_used_differential": diff_used,
        "registry_dead": dead,
        "registry_inert_skips": inert_skips,
    }


def _rollup(statuses: list[str]) -> str:
    """One word for a case across every provider x consumer it ran on.

    Worst-first, and `xfail` never absorbs a `fail`: a case that is registered
    broken on one surface and genuinely broken on another must report the
    genuine one.
    """
    if not statuses:
        return "not-run"
    for s in ("fail", "xpass", "not-run", "xfail", "skip"):
        if s in statuses:
            return s
    return "pass"


def _differential_summary(diffs, providers, consumers):
    """The provider-vs-provider comparison, agreements included.

    The driver records a differential ROW only when the two answers disagree,
    which is why 80 agreements are invisible in today's report. They are the
    evidence, not the absence of evidence.
    """
    out = {"providers": providers, "consumers": consumers, "by_consumer": {}}
    for cons in consumers:
        rows = [d for d in diffs if d.get("consumer", cons) == cons]
        out["by_consumer"][cons] = {
            "agree": sum(1 for d in rows if d.get("agree") is True
                         and not d.get("declared")),
            "differ": [d["case"] for d in rows if d.get("agree") is False
                       and not d.get("declared")],
            "declared": [d["case"] for d in rows if d.get("declared")],
            "declared_agree": sum(1 for d in rows
                                  if d.get("declared") and d.get("agree")),
        }
    return out


# ── terminal ────────────────────────────────────────────────────────────────


class _Paint:
    def __init__(self, enabled: bool):
        self.on = enabled

    def __call__(self, text: str, key: str) -> str:
        if not self.on or key not in ANSI:
            return text
        return f"{ANSI[key]}{text}{ANSI['reset']}"


def _grid_token(cell: dict) -> tuple[str, str]:
    """`8ok+1K` — the count of clean cases and the count of registered ones,
    as two tokens rather than one blended colour. An aggregate that averaged a
    known defect into a score would be the exact thing the registry exists to
    prevent."""
    if cell["state"] == "n/a":
        return ".", "dim"
    if cell["state"] == "uncovered":
        return "--", "fail"
    t = cell["tally"]
    parts, worst = [], "pass"
    if t.get("pass"):
        parts.append(f"{t['pass']}ok")
    for st, suffix in (("fail", "X"), ("xpass", "^"), ("xfail", "K"),
                       ("skip", "~"), ("not-run", "?")):
        if t.get(st):
            parts.append(f"{'+' if parts else ''}{t[st]}{suffix}")
            if worst == "pass" or st in ("fail", "xpass"):
                worst = st
    return "".join(parts), worst


def render_grid(payload, paint) -> list[str]:
    types = payload["axis"]["types"]
    positions = payload["axis"]["positions"]
    tw = max([len(t) for t in types] + [8]) + 2
    cw = 7
    out = ["", paint("  TYPE x POSITION"
                     "   — does a value of this type, in this position, survive?",
                     "head")]
    head = " " * (tw + 2) + "".join(
        POSITION_ABBR.get(p, p[:cw - 1]).rjust(cw) for p in positions)
    out.append(paint(head, "dim"))
    for ty in types:
        line = "  " + ty.ljust(tw)
        for pos in positions:
            tok, key = _grid_token(payload["grid"][ty][pos])
            line += paint(tok.rjust(cw), key)
        out.append(line)
    out.append("")
    n_surfaces = len(payload["meta"]["providers"]) * len(
        payload["meta"]["consumers"])
    out.append(paint(
        f"    n ok = n cases green on all {n_surfaces} provider x consumer "
        f"surfaces      +n K = n cases with a registered known-broken cell",
        "dim"))
    out.append(paint(
        "    --   = declared by the contract, NO CASE"
        "                        .    = no such position for this type", "dim"))
    if payload["synthetic"]:
        pairs = ", ".join(f"{t}@{p}" for t, p in payload["synthetic"])
        out.append(paint(
            f"    covered but not declared by the contract: {pairs}", "dim"))
    return out


def render_differential(payload, paint) -> list[str]:
    d = payload["differential"]
    provs, cons = d["providers"], d["consumers"]
    out = ["", paint("  DIFFERENTIAL"
                     "   — the answers compared to EACH OTHER, independently "
                     "of `expect`", "head")]
    if len(provs) < 2:
        for line in _wrap(f"provider  one provider "
                          f"({provs[0] if provs else '-'}); nothing to compare. "
                          f"This table has no differential column, which is a "
                          f"gap, not a design choice.", 84):
            out.append(paint(f"    {line}", "dim"))
    else:
        out.append(f"    provider  {provs[0]} vs {provs[1]}")
        for c in cons:
            s = d["by_consumer"][c]
            agree = paint(f"{s['agree']:>4} agree", "pass")
            if s["differ"]:
                verdict = paint(f"{len(s['differ'])} differ", "fail")
            else:
                verdict = paint("0 differ", "dim")
            # The breakdown, not just the count: "6 declared divergences" reads
            # as six facts about the system, and two of them are the provider
            # spelling its own name.
            kinds: dict[str, int] = {}
            for dd in payload["diffs"]:
                if dd.get("declared") and dd.get("consumer", c) == c:
                    kinds[dd.get("kind", "behaviour")] = kinds.get(
                        dd.get("kind", "behaviour"), 0) + 1
            label = {"behaviour": "by behaviour", "agree": "no longer diverging",
                     "identity": "by identity"}
            detail = "; ".join(f"{kinds[k]} {label[k]}" for k in
                               ("behaviour", "agree", "identity") if kinds.get(k))
            extra = (f"   +{len(s['declared'])} declared, not compared"
                     f"{f'  ({detail})' if detail else ''}"
                     if s["declared"] else "")
            out.append(f"      on {c:<16}{agree}   {verdict}{paint(extra, 'dim')}")
    if len(cons) < 2:
        out.append(paint(
            "    consumer  one consumer point; a cell proves a provider agreed "
            "with this client,", "dim"))
        out.append(paint(
            "              not that two consumer surfaces agree with each "
            "other.", "dim"))
    else:
        out.append("    consumer")
        for i, a in enumerate(cons):
            for b in cons[i + 1:]:
                out.append(f"      {a} vs {b}")
    # One entry per (case, consumer). Collapse to one line per case — a panel
    # that repeated every divergence once per surface would say the same thing
    # three times — but keep a case whose surfaces DISAGREE, by name: that is
    # Q1b's whole shape, and showing only the first surface would report it as
    # settled.
    grouped: dict[str, list] = {}
    for dd in payload["diffs"]:
        if dd.get("declared"):
            grouped.setdefault(dd["case"], []).append(dd)
    declared = [entries[0] for entries in grouped.values()]
    split = [(cid, entries) for cid, entries in grouped.items()
             if len({e.get("kind") for e in entries}) > 1]
    if declared:
        out.append("")
        out.append(paint("    DECLARED DIVERGENCES (expect_by_provider) — the "
                         "driver does not compare these; the divergence IS the "
                         "expectation", "dim"))
        buckets = {k: [dd for dd in declared if dd.get("kind") == k]
                   for k in ("behaviour", "agree", "identity")}

        # The point of the panel. Two providers of the same contract answering
        # differently is a fact about the system, and the difference is usually
        # deep inside the values — so each gets a line, at full width.
        for dd in buckets["behaviour"]:
            out.append(f"      {dd['case']:<34}{paint('DIFFER', 'xfail')}"
                       f"{paint('  by behaviour', 'dim')}")
            for p, v in (dd.get("values") or {}).items():
                out.append(paint(f"        {_prov(p):>6}  {_short(v, 88)}", "dim"))

        # Amber, not green. "Both providers now answer the same" reads as good
        # news and is not: it means the per-provider split in the table has
        # nothing left to say and should collapse to a plain `expect`. Same
        # shape of finding as an `xpass`, and it wears the same colour.
        for dd in buckets["agree"]:
            vals = list((dd.get("values") or {}).items())
            one = _short(vals[0][1], 56) if vals else ""
            out.append(f"      {dd['case']:<34}{paint('NO LONGER DIVERGES', 'xpass')}"
                       f"  both {one}")
        if buckets["agree"]:
            out.append(paint("        ^ the table still declares a per-provider "
                             "expectation for these; it is now vestigial.", "dim"))

        # True by construction, so said once, in one line, and never again.
        if buckets["identity"]:
            ids = ", ".join(dd["case"] for dd in buckets["identity"])
            for i, line in enumerate(_wrap(
                    f"{len(buckets['identity'])} more differ BY IDENTITY — each "
                    f"provider answers with its own name, which is what the case "
                    f"is for: {ids}", 80)):
                out.append(paint(("      " if i == 0 else "        ") + line, "dim"))

        # A declared divergence that does not behave the same on every consumer
        # surface. The lines above show the primary one; this says where that is
        # not the whole story, which is the only way the panel can carry a
        # finding like Q1b instead of averaging it away.
        for cid, entries in split:
            out.append(f"      {cid:<34}{paint('VARIES BY CONSUMER', 'xfail')}")
            for e in entries:
                verdict = {"behaviour": "providers DIFFER",
                           "agree": "providers agree — no longer diverges",
                           "identity": "differ by identity"}.get(e.get("kind"), "?")
                out.append(paint(f"        {e.get('consumer', '?'):>14}  {verdict}",
                                 "dim"))
    return out


def _prov(name: str) -> str:
    return name.replace("test_fullapi_", "")


def render_not_measured(payload, paint) -> list[str]:
    out = ["", paint("  NOT MEASURED"
                     "   — what this run does not answer, on the record", "head")]
    empty = True
    if payload["uncovered"]:
        empty = False
        out.append(paint(f"    {len(payload['uncovered'])} (type, position) "
                         "declared by the contract with no case — listed under "
                         "UNCOVERED below", "fail"))
    inert = payload["registry_inert_skips"]
    for i in inert:
        empty = False
        s = payload["registry"]["skip"][i]
        cons = ", ".join(s.get("consumers", []))
        out.append(f"    skip   {', '.join(s['cases'])}"
                   f"  on consumer(s) {cons}  [{s.get('reason', '-')}]")
    if inert:
        out.append(paint("           ^ this run has no such consumer, so these "
                         "produced no cells at all. \"Declared unsupported on a "
                         "surface", "dim"))
        out.append(paint("             nobody measured\" is not the same as "
                         "\"nothing was skipped\".", "dim"))
    for lim in payload["registry"]["coverage_limits"]:
        empty = False
        out.append(f"    limit  axis={lim['axis']:<10} {lim.get('status', 'OPEN')}")
        # Printed in FULL. These were clipped to three lines, which cut the
        # consumer-axis note at "This is not hypothetical: the …" — advertising
        # the evidence and then withholding it. There are two of them, they are
        # the whole reason the axis is a limit rather than a footnote, and a
        # reader who has to open known.json to finish the sentence is being
        # handed the JSON this report exists to replace.
        for line in _prose_lines(lim.get("note"), 84):
            out.append(paint(f"           {line}", "dim"))
    for entry_id in payload["registry_dead"]:
        empty = False
        out.append(paint(f"    stale? registry entry {entry_id} matched no cell "
                         f"in this run", "xfail"))
    if empty:
        out.append(paint("    nothing declared out of scope", "dim"))
    return out


_ABSENT = object()


def _measured_groups(payload, entry_id):
    """(expected, actual, status, where) for the cells one registry entry owns.

    Grouped on the outcome, not on the coordinate, so a defect both providers
    reproduce identically is reported as one line naming both.
    """
    groups: dict[str, tuple] = {}
    order: list[str] = []
    for cv in payload["cases"]:
        for key, r in cv["results"].items():
            if r.get("known") != entry_id or r["status"] == "pass":
                continue
            exp = r["expected"] if "expected" in r else _ABSENT
            sig = json.dumps([r["status"], exp is _ABSENT,
                              None if exp is _ABSENT else exp, r.get("actual")],
                             ensure_ascii=False)
            if sig not in groups:
                groups[sig] = (exp, r.get("actual"), r["status"], [])
                order.append(sig)
            groups[sig][3].append((cv["id"],
                                   f"{_prov(r['provider'])}/{r['consumer']}"))
    # The entry line above already names the cases, so a coordinate repeats the
    # case id once per cell for no gain — `cpp/py, rust/py` is the part that
    # differs. The case is re-stated only when the group spans several.
    out = []
    for sig in order:
        exp, act, status, keys = groups[sig]
        cases = {c for c, _ in keys}
        where = (", ".join(w for _, w in keys) if len(cases) == 1
                 else ", ".join(f"{c} {w}" for c, w in keys))
        out.append((exp, act, status,
                    f"{len(keys)} cells" if len(keys) > 3 else where))
    return out


def render_register(payload, paint) -> list[str]:
    entries = payload["registry"]["xfail"]
    if not entries:
        return []
    out = ["", paint("  KNOWN-BROKEN REGISTER"
                     "   — every amber cell above, and the claim behind it",
                     "head")]
    for e in entries:
        used = payload["registry_used"].get(e["id"], [])
        tally: dict[str, int] = {}
        for u in used:
            tally[u["status"]] = tally.get(u["status"], 0) + 1
        breakdown = ", ".join(f"{n} {s}" for s, n in sorted(tally.items()))
        diffs = payload["registry_used_differential"].get(e["id"], [])
        out.append(f"    {paint(e['id'], 'xfail')}  "
                   f"{len(used)} cell(s) in this run"
                   f"{' (' + breakdown + ')' if breakdown else ''}"
                   f"{f', + {len(diffs)} differential' if diffs else ''}   "
                   f"{paint('cases: ' + ', '.join(e['cases']), 'dim')}")
        for line in _wrap(e.get("summary", ""), 84):
            out.append(f"          {line}")
        # What the cell ACTUALLY DID, not only what the registry claims about
        # it. Without this the default report asserts a defect and shows no
        # evidence for it — and a registered cell is precisely the cell whose
        # measurement a reader has most reason to want. Identical outcomes on
        # several surfaces print once: two providers failing the same way is
        # one fact.
        for exp, act, status, where in _measured_groups(payload, e["id"]):
            if exp is not _ABSENT:
                out.append(paint(f"          expected {_short(exp, 68)}", "dim"))
            out.append(paint(f"          got      {_short(act, 68)}   "
                             f"[{status}] {where}", "dim"))
        # `fix_is` is a string in the older entries and a list of steps in the
        # newer ones (OPT1/OPT2). Both are valid registry data, so render both
        # rather than crashing on one — a report that dies on a well-formed
        # registry is worse than no report.
        if e.get("fix_is"):
            fix = e["fix_is"]
            parts = fix if isinstance(fix, list) else [fix]
            for n, part in enumerate(parts):
                label = "fix is: " if n == 0 else "        "
                for line in _wrap(label + str(part), 84):
                    out.append(paint(f"          {line}", "dim"))
    return out


def render_sections(payload, color=None) -> list[str]:
    """The blocks that go between the driver's own summary and its FAILURES
    list. The driver's lines are untouched — CI greps them."""
    paint = _Paint(_color_enabled(color))
    return (render_grid(payload, paint)
            + render_differential(payload, paint)
            + render_not_measured(payload, paint)
            + render_register(payload, paint))


def render_drilldown(payload, color=None) -> list[str]:
    """Per-type, per-case. Position is a COLUMN, not a nesting level: 47 of 86
    cases sit in `method_arg,method_return`, so grouping by position printed
    every one of them twice under an identical glyph strip."""
    paint = _Paint(_color_enabled(color))
    provs = payload["meta"]["providers"]
    cons = payload["meta"]["consumers"]
    out = ["", paint("  CASES   — what was actually verified", "head"),
           paint(f"    glyph groups are ({', '.join(_prov(p) for p in provs)}), "
                 f"one group per consumer: {', '.join(cons)}", "dim"),
           paint("    the middle column is the differential:  = agree   "
                 "≠ differ   ≠* the table DECLARES the divergence",
                 "dim")]
    by_type = _cases_by_type(payload)
    for ty in payload["axis"]["types"]:
        cases = by_type.get(ty)
        if not cases:
            continue
        total = sum(len(c["results"]) for c in cases)
        ok = sum(1 for c in cases for e in c["results"].values()
                 if e["status"] == "pass")
        broken = sum(1 for c in cases for e in c["results"].values()
                     if e["status"] in ("xfail", "fail", "xpass"))
        positions = sorted({p for c in cases for t, p in c["cells"] if t == ty},
                           key=lambda p: (POSITION_ORDER.index(p)
                                          if p in POSITION_ORDER else 99))
        head = (f"  {paint(ty, 'bold')}  — {len(cases)} cases, {ok}/{total} "
                f"cells pass")
        if broken:
            head += paint(f", {broken} known-broken", "xfail")
        head += paint("     positions: " + " ".join(
            POSITION_ABBR.get(p, p) for p in positions), "dim")
        out += ["", head]
        for cv in cases:
            strip = "  ".join(
                "".join(paint(GLYPH.get(cv["results"].get(f"{p}|{c}", {})
                                        .get("status", "not-run"), "?"),
                              cv["results"].get(f"{p}|{c}", {})
                              .get("status", "not-run"))
                        for p in provs)
                for c in cons)
            d = cv["differential"]
            agree = " " if not d else (
                "=" if d.get("agree") else ("\u2260*" if d.get("declared") else "\u2260"))
            pos = _pos_label(cv["cells"], ty)
            ids = (" " + paint("[" + ",".join(cv["known"]) + "]", "xfail")
                   if cv["known"] else "")
            out.append(f"      {strip:<{(len(provs) + 4) * len(cons)}}"
                       f" {paint(agree.ljust(2), 'dim')} {pos:<9} {cv['id']}{ids}")
            # What the case SENT and what the table wanted back. Without it the
            # section titled "what was actually verified" verified an id — a
            # reader learns that `uint/boundary/max` is green and not what
            # `max` was, which is the only fact in the row that a count did not
            # already carry. The HTML has always shown this; the terminal did
            # not, and that made `--report-text` the weaker of the two.
            call = _call_line(cv, 96)
            if call:
                out.append(paint(f"                    {call}", "dim"))
            interesting = cv["rollup"] != "pass" or bool(
                {"hostile", "adversarial"} & set(cv["tags"]))
            if cv["why"] and interesting:
                for i, line in enumerate(_wrap(cv["why"], 74)):
                    label = "why: " if i == 0 else "     "
                    out.append(paint(f"                    {label}{line}", "dim"))
            # Identical outcomes on several surfaces print ONCE, named. Two
            # providers failing the same way is one fact, not two.
            groups: dict[tuple, list[str]] = {}
            for key, e in cv["results"].items():
                if e["status"] == "pass":
                    continue
                sig = ("expected" in e, json.dumps(e.get("expected")),
                       json.dumps(e.get("actual")), e["status"])
                groups.setdefault(sig, []).append(key)
            for (has_exp, exp, act, status), keys in groups.items():
                where = "all surfaces" if len(keys) == len(cv["results"]) \
                    else ", ".join(keys)
                if has_exp:
                    out.append(f"                    expected {_short(json.loads(exp), 76)}")
                out.append(f"                    got      "
                           f"{_short(json.loads(act), 76)}   "
                           f"{paint(f'[{status}] {where}', 'dim')}")
    return out


def _call_line(cv, width: int) -> str:
    """`echoUint(18446744073709551615) -> 18446744073709551615`, on one line.

    The twin of `callHtml` in the page. A green cell has nothing to say beyond
    "it matched", so the values appear once — as the case's own argument and
    its own `expect` — rather than per provider; the per-provider values are
    printed underneath only where something did NOT match.
    """
    if cv.get("args") is not None:
        # Already serialized — `_short` would JSON-encode the encoding and
        # print echoBool("false") for a boolean argument.
        inner = json.dumps(cv["args"], ensure_ascii=False)[1:-1]
        sent = f"{cv['call']}({_clip(inner, width // 2)})"
    elif cv.get("call") or cv.get("fire"):
        sent = f"{cv.get('fire') or cv['call']} {_short(cv.get('value'), width // 2)}"
    else:
        return ""
    # The expectation gets whatever the argument did not use, not a fixed half:
    # `result/err` sends one boolean and answers a three-field record, and an
    # even split cut the record at the `error` field — the only field the case
    # is about.
    room = max(30, width - len(sent))
    if cv.get("expect_by_provider"):
        vals = list(cv["expect_by_provider"].values())
        if all(json.dumps(v, sort_keys=True) == json.dumps(vals[0], sort_keys=True)
               for v in vals):
            # A per-provider expectation whose entries are all the same value
            # is one expectation written N times; printing it N times, each
            # truncated to fit, spends the whole line saying nothing twice.
            want = f"{_short(vals[0], room)}  (declared per provider, identical)"
        elif (cv.get("differential") or {}).get("kind") == "identity":
            # Two values whose ONLY difference is the provider's own name, deep
            # inside a record. Truncated side by side they render identically
            # and the ≠ next to them looks like a lie; the full values are in
            # the differential panel, which is where a divergence belongs.
            want = "each provider answers with its own name — see DIFFERENTIAL"
        else:
            want = "  |  ".join(f"{_prov(p)} {_short(v, room // len(vals) - 8)}"
                                for p, v in cv["expect_by_provider"].items())
    elif cv.get("expect") is not None:
        want = _short(cv["expect"], room)
    else:
        return sent
    return f"{sent} → {want}"


def _cases_by_type(payload) -> dict[str, list]:
    """A case is listed under EVERY type it exercises, not under `case["type"]`.

    `arity/triple` has type `"int,tstr,bstr"`, which is not a type \u2014 it is three
    of them, one per argument slot. Grouping on that string files the case under
    a type nobody declared and drops it from every real one, which is how three
    cases went missing from an earlier draft of this report. The grid already
    resolves them through `cells`; the drill-down has to agree with the grid.
    """
    out: dict[str, list] = {}
    for cv in payload["cases"]:
        for ty in dict.fromkeys(t for t, _ in cv["cells"]):
            out.setdefault(ty, []).append(cv)
    # Re-sorted per block: the payload's global order is keyed on `case["type"]`,
    # which for a multi-argument case is a list of types, so an arity case would
    # otherwise lead the block of every type it touches.
    return {ty: sorted(cs, key=lambda c: (c["tag_rank"], c["id"]))
            for ty, cs in out.items()}


def _pos_label(cells, only_type=None) -> str:
    pos = [p for t, p in cells if only_type is None or t == only_type]
    if pos == ["method_arg", "method_return"]:
        return "arg\u2192ret"
    seen, uniq = set(), []
    for p in pos:
        if p not in seen:
            seen.add(p)
            uniq.append(POSITION_ABBR.get(p, p))
    return ",".join(uniq)


def _short(v, width=44) -> str:
    """Always JSON, including for a bare string.

    This printed a top-level string unquoted, so the integer `5` and the string
    `"5"` rendered as the same three characters \u2014 in a report whose entire
    subject is whether a value keeps its TYPE across a boundary. `hostile/
    {tstr:any}/scalar` really does answer the number 5 and `hostile/[any]/
    scalar` really does answer the string "notalist", and the two lines were
    indistinguishable. Quotes everywhere is noisier and correct.
    """
    return _clip(json.dumps(v, ensure_ascii=False), width)


def _clip(s: str, width: int) -> str:
    """Truncate text that is ALREADY the rendering. Never re-encodes."""
    return s if len(s) <= width else s[:width - 1] + "\u2026"


_SENTENCE_END = (".", ":", ";", "!", "?", '"')


def prose(value):
    """A registry string list is one of two different things, and rendering the
    wrong one is unreadable.

    Some are genuine bullets \u2014 `M4-residual.note` is three self-contained
    paragraphs. Others are a single paragraph hard-wrapped at ~80 columns, with
    `""` marking a blank line; the qml `skip` note and both `coverage_limits`
    notes are written that way. Bulleting those chops mid-sentence ("a LIDL
    `uint` above 2^53 can never be" / "exact once it reaches QML").

    Told apart by how the items END, which is the only signal the JSON carries:
    a wrapped line stops mid-sentence, a bullet does not.
    """
    if isinstance(value, str):
        return "text", [value]
    if not isinstance(value, list):
        return "text", [str(value)]
    items = [str(x) for x in value]
    body = [x for x in items[:-1] if x.strip()]
    wrapped = any(not x for x in items) or (
        body and sum(1 for x in body if not x.rstrip().endswith(_SENTENCE_END))
        > len(body) / 2)
    if not wrapped:
        return "bullets", [x for x in items if x.strip()]
    paras, cur = [], []
    for x in items:
        if not x.strip():
            if cur:
                paras.append(" ".join(cur))
                cur = []
        else:
            cur.append(x.strip())
    if cur:
        paras.append(" ".join(cur))
    return "text", paras


def _prose_lines(value, width, limit=None) -> list[str]:
    kind, parts = prose(value)
    out: list[str] = []
    for p in parts:
        prefix = "* " if kind == "bullets" else ""
        for i, line in enumerate(_wrap(p, width - len(prefix))):
            out.append((prefix if i == 0 else " " * len(prefix)) + line)
        if limit and len(out) >= limit:
            break
    if limit and len(out) > limit:
        out = out[:limit]
        out[-1] = out[-1].rstrip() + " \u2026"
    return out


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines or [""]


def _color_enabled(explicit) -> bool:
    if explicit is not None:
        return explicit
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


# ── markdown ────────────────────────────────────────────────────────────────


def render_markdown(payload) -> str:
    """Regenerates the registry table the conformance README hand-maintains.

    That table currently lists entries the registry has moved to `fixed[]`. A
    suite that ships `check_contract_copies.py` to stop eight copies of a
    contract drifting should not keep a ninth copy of its registry in prose.
    """
    m = payload["meta"]
    out = ["<!-- generated by run_matrix.py --md; do not edit by hand -->",
           "## Current known-broken cells", "",
           f"Measured on consumer(s) `{'`, `'.join(m['consumers'])}` against "
           f"provider(s) `{'`, `'.join(m['providers'])}`. "
           "See `known.json` for the full measurements and evidence.", "",
           "| id | cells in this run | cases | what |",
           "|----|----|----|----|"]
    for e in payload["registry"]["xfail"]:
        used = payload["registry_used"].get(e["id"], [])
        cases = "<br>".join(f"`{c}`" for c in e["cases"])
        out.append(f"| {e['id']} | {len(used)} | {cases} | "
                   f"{_md_cell(e.get('summary', ''))} |")
    if not payload["registry"]["xfail"]:
        out.append("| — | 0 | — | no registered known-broken cells |")
    fixed = payload["registry"]["fixed"]
    if fixed:
        out += ["", "### Closed", "",
                "Kept because it explains why several green cases exist at "
                "all: they are the regression guards a fix left behind.", "",
                "| id | fixed by | what |", "|----|----|----|"]
        for f in fixed:
            out.append(f"| {' / '.join(f['ids'])} | "
                       f"{_md_cell(f.get('fixed_by', ''))} | "
                       f"{_md_cell(f.get('what', ''))} |")
    unm = payload["registry"]["unmeasurable"]
    if unm:
        out += ["", "### Not measurable by this matrix", "",
                "| id | why |", "|----|----|"]
        for u in unm:
            out.append(f"| {u.get('id', '-')} | "
                       f"{_md_cell(u.get('summary', ''))} |")
    out.append("")
    return "\n".join(out)


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


# The keys that are STRUCTURE, not prose: they name cells and surfaces, and the
# view renders them itself. Everything else in a registry entry is prose, and
# there is no fixed schema for it — the four entries in known.json carry four
# different key sets — so the renderer must handle arbitrary names.
STRUCTURAL_KEYS = {"id", "ids", "cases", "providers", "consumers",
                   "per_case_providers", "per_case_consumers", "axis"}


def _prosify(entry):
    """Tag every prose value with how it should be laid out, so the view does
    not have to guess a second time."""
    if not isinstance(entry, dict):
        return entry
    out = {}
    for k, v in entry.items():
        if k not in STRUCTURAL_KEYS and isinstance(v, list) \
                and all(isinstance(x, str) for x in v):
            kind, parts = prose(v)
            out[k] = {"_prose": kind, "parts": parts}
        else:
            out[k] = v
    return out


# ── html ────────────────────────────────────────────────────────────────────


def render_html(payload) -> str:
    # The prose split (bullets vs a hard-wrapped paragraph) is decided here, in
    # python, rather than re-derived in JavaScript: one implementation, and the
    # embedded payload then carries the registry already shaped for reading.
    payload = dict(payload, registry={
        k: [_prosify(e) for e in v] for k, v in payload["registry"].items()})
    data = json.dumps(payload, ensure_ascii=False)
    # Close any literal </script> in the data so it cannot terminate the tag.
    data = data.replace("</", "<\\/")
    title = (f"LIDL conformance matrix \u2014 "
             f"{payload['meta'].get('contract_name') or 'full_api'}")
    return (_HTML_TEMPLATE
            .replace("__TITLE__", _html.escape(title))
            .replace("__DATA__", data))


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --panel2:#0b0f14; --border:#30363d;
    --text:#c9d1d9; --muted:#8b949e; --accent:#58a6ff;
    --pass:#2ea043; --fail:#f85149; --known:#d29922; --xpass:#bc8cff;
    --dim:#6e7681;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  code, .mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  header { position:sticky; top:0; z-index:10; background:var(--panel);
    border-bottom:1px solid var(--border); padding:10px 20px; }
  header .top { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:15px; margin:0; font-weight:600; }
  header .q { color:var(--muted); font-size:12.5px; }
  .pill { padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600;
    border:1px solid transparent; }
  .pill.pass { background:rgba(46,160,67,.15); color:var(--pass); }
  .pill.fail { background:rgba(248,81,73,.15); color:var(--fail); }
  .pill.known { background:rgba(210,153,34,.15); color:var(--known);
    background-image:repeating-linear-gradient(45deg,transparent,transparent 3px,rgba(210,153,34,.18) 3px,rgba(210,153,34,.18) 6px); }
  .pill.dim { background:rgba(110,118,129,.15); color:var(--muted); }
  nav { margin-top:8px; display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  nav a { color:var(--muted); font-size:12px; text-decoration:none; padding:2px 8px;
    border:1px solid var(--border); border-radius:999px; }
  nav a:hover { color:var(--accent); border-color:var(--accent); }
  nav input { background:var(--bg); color:var(--text); border:1px solid var(--border);
    border-radius:6px; padding:3px 8px; font-size:12px; min-width:190px; margin-left:auto; }
  main { max-width:1500px; margin:0 auto; padding:18px 20px 80px; }
  section { margin:34px 0 0; scroll-margin-top:96px; }
  h2 { font-size:16px; margin:0 0 4px; color:#fff; }
  h2 .sub { font-weight:400; font-size:12.5px; color:var(--muted); margin-left:8px; }
  .lede { color:var(--muted); font-size:12.5px; margin:0 0 14px; max-width:90ch; }
  table.grid { border-collapse:collapse; font-size:12.5px; }
  table.grid th { color:var(--muted); font-weight:600; padding:4px 8px; text-align:right;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  table.grid th.ty { text-align:left; }
  table.grid td { padding:2px 8px; text-align:right; border:1px solid transparent;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  table.grid td.ty { text-align:left; color:var(--text); font-weight:600; }
  td.c { cursor:pointer; border-radius:4px; }
  td.c:hover { border-color:var(--accent); }
  td.na { color:var(--dim); cursor:default; }
  td.unc { color:var(--fail); font-weight:700; }
  .ok { color:var(--pass); }
  .kn { color:var(--known); }
  .bad { color:var(--fail); font-weight:700; }
  .xp { color:var(--xpass); font-weight:700; }
  .sk { color:var(--dim); }
  .legend { color:var(--muted); font-size:12px; margin-top:10px; line-height:1.9; }
  .legend b { color:var(--text); font-weight:600; }
  .swatch { display:inline-block; padding:0 6px; border-radius:3px;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  .swatch.kn { background-image:repeating-linear-gradient(45deg,transparent,transparent 3px,rgba(210,153,34,.22) 3px,rgba(210,153,34,.22) 6px); }
  .card { border:1px solid var(--border); border-radius:8px; background:var(--panel);
    padding:12px 16px; margin:10px 0; }
  .card.known { border-left:3px solid var(--known); }
  .card.fail { border-left:3px solid var(--fail); }
  .card h3 { margin:0 0 6px; font-size:13.5px; color:#fff; }
  .card h3 .tag { font-size:11px; color:var(--muted); font-weight:400; margin-left:8px; }
  .kv { display:grid; grid-template-columns:max-content 1fr; gap:2px 14px;
    font-size:12.5px; margin-top:8px; }
  .kv dt { color:var(--muted); font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-size:11.5px; white-space:nowrap; }
  .kv dd { margin:0; max-width:100ch; }
  .kv dd ul { margin:0; padding-left:18px; }
  .kv dd li { margin-bottom:4px; }
  .typeblock { border:1px solid var(--border); border-radius:8px; margin:10px 0;
    overflow:hidden; }
  .typehead { background:var(--panel); padding:8px 14px; display:flex; gap:14px;
    align-items:baseline; flex-wrap:wrap; cursor:pointer; }
  .typehead .name { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-weight:700; color:#fff; font-size:13.5px; }
  .typehead .meta { color:var(--muted); font-size:12px; }
  .typehead .pos { color:var(--dim); font-size:11.5px; margin-left:auto;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  table.cases { width:100%; border-collapse:collapse; font-size:12.5px; }
  table.cases td { padding:5px 10px; border-top:1px solid var(--border);
    vertical-align:top; }
  table.cases tr.c:hover { background:rgba(88,166,255,.05); }
  td.strip { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    white-space:nowrap; width:1%; letter-spacing:1px; }
  td.strip .g { padding:0 1px; }
  td.pos { color:var(--muted); font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    white-space:nowrap; width:1%; }
  td.diff { color:var(--dim); text-align:center; width:1%; }
  td.diff.ne { color:var(--fail); font-weight:700; }
  td.cid { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  td.cid .why { display:block; color:var(--muted); font-size:12px; margin-top:3px;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    max-width:95ch; }
  td.cid .call { display:block; color:var(--dim); font-size:11.5px; margin-top:2px; }
  td.cid .call .arrow { color:var(--muted); }
  pre.val .where { color:var(--dim); font-weight:400; }
  .kv a, .card a { color:var(--accent); text-decoration:none; }
  .kv a:hover, .card a:hover { text-decoration:underline; }
  .kv dd p { margin:0 0 6px; }
  .typeblock.folded table.cases { display:none; }
  .chip { font-size:11.5px; color:var(--muted); border:1px solid var(--border);
    border-radius:999px; padding:2px 9px; cursor:pointer; background:none; }
  .chip.on { color:var(--accent); border-color:var(--accent); }
  .kid { font-size:11px; padding:1px 6px; border-radius:4px; color:var(--known);
    background-image:repeating-linear-gradient(45deg,transparent,transparent 3px,rgba(210,153,34,.22) 3px,rgba(210,153,34,.22) 6px);
    border:1px solid rgba(210,153,34,.4); margin-left:6px; }
  .tagchip { font-size:10.5px; color:var(--dim); border:1px solid var(--border);
    border-radius:3px; padding:0 4px; margin-left:5px; }
  pre.val { background:var(--panel2); border:1px solid var(--border); border-radius:5px;
    padding:6px 9px; margin:5px 0 0; font-size:11.5px; overflow-x:auto;
    white-space:pre-wrap; word-break:break-all; color:#b9c1ca; }
  pre.val b { color:var(--muted); font-weight:600; }
  details summary { cursor:pointer; color:var(--accent); font-size:12px; }
  details[open] summary { margin-bottom:8px; }
  table.flat { width:100%; border-collapse:collapse; font-size:12px; }
  table.flat th { text-align:left; color:var(--muted); font-weight:600;
    border-bottom:1px solid var(--border); padding:4px 8px; }
  table.flat td { padding:3px 8px; border-top:1px solid var(--border);
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  .hide { display:none !important; }
  .foot { color:var(--dim); font-size:11.5px; margin-top:40px;
    border-top:1px solid var(--border); padding-top:12px; }
</style>
</head>
<body>
<header>
  <div class="top">
    <h1>LIDL conformance matrix</h1>
    <span class="q">does a value of type <b>T</b>, in position <b>P</b>, survive provider <b>R</b> &rarr; consumer <b>K</b> intact?</span>
    <span id="pills" style="margin-left:auto"></span>
  </div>
  <nav>
    <a href="#grid">grid</a><a href="#differential">differential</a>
    <a href="#cases">cases</a><a href="#register">known-broken</a>
    <a href="#notmeasured">not measured</a><a href="#history">history</a>
    <a href="#all">all cells</a>
    <span style="width:14px"></span>
    <button class="chip on" data-f="all">all cases</button>
    <button class="chip" data-f="notgreen">not plain pass</button>
    <button class="chip" data-f="known">known-broken</button>
    <button class="chip" data-f="hostile">hostile &amp; adversarial</button>
    <input id="q" type="search" placeholder="filter cases (type, id, tag)&hellip;">
  </nav>
</header>
<main id="main"></main>
<script id="report-data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("report-data").textContent);
const esc = s => String(s === undefined || s === null ? "" : s)
  .replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const GLYPH = {pass:"\u00b7", xfail:"K", fail:"X", xpass:"^", skip:"~", "not-run":"?"};
const CLS   = {pass:"ok", xfail:"kn", fail:"bad", xpass:"xp", skip:"sk", "not-run":"bad"};
const ABBR  = {method_arg:"arg", "method_arg@0":"a0", "method_arg@1":"a1",
  "method_arg@2":"a2", method_return:"ret", event_param:"evt",
  "event_param@0":"e0", "event_param@1":"e1", "event_param@2":"e2",
  nested_in_container:"nest"};
/* There is deliberately NO value-to-text helper here. Every measured value is
   rendered in python (_render) and reaches this page as a string, because
   JSON.parse resolves numbers to doubles and would silently rewrite
   18446744073709551615 as ...52000 \u2014 the M1/M5/M6 defect, in the report that
   reports it. `clip` shortens text that is already the rendering. */
const clip  = (s, n) => s === undefined || s === null ? ""
  : (s.length > n ? s.slice(0, n-1) + "\u2026" : s);
const surfaces = [];
D.meta.providers.forEach(p => D.meta.consumers.forEach(c => surfaces.push(p + "|" + c)));
const shortProv = p => p.replace(/^test_fullapi_/, "");

/* ── header pills ─────────────────────────────────────────────────────── */
(function () {
  const c = D.counts, n = k => c[k] || 0;
  const bits = [`<span class="pill pass">${n("pass")} pass</span>`];
  if (n("xfail")) bits.push(`<span class="pill known">${n("xfail")} known-broken</span>`);
  if (n("fail")) bits.push(`<span class="pill fail">${n("fail")} FAIL</span>`);
  if (n("xpass")) bits.push(`<span class="pill fail">${n("xpass")} xpass \u2014 registry stale</span>`);
  if (D.uncovered.length) bits.push(`<span class="pill fail">${D.uncovered.length} uncovered</span>`);
  if (n("skip")) bits.push(`<span class="pill dim">${n("skip")} skip</span>`);
  document.getElementById("pills").innerHTML = bits.join(" ");
})();

/* ── grid ─────────────────────────────────────────────────────────────── */
function gridCell(cell) {
  if (cell.state === "n/a") return {html:'<span class="sk">.</span>', cls:"na"};
  if (cell.state === "uncovered") return {html:"--", cls:"unc c"};
  const t = cell.tally, parts = [];
  if (t.pass) parts.push(`<span class="ok">${t.pass}ok</span>`);
  [["fail","X","bad"],["xpass","^","xp"],["xfail","K","kn"],["skip","~","sk"],
   ["not-run","?","bad"]].forEach(([k, sfx, cls]) => {
    if (t[k]) parts.push(`<span class="${cls}">${parts.length?"+":""}${t[k]}${sfx}</span>`);
  });
  return {html: parts.join(""), cls: "c"};
}
function renderGrid() {
  const P = D.axis.positions, T = D.axis.types;
  let h = `<table class="grid"><tr><th class="ty">type</th>` +
    P.map(p => `<th title="${esc(p)}">${esc(ABBR[p] || p)}</th>`).join("") + `</tr>`;
  T.forEach(ty => {
    h += `<tr><td class="ty">${esc(ty)}</td>`;
    P.forEach(p => {
      const cell = D.grid[ty][p], g = gridCell(cell);
      const title = cell.state === "covered"
        ? cell.cases.join("\n")
        : (cell.state === "uncovered"
            ? "declared by the contract, exercised by no case"
            : "no such position for this type");
      h += `<td class="${g.cls}" data-ty="${esc(ty)}" data-pos="${esc(p)}" title="${esc(title)}">${g.html}</td>`;
    });
    h += `</tr>`;
  });
  h += `</table>`;
  const surf = D.meta.providers.length * D.meta.consumers.length;
  h += `<div class="legend">
    <b><span class="ok">n ok</span></b> = n cases green on all ${surf} provider &times; consumer surfaces &nbsp;&nbsp;
    <b><span class="swatch kn">+n K</span></b> = n cases with a registered known-broken cell (never counted as a pass) &nbsp;&nbsp;
    <b><span class="bad">n X</span></b> = failing &nbsp;&nbsp;
    <b><span class="xp">n ^</span></b> = xpass, the registry is stale<br>
    <b><span class="bad">--</span></b> = declared by the contract, <b>no case</b> &nbsp;&nbsp;
    <b><span class="sk">.</span></b> = no such position for this type &nbsp;&nbsp;
    <span class="sk">click any covered cell to jump to its cases</span>`;
  if (D.synthetic.length) {
    h += `<br><span class="sk">covered but not declared by the contract: ` +
      D.synthetic.map(c => esc(c[0] + " @ " + c[1])).join(", ") + `</span>`;
  }
  h += `</div>`;
  return h;
}

/* ── differential ─────────────────────────────────────────────────────── */
function renderDifferential() {
  const d = D.differential;
  let h = "";
  if (d.providers.length < 2) {
    h += `<p class="lede">One provider (<code>${esc(d.providers[0] || "-")}</code>) \u2014
      nothing to compare. This table has no differential column, which is a gap,
      not a design choice.</p>`;
  } else {
    h += `<div class="card"><h3>provider &nbsp;<code>${esc(d.providers[0])}</code>
      vs <code>${esc(d.providers[1])}</code></h3><table class="flat">`;
    const KLABEL = {behaviour: "by behaviour", agree: "no longer diverging",
                    identity: "by identity"};
    d.consumers.forEach(c => {
      const s = d.by_consumer[c];
      /* The breakdown, not just the count: "6 declared divergences" reads as
         six facts about the system, and two of them are a provider spelling
         its own name. */
      const k = {};
      D.diffs.filter(x => x.declared && (x.consumer || c) === c)
        .forEach(x => k[x.kind || "behaviour"] = (k[x.kind || "behaviour"] || 0) + 1);
      const detail = ["behaviour", "agree", "identity"].filter(x => k[x])
        .map(x => `${k[x]} ${KLABEL[x]}`).join("; ");
      h += `<tr><td>on ${esc(c)}</td>
        <td class="ok">${s.agree} agree</td>
        <td class="${s.differ.length ? "bad" : "sk"}">${s.differ.length} differ</td>
        <td class="sk">${s.declared.length ? "+" + s.declared.length +
          " declared, not compared" + (detail ? ` (${esc(detail)})` : "") : ""}</td></tr>`;
    });
    h += `</table></div>`;
  }
  if (d.consumers.length < 2) {
    h += `<div class="card"><h3>consumer <span class="tag">one point</span></h3>
      <p class="lede" style="margin:0">A cell proves a provider agreed with this
      client, not that two consumer surfaces agree with each other. A defect a
      consumer happens to undo is invisible from here.</p></div>`;
  } else {
    h += `<div class="card"><h3>consumer</h3><table class="flat">`;
    for (let i = 0; i < d.consumers.length; i++)
      for (let j = i + 1; j < d.consumers.length; j++)
        h += `<tr><td>${esc(d.consumers[i])} vs ${esc(d.consumers[j])}</td></tr>`;
    h += `</table></div>`;
  }
  const decl = D.diffs.filter(x => x.declared);
  if (decl.length) {
    h += `<h3 style="font-size:13.5px;margin:20px 0 4px">Declared divergences
      <span style="font-weight:400;color:var(--muted);font-size:12px">
      (<code>expect_by_provider</code>) \u2014 what each provider actually answered</span></h3>
      <p class="lede">The driver does not compare these: the divergence <em>is</em>
      the expectation. Shown because a declared divergence that quietly stopped
      diverging is worth seeing, and it carries no verdict and no exit code.</p>
      <table class="flat"><tr><th>case</th><th></th><th>answers</th></tr>`;
    /* Behaviour first, then the ones that stopped diverging, then identity.
       `kind` is decided in python (see _divergence_kind) so the page and the
       terminal cannot classify the same divergence differently. */
    const KIND = {behaviour: 0, agree: 1, identity: 2};
    [...decl].sort((a, b) => (KIND[a.kind] ?? 0) - (KIND[b.kind] ?? 0)).forEach(x => {
      /* One provider per line: the difference between two long values is
         usually deep inside them, and a side-by-side truncation hides it.
         When they AGREE there is no difference to place, so the value is
         stated once. */
      /* values_s, not values: pre-rendered in python so a uint64 answer is not
         re-encoded through a double on the way to the screen. */
      const ents = Object.entries(x.values_s || {});
      const vals = x.kind === "agree" && ents.length
        ? `<div><b>both</b> ${esc(clip(ents[0][1], 150))}</div>`
        : ents.map(([p, v]) =>
            `<div><b>${esc(shortProv(p))}</b> ${esc(clip(v, 150))}</div>`).join("");
      /* "Both providers now answer the same" is not good news and is not
         green: it means the per-provider split in the table is vestigial.
         Same shape of finding as an xpass, same colour. */
      const verdict = x.kind === "agree"
        ? `<span class="xp">NO LONGER DIVERGES</span>
           <div class="sk" style="font-size:11.5px">the declaration is vestigial</div>`
        : x.kind === "identity"
        ? `<span class="sk">DIFFER</span>
           <div class="sk" style="font-size:11.5px">by identity — true by construction</div>`
        : `<span class="kn">DIFFER</span>
           <div class="sk" style="font-size:11.5px">by behaviour</div>`;
      h += `<tr><td>${esc(x.case)}</td><td>${verdict}</td><td>${vals}</td></tr>`;
    });
    h += `</table>`;
  }
  return h;
}

/* ── cases ────────────────────────────────────────────────────────────── */
function stripHtml(cv) {
  return D.meta.consumers.map(c =>
    `<span class="g">` + D.meta.providers.map(p => {
      const e = cv.results[p + "|" + c] || {status: "not-run"};
      return `<span class="${CLS[e.status] || ""}" title="${esc(p + " / " + c + " \u2014 " + e.status)}">${GLYPH[e.status] || "?"}</span>`;
    }).join("") + `</span>`).join(" ");
}
function posLabel(cells, onlyType) {
  const p = cells.filter(c => !onlyType || c[0] === onlyType).map(c => c[1]);
  if (p.length === 2 && p[0] === "method_arg" && p[1] === "method_return") return "arg\u2192ret";
  return [...new Set(p)].map(x => ABBR[x] || x).join(",");
}
/* What the case sent and what the table expects, on ONE dim line. A green cell
   has nothing more to say than "it matched", and several hundred identical
   "it matched" blobs are what makes a report unreadable. Values appear in full
   only where something did not match. */
function callHtml(cv) {
  /* Every value here comes PRE-RENDERED from python (cv.render). JSON.parse
     puts numbers in doubles, so reading cv.args/cv.expect would print
     18446744073709552000 for the case that exists to prove
     18446744073709551615 survives. clip only — never re-encode. */
  const r = cv.render || {};
  if (!r.sent) return "";
  const sent = esc(clip(r.sent, 100));
  let want = "";
  if (r.want_kind === "identical")
    /* One expectation written N times is not N expectations. */
    want = `${esc(clip(r.want, 90))} <span class="where">(declared per provider, identical)</span>`;
  else if (r.want_kind === "identity")
    /* Values differing only by the provider's own name render identically
       once truncated; the full pair lives in the differential panel. */
    want = `<span class="where">each provider answers with its own name &mdash;
       see <a href="#differential">differential</a></span>`;
  else if (r.want_kind === "per_provider")
    want = (r.want_pairs || []).map(([p, v]) =>
      `${esc(p)} ${esc(clip(v, 46))}`).join(" &nbsp;|&nbsp; ");
  else if (r.want_kind === "one")
    want = esc(clip(r.want, 90));
  return `<span class="call">${sent}${want ? ` <span class="arrow">&rarr;</span> ${want}` : ""}</span>`;
}
function valuesHtml(cv) {
  /* Identical outcomes on several surfaces print once, named: two providers
     failing the same way is one fact, not two. */
  const groups = new Map();
  Object.entries(cv.results).forEach(([k, e]) => {
    if (e.status === "pass") return;
    /* Grouped and displayed on the PRE-RENDERED text, for the same reason
       callHtml is: two values that differ only above 2^53 are one value once
       JSON.parse has been through them. */
    const sig = JSON.stringify([e.status, e.expected_s === undefined, e.expected_s, e.actual_s]);
    (groups.get(sig) || groups.set(sig, {e, keys: []}).get(sig)).keys.push(k);
  });
  let h = "";
  groups.forEach(({e, keys}) => {
    const where = keys.length === Object.keys(cv.results).length
      ? "all surfaces" : keys.join(", ");
    let t = `<b>${esc(e.status)}</b> <span class="where">${esc(where)}</span>`;
    if (e.expected_s !== undefined) t += `\n<b>expected</b> ${esc(clip(e.expected_s, 300))}`;
    t += `\n<b>got     </b> ${esc(clip(e.actual_s, 300))}`;
    h += `<pre class="val">${t}</pre>`;
  });
  return h;
}
function renderCases() {
  /* A case is listed under EVERY type it exercises: `arity/triple` has type
     "int,tstr,bstr", which is three types, one per slot — not one. */
  const byType = {}, anchored = new Set();
  D.cases.forEach(cv => [...new Set(cv.cells.map(c => c[0]))].forEach(ty =>
    (byType[ty] = byType[ty] || []).push(cv)));
  /* re-sorted per block: the payload's global order is keyed on case.type, which
     for a multi-argument case is a LIST of types */
  Object.values(byType).forEach(cs => cs.sort((a, b) =>
    a.tag_rank - b.tag_rank || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)));
  let h = `<p class="lede">Glyph groups are
    (<code>${D.meta.providers.map(shortProv).map(esc).join(", ")}</code>), one group per
    consumer: <code>${D.meta.consumers.map(esc).join(", ")}</code> &mdash;
    <span class="ok">&middot;</span> pass,
    <span class="kn">K</span> registered known-broken,
    <span class="bad">X</span> fail, <span class="xp">^</span> xpass.
    The middle column is the differential: <b>=</b> the two providers answer identically,
    <b class="bad">&ne;</b> they do not, <b>&ne;*</b> the table declares the divergence.
    Cases are ordered by what they are for: round-trip, boundary, empty, container,
    arity, hostile, adversarial. Click a type to collapse it.</p>`;
  D.axis.types.forEach(ty => {
    const cs = byType[ty];
    if (!cs) return;
    let total = 0, ok = 0, broken = 0;
    cs.forEach(cv => Object.values(cv.results).forEach(e => {
      total++;
      if (e.status === "pass") ok++;
      if (["xfail", "fail", "xpass"].includes(e.status)) broken++;
    }));
    /* Same order as the grid's columns, so a type's position list reads the
       way its row does. Encounter order put `evt` first for `any` purely
       because the event case sorts first inside the block. */
    const positions = D.axis.positions.filter(p => cs.some(c =>
      c.cells.some(x => x[0] === ty && x[1] === p))).map(p => ABBR[p] || p).join(" ");
    h += `<div class="typeblock" data-type="${esc(ty)}">
      <div class="typehead"><span class="name">${esc(ty)}</span>
        <span class="meta">${cs.length} cases &middot; ${ok}/${total} cells pass` +
      (broken ? ` &middot; <span class="kn">${broken} known-broken</span>` : "") +
      `</span><span class="pos">${esc(positions)}</span></div>
      <table class="cases">`;
    cs.forEach(cv => {
      const d = cv.differential;
      const agree = d ? (d.agree ? "=" : (d.declared ? "\u2260*" : "\u2260")) : "";
      const kids = cv.known.map(k =>
        `<a class="kid" href="#reg-${esc(k)}">${esc(k)}</a>`).join("");
      const tags = cv.tags.map(t => `<span class="tagchip">${esc(t)}</span>`).join("");
      /* a case listed under three types must not carry the anchor three times */
      const anchor = anchored.has(cv.id) ? "" : ` id="case-${esc(cv.id)}"`;
      anchored.add(cv.id);
      h += `<tr class="c"${anchor} data-case="${esc(cv.id)}"
              data-search="${esc((cv.id + " " + cv.type + " " + cv.tags.join(" ") + " " + (cv.why||"")).toLowerCase())}"
              data-status="${esc(cv.rollup)}" data-ty="${esc(cv.type)}"
              data-pos="${esc(cv.cells.map(c => c[1]).join(" "))}">
        <td class="strip">${stripHtml(cv)}</td>
        <td class="diff ${d && !d.agree && !d.declared ? "ne" : ""}"
            title="do the two providers answer identically?">${agree}</td>
        <td class="pos">${esc(posLabel(cv.cells, ty))}</td>
        <td class="cid">${esc(cv.id)}${kids}${tags}
          ${callHtml(cv)}
          ${cv.why ? `<span class="why">${esc(cv.why)}</span>` : ""}
          ${valuesHtml(cv)}</td></tr>`;
    });
    h += `</table></div>`;
  });
  return h;
}

/* ── registry prose, rendered generically ─────────────────────────────── */
/* A registry entry has no fixed schema \u2014 the four in known.json carry four
   different key sets \u2014 so prose is rendered generically, by shape. */
const SKIP_KEYS = new Set(["id", "ids", "cases", "providers", "consumers",
  "per_case_providers", "per_case_consumers", "axis", "summary"]);
function proseHtml(entry, also) {
  let h = `<dl class="kv">`;
  Object.entries(entry).forEach(([k, v]) => {
    if (SKIP_KEYS.has(k) || (also && also.includes(k))) return;
    if (v === null || v === undefined) return;
    let body;
    if (v && typeof v === "object" && v._prose) {
      body = v._prose === "bullets"
        ? `<ul>${v.parts.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`
        : v.parts.map(x => `<p>${esc(x)}</p>`).join("");
    } else if (Array.isArray(v)) {
      body = `<ul>${v.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;
    } else if (typeof v === "object") {
      body = `<ul>${Object.entries(v).map(([kk, vv]) =>
        `<li><code>${esc(kk)}</code> \u2014 ${esc(vv)}</li>`).join("")}</ul>`;
    } else body = esc(v);
    h += `<dt>${esc(k)}</dt><dd>${body}</dd>`;
  });
  return h + `</dl>`;
}
function renderRegister() {
  if (!D.registry.xfail.length)
    return `<p class="lede">No registered known-broken cells.</p>`;
  let h = `<p class="lede">Every amber cell above resolves to one of these, and the
    entry is the claim behind it \u2014 measured, with evidence, in the providers'
    own <code>known.json</code>. A registered cell is <b>never</b> counted as a pass.</p>`;
  D.registry.xfail.forEach(e => {
    const used = D.registry_used[e.id] || [];
    const tally = {};
    used.forEach(u => tally[u.status] = (tally[u.status] || 0) + 1);
    const breakdown = Object.entries(tally).map(([s, n]) =>
      `<span class="${CLS[s]}">${n} ${esc(s)}</span>`).join(", ");
    const dcount = (D.registry_used_differential[e.id] || []).length;
    h += `<div class="card known" id="reg-${esc(e.id)}">
      <h3>${esc(e.id)}<span class="tag">${used.length} cell(s) in this run${
        breakdown ? " &mdash; " + breakdown : ""}${
        dcount ? ", + " + dcount + " differential" : ""}</span></h3>
      <div>${esc(e.summary || "")}</div>
      <div class="kv" style="margin-top:8px"><dt>cases</dt><dd>` +
      e.cases.map(c => `<a href="#case-${esc(c)}"><code>${esc(c)}</code></a>`).join(", ") +
      `</dd><dt>providers</dt><dd><code>${esc((e.providers||[]).join(", "))}</code></dd>` +
      (e.consumers ? `<dt>consumers</dt><dd><code>${esc(e.consumers.join(", "))}</code></dd>` : "") +
      `</div>` + proseHtml(e) + `</div>`;
  });
  return h;
}
function renderNotMeasured() {
  let h = `<p class="lede">Nothing may be silently absent from the matrix. A cell is
    either covered, explicitly skipped with a typed reason, or registered \u2014 and
    this is where the second and third kinds are visible.</p>`;
  if (D.uncovered.length) {
    h += `<div class="card fail"><h3>Uncovered contract cells</h3><table class="flat">` +
      D.uncovered.map(c => `<tr><td>${esc(c[0])}</td><td>${esc(c[1])}</td></tr>`).join("") +
      `</table></div>`;
  } else {
    h += `<div class="card"><h3>Uncovered contract cells <span class="tag">none</span></h3>
      <p class="lede" style="margin:0">Every <code>(type, position)</code> the
      <code>.lidl</code> declares has at least one case.</p></div>`;
  }
  D.registry.skip.forEach((s, i) => {
    const inert = D.registry_inert_skips.includes(i);
    h += `<div class="card"><h3>skip <span class="tag">${esc((s.consumers||[]).join(", "))}
      &middot; ${esc(s.reason || "")}</span></h3>
      <div class="kv"><dt>cases</dt><dd><code>${esc((s.cases||[]).join(", "))}</code></dd></div>` +
      (inert ? `<p class="lede" style="margin:8px 0 0"><b>This run has no such
        consumer</b>, so the entry produced no cells at all \u2014 "declared unsupported
        on a surface nobody measured" is not the same as "nothing was skipped".</p>` : "") +
      proseHtml(s) + `</div>`;
  });
  D.registry.coverage_limits.forEach(l => {
    h += `<div class="card"><h3>limit \u2014 ${esc(l.axis)} axis
      <span class="tag">${esc(l.status || "open")}</span></h3>` + proseHtml(l) + `</div>`;
  });
  if (D.registry_dead.length) {
    h += `<div class="card known"><h3>Registry entries that matched no cell</h3>
      <p class="lede" style="margin:0 0 6px">These did not fire in this run. Either the
      surface they name is not in it, or the entry is stale. The driver does not fail on
      this \u2014 whether it should is a policy call, and this report only surfaces it.</p>
      <div class="mono">${D.registry_dead.map(esc).join(", ")}</div></div>`;
  }
  return h;
}
function renderHistory() {
  let h = `<p class="lede">Why several green cases exist at all: each is a regression
    guard a fix left behind. Kept verbatim from the registry.</p>`;
  D.registry.fixed.forEach(f => {
    /* `ids` is the fixed-entry schema, but an entry promoted out of xfail may
       still carry the singular `id`. Render either rather than throwing — see
       the section guard below for why one bad entry used to blank the page. */
    const label = Array.isArray(f.ids) ? f.ids.join(" / ") : (f.id || "");
    h += `<details class="card"><summary><b>${esc(label)}</b> \u2014
      ${esc(f.what || "")}</summary>` + proseHtml(f, ["what"]) + `</details>`;
  });
  D.registry.unmeasurable.forEach(u => {
    h += `<details class="card"><summary><b>${esc(u.id)}</b> (not measurable here) \u2014
      ${esc(u.summary || "")}</summary>` + proseHtml(u) + `</details>`;
  });
  return h;
}
/* Every section is rendered behind this. The page is assembled as ONE
   innerHTML assignment, so before this guard a single throw anywhere in any
   section left <main> completely empty — a blank report that looks like "no
   data" rather than "a bug in one panel". Now the failing panel says so and
   the rest of the report still renders. */
function safeSection(name, fn) {
  try { return fn(); }
  catch (e) {
    return `<p class="lede">This section failed to render: <code>${esc(name)}: ${esc(e.message)}</code>.
      The rest of the report is unaffected; the data is intact in the jsonl.</p>`;
  }
}
function renderAll() {
  let h = `<p class="lede">Completeness, reachable but not the landing view: one row per
    (case, provider, consumer) cell, ${D.meta.cell_count} in total.</p>
    <details><summary>show every cell</summary><table class="flat">
    <tr><th>status</th><th>type</th><th>position</th><th>provider</th><th>consumer</th>
    <th>case</th><th>known</th></tr>`;
  D.cases.forEach(cv => surfaces.forEach(k => {
    const e = cv.results[k];
    if (!e) return;
    h += `<tr><td class="${CLS[e.status]}">${esc(e.status)}</td><td>${esc(cv.type)}</td>
      <td>${esc(cv.position)}</td><td>${esc(e.provider)}</td><td>${esc(e.consumer)}</td>
      <td>${esc(cv.id)}</td><td class="kn">${esc(e.known || "")}</td></tr>`;
  }));
  return h + `</table></details>`;
}

/* ── assemble ─────────────────────────────────────────────────────────── */
const m = D.meta;
document.getElementById("main").innerHTML = `
  <p class="lede">Contract <code>${esc(m.contract_name || "full_api")}</code> &middot;
    ${m.case_count} cases &times; ${m.providers.length} provider(s)
    &times; ${m.consumers.length} consumer(s) = ${m.cell_count} cells &middot;
    generated ${esc(m.generated)}<br>
    providers <code>${m.providers.map(esc).join("</code> <code>")}</code> &middot;
    consumers <code>${m.consumers.map(esc).join("</code> <code>")}</code> &middot;
    transport LocalSocket/QtRO</p>
  <section id="grid"><h2>Type &times; position<span class="sub">what this system
    guarantees, per type</span></h2>${safeSection("renderGrid", renderGrid)}</section>
  <section id="differential"><h2>Differential<span class="sub">the answers compared to
    each other, independently of <code>expect</code></span></h2>${safeSection("renderDifferential", renderDifferential)}</section>
  <section id="cases"><h2>Cases<span class="sub">what was actually verified</span></h2>
    ${safeSection("renderCases", renderCases)}</section>
  <section id="register"><h2>Known-broken register<span class="sub">every amber cell,
    and its claim</span></h2>${safeSection("renderRegister", renderRegister)}</section>
  <section id="notmeasured"><h2>Not measured<span class="sub">on the record, not
    silently absent</span></h2>${renderNotMeasured()}</section>
  <section id="history"><h2>Registry history<span class="sub">closed entries, and the
    guards they left behind</span></h2>${safeSection("renderHistory", renderHistory)}</section>
  <section id="all"><h2>All cells</h2>${safeSection("renderAll", renderAll)}</section>
  <div class="foot">Generated by <code>run_matrix.py --report</code>. Case table and
    registry: <code>${esc(m.paths.cases || "")}</code>,
    <code>${esc(m.paths.known || "")}</code>. This report measures nothing; every
    number is a re-arrangement of one the driver produced.</div>`;

/* grid cell -> jump to the type block */
document.querySelectorAll("td.c").forEach(td => td.addEventListener("click", () => {
  const el = document.querySelector(`.typeblock[data-type="${td.dataset.ty}"]`);
  if (!el) return;
  el.classList.remove("folded");
  el.scrollIntoView({behavior: "smooth", block: "start"});
}));

/* a type block folds away, so the type headers alone are a scannable index */
document.querySelectorAll(".typehead").forEach(th =>
  th.addEventListener("click", () => th.parentElement.classList.toggle("folded")));

/* filters: text, plus the four questions worth asking of a case list */
let FILTER = "all";
const KEEP = {
  all: () => true,
  notgreen: tr => tr.dataset.status !== "pass",
  known: tr => tr.dataset.status === "xfail" || !!tr.querySelector(".kid"),
  hostile: tr => /hostile|adversarial/.test(tr.dataset.search),
};
function applyFilters() {
  const q = document.getElementById("q").value.trim().toLowerCase();
  document.querySelectorAll("table.cases tr.c").forEach(tr => {
    const ok = KEEP[FILTER](tr) && (!q || tr.dataset.search.includes(q));
    tr.classList.toggle("hide", !ok);
  });
  document.querySelectorAll(".typeblock").forEach(b => {
    b.classList.toggle("hide", !b.querySelector("tr.c:not(.hide)"));
  });
}
document.getElementById("q").addEventListener("input", applyFilters);
document.querySelectorAll(".chip").forEach(btn => btn.addEventListener("click", () => {
  document.querySelectorAll(".chip").forEach(b => b.classList.remove("on"));
  btn.classList.add("on");
  FILTER = btn.dataset.f;
  applyFilters();
}));

/* deep links: #case-<id> and #reg-<id> land on a row even inside a folded block */
function revealHash() {
  const id = decodeURIComponent(location.hash.slice(1));
  if (!id) return;
  const el = document.getElementById(id);
  if (!el) return;
  const block = el.closest(".typeblock");
  if (block) block.classList.remove("folded");
  el.scrollIntoView({block: "center"});
}
window.addEventListener("hashchange", revealHash);
revealHash();
</script>
</body>
</html>
"""
