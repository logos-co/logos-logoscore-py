"""Shared reflection helpers — turn a module's `module-info` into typed call metadata.

`module-info` returns each `Q_INVOKABLE` method's name, ordered `parameters`
(`{name, type}`) and `returnType`. Both the CLI (``python -m logoscore``) and the
MCP server (:mod:`logoscore.mcp`) build on this so typed args, completion and MCP
tools all track whatever a module actually exposes — no hand-maintained lists.
"""
from __future__ import annotations

import json
from typing import Any

from .client import LogoscoreClient

# Qt/QVariant type names → how a string value should be coerced before it crosses
# the argv boundary. Everything else stays a string. Coercing here (rather than
# letting the CLI type positionally) is what avoids a silent no-op on a mismatch.
_NUM = {"int", "qint32", "qint64", "qlonglong", "qulonglong", "uint", "uint32",
        "uint64", "double", "float", "qreal", "long", "short"}
_BOOL = {"bool"}
_JSON = {"qvariantmap", "qjsonobject", "qvariantlist", "qjsonarray", "qstringlist",
         "qvariant", "qvariantmap<qstring,qvariant>"}


def json_schema_type(qtype: str) -> dict:
    """A JSON-Schema type node for an MCP tool parameter of Qt type ``qtype``."""
    t = (qtype or "").lower()
    if t in _BOOL:
        return {"type": "boolean"}
    if t in _NUM:
        return {"type": "number"}
    if t in _JSON:
        return {"type": "object"}
    return {"type": "string"}


def coerce(value: Any, qtype: str) -> Any:
    """Coerce a CLI/JSON ``value`` to the Python type ``qtype`` implies.

    Numbers become ``int``/``float``, booleans become ``bool``, container/variant
    types are parsed from JSON, everything else is left as a string. The client's
    ``_arg_to_str`` then encodes each by its Python type (``json:``-tagging the
    containers), so the daemon reconstructs the value losslessly.
    """
    if not isinstance(value, str):
        return value  # already typed (e.g. from an MCP client's JSON)
    t = (qtype or "").lower()
    if t in _BOOL:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if t in _NUM:
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError as e:
                raise ValueError(f"{value!r} is not a valid {qtype}") from e
    if t in _JSON:
        try:
            return json.loads(value)
        except Exception as e:
            raise ValueError(f"a {qtype} value must be JSON, got {value!r}") from e
    return value


def invokable_methods(client: LogoscoreClient, module: str) -> list[dict]:
    """The module's invokable methods, each ``{name, parameters, returnType}``."""
    info = client.module_info(module)
    return [m for m in info.get("methods", []) if m.get("isInvokable", True)]


def ordered_args(params: list[dict], values: dict[str, Any]) -> list[Any]:
    """Map a ``{name: value}`` dict onto positional args in declared order,
    coercing each to its parameter type. Missing params are omitted (so the
    module applies its own defaults); an unknown key raises ``KeyError``."""
    known = {p["name"] for p in params}
    for key in values:
        if key not in known:
            sig = ", ".join(f"--{p['name']} <{p.get('type', 'QString')}>" for p in params) or "(none)"
            raise KeyError(f"no parameter {key!r}; parameters: {sig}")
    # Positional args must be a contiguous prefix: once a parameter is omitted,
    # no later one may be provided (that would shift positions). A trailing
    # omitted parameter is fine — the module applies its own default.
    out: list[Any] = []
    gap = False
    for p in params:
        if p["name"] in values:
            if gap:
                raise ValueError(
                    f"{p['name']!r} provided after an omitted earlier parameter")
            out.append(coerce(values[p["name"]], p.get("type", "QString")))
        else:
            gap = True
    return out
