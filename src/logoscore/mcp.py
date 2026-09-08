"""MCP server — expose a daemon's module methods as Model Context Protocol tools.

One typed tool per invokable method (``<module>__<method>``, with a JSON-schema
``inputSchema`` derived from the Qt parameter types), generated from
``module-info``. Add a method to a module and it appears on the next
``tools/list`` — nothing here changes. Any MCP client (Claude, an agent runtime)
then drives the modules with first-class typed tool-calls.

Attach to a running daemon, or own a private one::

    logoscore-mcp --attach ~/.logos-hub/run/kym/cfg
    logoscore-mcp --modules ./modules --load kym_core --load qaku_core

JSON-RPC 2.0 over stdio, no third-party dependencies. MCP client config:
command ``logoscore-mcp``, args e.g. ``["--attach", "<config_dir>"]``.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ._reflect import invokable_methods, json_schema_type, ordered_args
from ._session import session

PROTOCOL_VERSION = "2024-11-05"


def build_tools(client, modules: list[str]):
    """(tools, param_index) — MCP tool defs plus a name→parameters map for calls."""
    tools: list[dict] = []
    param_index: dict[str, list[dict]] = {}
    for module in modules:
        for m in invokable_methods(client, module):
            name = f"{module}__{m['name']}"
            params = m.get("parameters", [])
            param_index[name] = params
            props = {p["name"]: json_schema_type(p.get("type")) for p in params}
            sig = ", ".join(f"{p['name']}: {p.get('type', '')}" for p in params)
            tools.append({
                "name": name,
                "description": f"{module}.{m['name']}({sig}) -> {m.get('returnType', 'void')}",
                "inputSchema": {"type": "object", "properties": props, "required": list(props)},
            })
    return tools, param_index


def _modules(client, explicit: list[str]) -> list[str]:
    if explicit:
        return list(explicit)
    return [m["name"] for m in client.list_modules(loaded=True)]


def serve(client, modules: list[str], out=sys.stdout, inp=sys.stdin) -> None:
    tools, param_index = build_tools(client, modules)

    def send(obj: dict) -> None:
        out.write(json.dumps(obj) + "\n")
        out.flush()

    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "logoscore", "version": "0.1.0"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call":
            params = msg.get("params", {}) or {}
            name, arguments = params.get("name"), (params.get("arguments") or {})
            try:
                if name not in param_index:
                    raise KeyError(f"unknown tool {name!r}")
                module, _, meth = name.partition("__")
                result: Any = client.call(module, meth, *ordered_args(param_index[name], arguments))
                text = result if isinstance(result, str) else json.dumps(result, default=str)
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"content": [{"type": "text", "text": text}]}})
            except Exception as e:
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="logoscore-mcp",
                                 description="Expose a daemon's module methods as MCP tools (stdio).")
    ap.add_argument("--attach", metavar="CONFIG_DIR", help="attach to a running daemon by its config dir")
    ap.add_argument("--modules", metavar="DIR", help="own a private daemon built from this modules dir")
    ap.add_argument("--load", action="append", default=[], metavar="MODULE",
                    help="module to load (repeatable; own mode)")
    ap.add_argument("--module", action="append", default=[], metavar="MODULE",
                    help="expose only these modules (default: all loaded)")
    ap.add_argument("--binary", default="logoscore", help="logoscore binary (default: on PATH)")
    ap.add_argument("--env", action="append", default=[], metavar="K=V",
                    help="environment for an owned daemon (repeatable)")
    a = ap.parse_args(argv)
    if not a.attach and not a.modules:
        ap.error("pass --attach <config_dir> or --modules <dir>")
    env = dict(kv.split("=", 1) for kv in a.env if "=" in kv)
    with session(config_dir=a.attach, modules_dir=a.modules, load=a.load,
                 binary=a.binary, env=env) as client:
        serve(client, _modules(client, a.module))


if __name__ == "__main__":
    main()
