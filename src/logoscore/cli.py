"""A typed CLI over logoscore, generated from `module-info`.

    export LOGOSCORE_CONFIG_DIR=~/.logos-hub/run/kym/cfg   # a running daemon
    python -m logoscore describe kym_core
    python -m logoscore call kym_core createBudget --name Groceries
    python -m logoscore call kym_core addAccount --name Checking --type asset --balance 100
    python -m logoscore completions            # bash completion

Named ``--flags`` are coerced per each parameter's Qt type, so a mismatch is a
clear error rather than a silent no-op. Attach to a running daemon
(``--attach`` / ``LOGOSCORE_CONFIG_DIR``) or own a one-shot (``--modules DIR --load M``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ._reflect import invokable_methods, ordered_args
from ._session import session

SUBCOMMANDS = ["describe", "call", "ls", "completions"]


def _open(a):
    return session(config_dir=a.attach or os.environ.get("LOGOSCORE_CONFIG_DIR"),
                   modules_dir=a.modules, load=a.load, binary=a.binary,
                   env=dict(kv.split("=", 1) for kv in a.env if "=" in kv))


def _flags_to_values(raw, params):
    known = {p["name"] for p in params}
    vals, i = {}, 0
    while i < len(raw):
        tok = raw[i]
        if not tok.startswith("--"):
            raise SystemExit(f"unexpected '{tok}' — use --name value")
        if "=" in tok:
            name, val = tok[2:].split("=", 1)
        else:
            name = tok[2:]
            i += 1
            if i >= len(raw):
                raise SystemExit(f"--{name} needs a value")
            val = raw[i]
        if name not in known:
            sig = ", ".join(f"--{p['name']} <{p.get('type', 'QString')}>" for p in params) or "(none)"
            raise SystemExit(f"no parameter --{name}. Parameters: {sig}")
        vals[name] = val
        i += 1
    return vals


def cmd_ls(client, a):
    for m in client.list_modules(loaded=True):
        print(f"  {m['name']}")


def cmd_describe(client, a):
    methods = sorted(invokable_methods(client, a.module), key=lambda x: x["name"])
    print(f"{a.module} — {len(methods)} methods")
    for m in methods:
        flags = " ".join(f"--{p['name']} <{p.get('type', 'QString')}>" for p in m.get("parameters", []))
        print(f"  {m['name']} {flags}".rstrip())


def cmd_call(client, a):
    methods = {m["name"]: m for m in invokable_methods(client, a.module)}
    if a.method not in methods:
        raise SystemExit(f"{a.module} has no invokable method {a.method!r}")
    params = methods[a.method].get("parameters", [])
    if a.args and not a.args[0].startswith("--"):     # raw positional / JSON passthrough
        result = client.call(a.module, a.method, *a.args)
    else:
        result = client.call(a.module, a.method, *ordered_args(params, _flags_to_values(a.args, params)))
    print(result if isinstance(result, str) else json.dumps(result, indent=2, default=str))


def cmd_complete(client, a):
    """(internal) newline-separated candidates for the bash completion function."""
    what, rest = a.what, a.rest
    if what == "subcommands":
        return print("\n".join(SUBCOMMANDS))
    if what == "modules":
        return print("\n".join(m["name"] for m in client.list_modules(loaded=True)))
    if what == "methods" and rest:
        return print("\n".join(sorted(m["name"] for m in invokable_methods(client, rest[0]))))
    if what == "params" and len(rest) >= 2:
        for m in invokable_methods(client, rest[0]):
            if m["name"] == rest[1]:
                return print("\n".join(f"--{p['name']}" for p in m.get("parameters", [])))


def cmd_completions(_client, _a):
    print(r'''# bash completion for `python -m logoscore` / logoscore-cli. Uses $LOGOSCORE_CONFIG_DIR.
_logoscore_cli() {
  local cur cword words c; cur="${COMP_WORDS[COMP_CWORD]}"; cword=$COMP_CWORD; words=("${COMP_WORDS[@]}")
  c="${words[0]}"
  if [ "$cword" -eq 1 ]; then COMPREPLY=($(compgen -W "$("$c" complete subcommands 2>/dev/null)" -- "$cur")); return; fi
  case "${words[1]}" in
    describe) [ "$cword" -eq 2 ] && COMPREPLY=($(compgen -W "$("$c" complete modules 2>/dev/null)" -- "$cur"));;
    call)
      [ "$cword" -eq 2 ] && { COMPREPLY=($(compgen -W "$("$c" complete modules 2>/dev/null)" -- "$cur")); return; }
      [ "$cword" -eq 3 ] && { COMPREPLY=($(compgen -W "$("$c" complete methods "${words[2]}" 2>/dev/null)" -- "$cur")); return; }
      [ "$cword" -ge 4 ] && { COMPREPLY=($(compgen -W "$("$c" complete params "${words[2]}" "${words[3]}" 2>/dev/null)" -- "$cur")); return; };;
  esac
}
complete -F _logoscore_cli logoscore-cli''')


def build_parser():
    ap = argparse.ArgumentParser(prog="logoscore-cli", description=__doc__.splitlines()[0])
    ap.add_argument("--attach", metavar="CONFIG_DIR", help="attach to a running daemon (or set LOGOSCORE_CONFIG_DIR)")
    ap.add_argument("--modules", metavar="DIR", help="own a one-shot daemon from this modules dir")
    ap.add_argument("--load", action="append", default=[], metavar="MODULE", help="module to load (own mode)")
    ap.add_argument("--binary", default="logoscore")
    ap.add_argument("--env", action="append", default=[], metavar="K=V")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls")
    sp = sub.add_parser("describe"); sp.add_argument("module")
    sp = sub.add_parser("call"); sp.add_argument("module"); sp.add_argument("method"); sp.add_argument("args", nargs=argparse.REMAINDER)
    sub.add_parser("completions")
    sp = sub.add_parser("complete"); sp.add_argument("what"); sp.add_argument("rest", nargs="*")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "completions":
        return cmd_completions(None, a)
    with _open(a) as client:
        {"ls": cmd_ls, "describe": cmd_describe, "call": cmd_call,
         "complete": cmd_complete}[a.cmd](client, a)


if __name__ == "__main__":
    main()
