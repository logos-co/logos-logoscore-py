"""The conformance report is a design artifact, so its rules get a test.

Not "does it render" — it obviously renders. These pin the four things the
report exists to get right, each of which is a way a matrix report normally
lies:

  * a cell that is green because it PASSES and a cell that is green because it
    is a registered `xfail` must never look alike, and an amber cell must never
    appear without its registry id;
  * the differential's AGREEMENTS must be reported, not only its failures;
  * what is NOT covered must be visible — uncovered contract cells, and a
    registry entry that matched nothing;
  * the layout must survive a second consumer, which this repo's driver cannot
    produce yet but the registry already declares entries for.

The payload here is synthetic on purpose: these are assertions about the VIEW,
and building them from a live run would make them assertions about the system
under test instead.
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "conformance"))

import matrix_report as R  # noqa: E402


def _norm(t: str) -> str:
    return t.replace(" ", "")


PROVIDERS = ["prov_a", "prov_b"]
CONSUMERS = ["py", "qtproxy"]

CASES = {
    "uint/nominal": {"id": "uint/nominal", "type": "uint",
                     "position": "method_arg,method_return", "method": "echoUint",
                     "args": [1], "expect": 1, "tags": ["nominal"]},
    "hostile/uint/negative": {"id": "hostile/uint/negative", "type": "uint",
                              "position": "method_arg", "method": "echoUint",
                              "args": [-1], "expect": {"__error__": "dispatch_failed"},
                              "tags": ["hostile"],
                              "why": "unsigned must stay unsigned"},
    "arity/triple": {"id": "arity/triple", "type": "int,tstr,bstr",
                     "position": "method_arg@0,method_arg@1,method_return",
                     "method": "three", "args": [1, "a", None], "expect": "ok",
                     "tags": ["arity"],
                     "cells": [["int", "method_arg@0"], ["tstr", "method_arg@1"],
                               ["tstr", "method_return"]]},
}


def _rows(status_of):
    out = []
    for cid in CASES:
        for p in PROVIDERS:
            for c in CONSUMERS:
                st = status_of(cid, p, c)
                r = {"type": CASES[cid]["type"], "position": CASES[cid]["position"],
                     "provider": p, "consumer": c, "case": cid, "status": st,
                     "actual": "measured"}
                if st != "pass":
                    r["expected"] = "wanted"
                if st in ("xfail", "xpass"):
                    r["known"] = "K9"
                out.append(r)
    return out


KNOWN = {
    "xfail": [{"id": "K9", "cases": ["hostile/uint/negative"],
               "providers": PROVIDERS, "consumers": CONSUMERS,
               "summary": "a negative reaches a uint slot",
               "note": ["One self-contained paragraph, ending in a full stop.",
                        "And a second one, likewise."],
               "fix_is": "the provider validating its declared type"}],
    "skip": [{"cases": ["bstr/*"], "consumers": ["qml"],
              "reason": "unsupported-by-surface",
              "note": "JavaScript has no byte-string literal"}],
    "coverage_limits": [{"axis": "transport",
                         "note": ["A note hard-wrapped at some column and so",
                                  "continuing mid-sentence onto the next entry,",
                                  "which must not be bulleted."]}],
    "fixed": [{"ids": ["K1"], "fixed_by": "some commit", "what": "an old defect"}],
    "unmeasurable": [{"id": "U1", "summary": "needs a surface nobody drives"}],
}

DECLARED = {("uint", "method_arg"), ("uint", "method_return"),
            ("int", "method_arg@0"), ("tstr", "method_arg@1"),
            ("tstr", "method_return"), ("bool", "method_return")}


def _payload(status_of=lambda cid, p, c: "pass" if cid != "hostile/uint/negative"
             else "xfail", diffs=None):
    return R.build_payload(
        consumer="py", providers=PROVIDERS, provider_dirs={p: "/dev/null" for p in PROVIDERS},
        by_case=CASES, rows=_rows(status_of),
        diffs=diffs if diffs is not None else [
            {"case": "uint/nominal", "consumer": "py", "declared": False, "agree": True,
             "status": "pass", "known": None},
            {"case": "arity/triple", "consumer": "py", "declared": True, "agree": False,
             "values": {"prov_a": 1, "prov_b": 2}},
        ],
        counts={"pass": 8, "xfail": 4}, known=KNOWN, declared_cells=DECLARED,
        paths={"cases": "cases.json", "known": "known.json"},
        table={"contract": "demo"}, norm_type=_norm)


# ── a registered cell must never read as a working one ──────────────────────


def test_xfail_is_never_counted_as_a_pass_in_the_grid():
    grid = _payload()["grid"]
    cell = grid["uint"]["method_arg"]
    assert cell["tally"] == {"pass": 1, "xfail": 1}
    token, _ = R._grid_token(cell)
    # Two separate tokens, not one blended number: an aggregate that averaged a
    # registered defect into a score is the thing the registry exists to stop.
    assert token == "1ok+1K"


def test_an_amber_cell_always_carries_its_registry_id():
    text = "\n".join(R.render_drilldown(_payload(), color=False))
    line = next(l for l in text.splitlines() if "hostile/uint/negative" in l
                and "why" not in l)
    assert "K" in line and "[K9]" in line


def test_a_failing_cell_and_a_registered_one_use_different_glyphs():
    assert len({R.GLYPH["pass"], R.GLYPH["xfail"], R.GLYPH["fail"],
                R.GLYPH["xpass"]}) == 4


def test_the_register_says_how_many_cells_and_in_what_state():
    p = _payload(lambda cid, pr, c: "xpass" if cid == "hostile/uint/negative" else "pass")
    text = "\n".join(R.render_register(p, R._Paint(False)))
    # An entry whose cells have all started passing is stale, and a bare count
    # would read exactly like a live one.
    assert "4 cell(s) in this run (4 xpass)" in text


# ── the differential, agreements included ───────────────────────────────────


def test_agreements_are_reported_not_only_failures():
    text = "\n".join(R.render_differential(_payload(), R._Paint(False)))
    assert "1 agree" in text
    assert "0 differ" in text


def test_a_declared_divergence_is_shown_without_a_verdict():
    p = _payload()
    text = "\n".join(R.render_differential(p, R._Paint(False)))
    assert "DECLARED DIVERGENCES" in text and "arity/triple" in text
    # It must not be counted as either an agreement or a failure.
    assert p["differential"]["by_consumer"]["py"]["agree"] == 1
    assert p["differential"]["by_consumer"]["py"]["differ"] == []


def test_the_case_row_marks_a_declared_divergence_apart_from_a_real_one():
    text = "\n".join(R.render_drilldown(_payload(), color=False))
    rows = [l for l in text.splitlines() if "arity/triple" in l]
    # once per type it exercises: int (slot 0) and tstr (slot 1 and the return)
    assert len(rows) == 2
    assert all("≠*" in r for r in rows)


# ── nothing may be silently absent ──────────────────────────────────────────


def test_a_contract_cell_with_no_case_is_red_in_the_grid():
    p = _payload()
    assert ["bool", "method_return"] in p["uncovered"]
    token, key = R._grid_token(p["grid"]["bool"]["method_return"])
    assert (token, key) == ("--", "fail")


def test_a_registry_entry_that_matched_nothing_is_surfaced():
    p = _payload(lambda cid, pr, c: "pass")   # nothing is registered-broken now
    assert p["registry_dead"] == ["K9"]
    assert "K9" in "\n".join(R.render_not_measured(p, R._Paint(False)))


def test_a_skip_naming_a_consumer_this_run_lacks_is_surfaced():
    p = _payload()
    assert p["registry_inert_skips"] == [0]
    text = "\n".join(R.render_not_measured(p, R._Paint(False)))
    assert "qml" in text and "no such consumer" in text


def test_a_multi_argument_case_is_filed_under_its_real_cells():
    p = _payload()
    # NOT under a type literally named "int,tstr,bstr".
    assert "int,tstr,bstr" not in p["axis"]["types"]
    assert p["grid"]["int"]["method_arg@0"]["cases"] == ["arity/triple"]
    assert p["grid"]["tstr"]["method_arg@1"]["cases"] == ["arity/triple"]


# ── registry prose has two shapes and only one is a bullet list ─────────────


@pytest.mark.parametrize("value,kind", [
    (["A whole sentence.", "And another one."], "bullets"),
    (["a line wrapped at some column and", "continuing here"], "text"),
    (["para one.", "", "para two."], "text"),
    ("just a string", "text"),
])
def test_prose_tells_bullets_from_a_hard_wrapped_paragraph(value, kind):
    assert R.prose(value)[0] == kind


def test_a_wrapped_paragraph_is_rejoined_not_chopped():
    _, parts = R.prose(["a line wrapped at some column and", "continuing here"])
    assert parts == ["a line wrapped at some column and continuing here"]


# ── the layout survives a second consumer ───────────────────────────────────


def test_a_second_consumer_widens_the_glyph_strip_without_changing_the_pivot():
    p = _payload()
    assert p["meta"]["consumers"] == CONSUMERS
    assert p["meta"]["cell_count"] == len(CASES) * len(PROVIDERS) * len(CONSUMERS)
    text = "\n".join(R.render_drilldown(p, color=False))
    row = next(l for l in text.splitlines() if "hostile/uint/negative" in l
               and "why" not in l)
    # one glyph per provider, one GROUP per consumer
    assert "KK  KK" in row


def test_a_case_broken_on_one_consumer_only_does_not_roll_up_as_a_pass():
    p = _payload(lambda cid, pr, c: "fail" if c == "qtproxy" else "pass")
    assert {cv["rollup"] for cv in p["cases"]} == {"fail"}


# ── the html is self-contained and carries the prose ────────────────────────


def test_html_has_no_external_references():
    html = R.render_html(_payload())
    for bad in ("http://", "https://", "//cdn", "<link", "src="):
        assert bad not in html, f"the report must be self-contained; found {bad!r}"


def test_html_embeds_the_case_why_and_the_registry_evidence():
    html = R.render_html(_payload())
    assert "unsigned must stay unsigned" in html          # the case's own `why`
    assert "the provider validating its declared type" in html   # registry fix_is
    assert "a negative reaches a uint slot" in html              # registry summary


def test_the_embedded_payload_cannot_close_its_own_script_tag():
    p = _payload()
    p["registry"]["xfail"][0]["summary"] = "</script><script>alert(1)</script>"
    assert "</script><script>" not in R.render_html(p)


def test_markdown_table_is_generated_from_the_registry_not_from_prose():
    md = R.render_markdown(_payload())
    assert "| K9 | 4 |" in md
    assert "an old defect" in md      # `fixed` entries explain the green guards
    assert "U1" in md


# ── a report about types must not blur types ────────────────────────────────


def test_a_string_and_a_number_do_not_render_alike():
    # The whole subject is whether a value keeps its TYPE across a boundary.
    # `hostile/{tstr:any}/scalar` really answers the number 5 and
    # `hostile/[any]/scalar` really answers the string "notalist"; rendering a
    # top-level string bare made those two lines indistinguishable.
    assert R._short(5) == "5"
    assert R._short("5") == '"5"'
    assert R._short("notalist") == '"notalist"'


def test_the_call_line_does_not_re_encode_its_arguments():
    line = R._call_line({"call": "echoBool", "args": [False], "expect": False,
                         "has_expect": True}, 84)
    assert line == "echoBool(false) → false"      # not echoBool("false")


def test_an_expectation_of_null_is_an_expectation():
    """`"expect": null` is the whole subject of one case, not the absence of one.

    A case_view always CARRIES the `expect` key (build_payload sets it from
    `case.get`), so truthiness cannot tell "expects null" from "has no expect"
    — and a truthiness test rendered failure/D/null-is-a-value, whose entire
    claim is that a legitimate null comes back as a null, as a case with no
    expectation at all. `has_expect` is what separates them.
    """
    assert R._call_line({"call": "echoAny", "args": [None], "expect": None,
                         "has_expect": True}, 84) == "echoAny(null) → null"
    # ...and an event, which really has none, still renders without an arrow.
    assert "→" not in R._call_line(
        {"call": "stringEvent", "fire": "fireStringEvent", "value": "hi",
         "expect": None, "has_expect": False}, 84)


def test_build_payload_carries_has_expect_for_both_shapes():
    payload = R.build_payload(
        consumer="py", providers=["p"], provider_dirs={"p": "/d"},
        by_case={"c/null": {"id": "c/null", "type": "any",
                            "position": "method_return", "method": "echoAny",
                            "args": [None], "expect": None, "tags": []},
                 "c/none": {"id": "c/none", "type": "any",
                            "position": "method_return", "method": "echoAny",
                            "args": [1], "tags": []}},
        rows=[], diffs=[], counts={}, known={"xfail": [], "skip": [], "fixed": [],
                                             "unmeasurable": []},
        declared_cells=None, table={}, norm_type=lambda s: s, paths={})
    by_id = {c["id"]: c for c in payload["cases"]}
    assert by_id["c/null"]["has_expect"] is True
    assert by_id["c/none"]["has_expect"] is False


def test_the_call_line_states_an_identical_per_provider_expectation_once():
    line = R._call_line({"call": "echoBool", "args": [1], "expect_by_provider": {
        "test_fullapi_cpp": {"__error__": "dispatch_failed"},
        "test_fullapi_rust": {"__error__": "dispatch_failed"}}}, 84)
    assert line.count("dispatch_failed") == 1
    assert "identical" in line


def test_every_case_row_says_what_was_sent():
    # "what was actually verified" that names only an id verifies an id: a
    # reader learns `uint/nominal` is green, not what was sent.
    text = "\n".join(R.render_drilldown(_payload(), color=False))
    assert "echoUint(1) → 1" in text
    assert "echoUint(-1) →" in text


# ── a declared divergence is not one kind of thing ──────────────────────────


IDENTITY_DIFF = {"case": "identity/whoami", "consumer": "py", "declared": True,
                 "agree": False, "values": {"prov_a": "prov_a", "prov_b": "prov_b"}}
BEHAVIOUR_DIFF = {"case": "hostile/x", "consumer": "py", "declared": True,
                  "agree": False,
                  "values": {"prov_a": 5, "prov_b": {"__error__": "no"}}}
VESTIGIAL_DIFF = {"case": "hostile/y", "consumer": "py", "declared": True,
                  "agree": True,
                  "values": {"prov_a": {"__error__": "no"},
                             "prov_b": {"__error__": "no"}}}


@pytest.mark.parametrize("diff,kind", [
    (IDENTITY_DIFF, "identity"), (BEHAVIOUR_DIFF, "behaviour"),
    (VESTIGIAL_DIFF, "agree")])
def test_a_declared_divergence_knows_why_it_diverges(diff, kind):
    # Decided from the measured values, not a hand-kept list of case ids, so a
    # new identity case classifies itself.
    assert R._divergence_kind(diff, PROVIDERS) == kind


def test_a_divergence_true_by_construction_does_not_crowd_out_a_real_one():
    p = _payload(diffs=[IDENTITY_DIFF, BEHAVIOUR_DIFF])
    lines = R.render_differential(p, R._Paint(False))
    text = "\n".join(lines)
    body = text[text.index("DECLARED DIVERGENCES"):]
    # The behavioural split leads with its values; the identity one is one
    # summary line and never its own block.
    assert body.index("hostile/x") < body.index("identity/whoami")
    assert "BY IDENTITY" in body
    assert '"prov_a"' not in body        # its values are not reprinted


def test_a_divergence_that_stopped_diverging_is_not_green():
    p = _payload(diffs=[VESTIGIAL_DIFF])
    text = "\n".join(R.render_differential(p, R._Paint(False)))
    # "Both providers now answer the same" is a finding, not good news: the
    # per-provider split in the table has nothing left to say.
    assert "NO LONGER DIVERGES" in text and "vestigial" in text
    assert "AGREE" not in text


# ── the sparse columns must not split the dense ones ────────────────────────


def test_the_grid_keeps_arg_ret_evt_adjacent():
    positions = _payload()["axis"]["positions"]
    slots = [p for p in positions if "@" in p]
    plain = [p for p in positions if "@" not in p]
    # Per-slot columns exist for three multi-argument cases and are ~90% empty;
    # sorted by travel order they sat between `arg` and `ret`.
    assert positions == plain + slots


# ── a registered defect must show its evidence, not only its claim ──────────


def test_the_register_prints_what_the_cell_actually_did():
    text = "\n".join(R.render_register(_payload(), R._Paint(False)))
    assert "expected" in text and "got" in text and "measured" in text
    # Identical outcomes on several surfaces are one fact, said once.
    assert text.count("got      ") == 1


# ── the report must not reproduce the defect it reports ─────────────────────


def test_the_page_never_renders_a_measured_value_through_javascript():
    # JSON.parse resolves every number to a double, so a page that reads
    # cv.expect prints 18446744073709552000 for the case that exists to prove
    # 18446744073709551615 survives — M1/M5/M6, in the report about them.
    import re
    html = R.render_html(_payload())
    # Comments are allowed to NAME the trap; only executable code is checked.
    code = re.sub(r"/\*.*?\*/", "", html[html.index("</script>"):], flags=re.S)
    assert "const short " not in code       # no value-to-text helper in JS
    for js in ("cv.expect", "cv.args", "e.actual)", "e.expected)"):
        assert js not in code


def test_a_uint64_survives_into_the_page_exactly():
    case = {"id": "uint/max", "type": "uint", "position": "method_arg",
            "method": "echoUint", "args": [18446744073709551615],
            "expect": 18446744073709551615, "tags": ["boundary"]}
    r = R._render_call(case)
    assert r["sent"] == "echoUint(18446744073709551615)"
    assert r["want"] == "18446744073709551615"


# ── one page, several contracts, and never one number across them ───────────
# The merged report replaces the two separate pages. It is a VIEW-layer merge:
# it takes the payloads the single-table pages already embed and lays them out
# side by side. The properties below are the ones that make that honest rather
# than convenient.


def _payload_named(contract, counts):
    p = copy.deepcopy(_payload())
    p["meta"]["contract_name"] = contract
    p["counts"] = counts
    return p


def _embedded(html):
    m = re.search(r'<script id="report-data" type="application/json">(.*?)</script>',
                  html, re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_the_single_table_page_still_embeds_a_bare_payload():
    # Not wrapped, not renamed: anything already reading this page — including
    # the merge below — keeps working, and the single-table path is unchanged.
    data = _embedded(R.render_html(_payload()))
    assert "reports" not in data
    assert data["meta"]["contract_name"] == "demo"
    assert data["counts"] == {"pass": 8, "xfail": 4}


def test_a_merged_page_carries_each_payload_unchanged():
    a, b = _payload_named("full_api", {"pass": 8}), _payload_named("ext", {"pass": 3})
    data = _embedded(R.render_merged_html([a, b]))
    assert [r["name"] for r in data["reports"]] == ["full_api", "ext"]
    # Byte-for-byte the object the standalone page shows, so the two views
    # cannot disagree about what was measured.
    for src, r in zip((a, b), data["reports"]):
        assert r["payload"] == _embedded(R.render_html(src))


def test_a_merged_page_never_blends_the_counts():
    a, b = _payload_named("full_api", {"pass": 8}), _payload_named("ext", {"pass": 3})
    data = _embedded(R.render_merged_html([a, b]))
    # Each contract keeps its own counts and there is no total anywhere. A
    # single blended "11 pass" would hide which contract owns a regression,
    # which is worse than the two pages this replaces.
    assert [r["payload"]["counts"] for r in data["reports"]] == [{"pass": 8}, {"pass": 3}]
    assert "counts" not in data
    for key in ("grid", "cases", "differential", "diffs", "registry", "axis"):
        assert key not in data


def test_a_merged_page_computes_no_cross_contract_differential():
    a = _payload_named("full_api", {"pass": 8})
    b = _payload_named("ext", {"pass": 3})
    b["meta"]["providers"] = ["ext_a", "ext_b"]
    b["differential"]["providers"] = ["ext_a", "ext_b"]
    data = _embedded(R.render_merged_html([a, b]))
    # A differential compares the providers OF ONE CONTRACT. The two tables have
    # different providers and different cases, so there is nothing to compare
    # across them — and the page must not invent it.
    assert data["reports"][0]["payload"]["differential"]["providers"] == PROVIDERS
    assert data["reports"][1]["payload"]["differential"]["providers"] == ["ext_a", "ext_b"]
    assert "differential" not in data


def test_each_contract_gets_its_own_id_namespace():
    data = _embedded(R.render_merged_html(
        [_payload_named("full_api", {}), _payload_named("full_api_ext", {})]))
    assert [r["ns"] for r in data["reports"]] == ["full_api", "full_api_ext"]


def test_two_contracts_with_the_same_name_still_get_distinct_namespaces():
    # Colliding ids would make `#case-x` land in whichever contract came first,
    # silently.
    data = _embedded(R.render_merged_html(
        [_payload_named("same", {}), _payload_named("same", {})]))
    ns = [r["ns"] for r in data["reports"]]
    assert len(set(ns)) == 2


def test_every_section_of_a_contract_renders_behind_the_guard():
    # The page is assembled with ONE innerHTML assignment, so an unguarded
    # section that throws leaves <main> empty — a blank report that reads as
    # "no data". This shipped once (renderNotMeasured was the unguarded one),
    # and on a merged page it would take the OTHER contract down too.
    body = R._HTML_TEMPLATE
    sections = re.findall(r'<section id="\$\{esc\(nsid\((.*?)</section>', body, re.S)
    assert len(sections) >= 7
    for s in sections:
        assert "safeSection(" in s, s
    # ...and the panel as a whole, for anything that throws outside a section.
    assert "function safePanel(" in body
    assert "REPORTS.map(safePanel)" in body


def test_the_filter_state_exists_before_anything_activates_a_contract():
    # `let` is in a temporal dead zone until its declaration is evaluated, so an
    # activate() above this block threw inside applyFilters and took every
    # listener after it with it — chips, search and grid jumps all dead on a
    # page that looked perfect.
    body = R._HTML_TEMPLATE
    assert body.index("let FILTER") < body.index("function activate(")
    assert body.index("let FILTER") < body.index("activate(REPORTS[0].ns)")


def test_a_payload_reads_back_from_the_page_it_was_embedded_in(tmp_path):
    p = _payload()
    page = tmp_path / "matrix.html"
    page.write_text(R.render_html(p))
    blob = tmp_path / "payload.json"
    blob.write_text(json.dumps(p, ensure_ascii=False))
    from_page = R.load_payload(page)
    from_json = R.load_payload(blob)
    # The two routes into the merge agree, so a merged page built from HTML says
    # exactly what one built from the driver's --payload says.
    assert R.render_merged_html([from_page]) == R.render_merged_html([from_json])


def test_a_merged_page_is_not_itself_mergeable(tmp_path):
    page = tmp_path / "merged.html"
    page.write_text(R.render_merged_html([_payload()]))
    with pytest.raises(SystemExit):
        R.load_payload(page)


def test_hrefs_are_matched_to_reports_by_position():
    with pytest.raises(SystemExit):
        R.render_merged_html([_payload(), _payload()], hrefs=["./only-one/"])


def test_the_merge_cli_writes_one_page_from_several_reports(tmp_path):
    a, b = tmp_path / "a.html", tmp_path / "b.html"
    a.write_text(R.render_html(_payload_named("full_api", {"pass": 8})))
    b.write_text(R.render_html(_payload_named("full_api_ext", {"pass": 3})))
    out = tmp_path / "merged.html"
    assert R._main(["--merge", str(a), str(b), "--href", "./full/", "./ext/",
                    "-o", str(out)]) == 0
    data = _embedded(out.read_text())
    assert [r["name"] for r in data["reports"]] == ["full_api", "full_api_ext"]
    assert [r["href"] for r in data["reports"]] == ["./full/", "./ext/"]
