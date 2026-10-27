"""Dashboard device profiles (format: clients/device_profiles/README.md),
derived from an instrument's schema (<prefix>IOC:SCHEMA). A hand-written
clients/device_profiles/<type>.json still wins."""
from __future__ import annotations


def _label(p: dict) -> str:
    base = p.get("label") or p["name"]
    return f"{base} ({p['units']})" if p.get("units") else base


def _control(p: dict) -> dict | None:
    rb = p["name"] + "_RBV" if p.get("rbv") else p["name"]
    base = {"label": _label(p), "write_suffix": p["name"], "readback_suffix": rb,
            **{k: p[k] for k in ("doc", "limits", "precision", "safety") if p.get(k) is not None}}
    d = p["dtype"]
    if d in ("float", "int"):
        return {"type": "setpoint", **base}
    if d == "enum":
        return {"type": "enum", "choices": p["choices"], **base}
    if d == "bool":
        return {"type": "toggle", **base}
    if d == "str":
        return {"type": "text", **base}
    return None  # arrays: written by plans, not by hand


def profile_from_schema(schema: dict) -> dict:
    """Fields = the readable params, controls = the writable params + actions."""
    params = schema.get("params", [])
    fields = [{"suffix": "Connected", "label": "Connected"}]
    overview, trend, controls = [], None, []
    for p in params:
        if p.get("writable"):
            c = _control(p)
            if c:
                controls.append(c)
        if p.get("kind") == "omitted" and not p.get("overview") and not p.get("trend"):
            continue
        shown = p["name"] + "_RBV" if p.get("rbv") else p["name"]
        if p.get("readable", True):
            fields.append({"suffix": shown, "label": _label(p), "precision": p.get("precision"),
                           **({"derived": p["derived"]} if p.get("derived") else {}),
                           **({"numeric": True} if p["dtype"] in ("float", "int", "bool") else {}),
                           **({"doc": p["doc"]} if p.get("doc") else {}),
                           **({"array": True} if p["dtype"] == "array" else {})})
        if p.get("overview"):
            overview.append(shown)
        if p.get("trend") and trend is None:
            trend = shown
    for a in schema.get("actions", []):
        controls.append({"type": "action", "label": a.get("label") or a["name"], "write_suffix": a["name"],
                         **{k: a[k] for k in ("arg", "doc", "safety") if a.get(k) is not None}})
    return {"fields": fields, "overview": overview, "trend": trend, "controls": controls,
            "description": schema.get("description", ""), "generated": True}
