#!/usr/bin/env python3
"""
probe_calc_data.py — what does /v1/assets give us for a build calculator?

Assets only: /v1/assets/heroes and /v1/assets/items. No key, no SQL budget
(see PROBES.md — the SQL 20/hr cap is untouched by this).

Answers five questions:
  1. hero stat fields   — which numeric stats exist, where they live, ranges
  2. item property shape— what a `properties` entry actually looks like
  3. property vocabulary— distinct provided_property_type values
  4. conditional/scaled — how many items carry conditions or scaling
  5. payload size       — bytes, and bytes of a trimmed calculator payload

Writes ./probe_out/*.json and prints a summary. Stdlib only.

    python3 probe_calc_data.py
"""

import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT = "probe_out"

# Field names are NOT assumed anywhere below. Everything is a census over the
# keys actually present, because the whole point of the probe is that we don't
# know the shape. These regexes only decide what gets *highlighted*.
COND_HINT = re.compile(r"condition|conditional|requires|active|toggle|proc|on_", re.I)
SCALE_HINT = re.compile(r"scale|scaling|per_|growth|curve|ratio", re.I)


def get(path):
    req = urllib.request.Request(BASE + path,
                                 headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = r.read()
    return raw, json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# generic structure census
# ---------------------------------------------------------------------------

def walk(node, path, keys, depth=0, max_depth=8):
    """Record every dotted path, its value types, and sample/range info."""
    if depth > max_depth:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            p = "%s.%s" % (path, k) if path else k
            rec = keys[p]
            rec["n"] += 1
            rec["types"][type(v).__name__] += 1
            if isinstance(v, bool):
                rec["values"][v] += 1
            elif isinstance(v, (int, float)):
                rec["min"] = v if rec["min"] is None else min(rec["min"], v)
                rec["max"] = v if rec["max"] is None else max(rec["max"], v)
            elif isinstance(v, str) and len(v) <= 48:
                rec["values"][v] += 1
            walk(v, p, keys, depth + 1, max_depth)
    elif isinstance(node, list):
        keys[path]["list_len"].append(len(node))
        for v in node[:24]:            # cap: shape, not volume
            walk(v, path + "[]", keys, depth + 1, max_depth)


def new_keys():
    return defaultdict(lambda: {
        "n": 0, "types": Counter(), "values": Counter(),
        "min": None, "max": None, "list_len": [],
    })


def dump_keys(keys, n_records, top_values=8):
    out = {}
    for p, r in sorted(keys.items()):
        e = {
            "seen": r["n"],
            "coverage": round(r["n"] / n_records, 3) if n_records else None,
            "types": dict(r["types"]),
        }
        if r["min"] is not None:
            e["min"], e["max"] = r["min"], r["max"]
        if r["values"]:
            e["distinct"] = len(r["values"])
            e["top"] = r["values"].most_common(top_values)
        if r["list_len"]:
            ll = r["list_len"]
            e["list_len"] = {"min": min(ll), "max": max(ll),
                             "mean": round(sum(ll) / len(ll), 2)}
        out[p] = e
    return out


# ---------------------------------------------------------------------------

def main():
    os.makedirs(OUT, exist_ok=True)
    report = {}

    # ---- fetch --------------------------------------------------------
    print("[probe] fetching assets", file=sys.stderr)
    hraw, heroes = get("/v1/assets/heroes")
    iraw, items = get("/v1/assets/items")
    if isinstance(heroes, dict):
        heroes = heroes.get("data", heroes.get("heroes", []))
    if isinstance(items, dict):
        items = items.get("data", items.get("items", []))
    print("  heroes %d (%.1f KB) / items %d (%.1f KB)"
          % (len(heroes), len(hraw) / 1024, len(items), len(iraw) / 1024),
          file=sys.stderr)

    report["payload"] = {
        "heroes_bytes": len(hraw), "heroes_kb": round(len(hraw) / 1024, 1),
        "items_bytes": len(iraw), "items_kb": round(len(iraw) / 1024, 1),
        "total_kb": round((len(hraw) + len(iraw)) / 1024, 1),
        "n_heroes": len(heroes), "n_items": len(items),
    }

    # ---- 1. hero stat fields ------------------------------------------
    hk = new_keys()
    for h in heroes:
        walk(h, "", hk)
    hero_keys = dump_keys(hk, len(heroes))
    report["hero_numeric_fields"] = {
        p: e for p, e in hero_keys.items()
        if ("int" in e["types"] or "float" in e["types"]) and "min" in e
    }
    json.dump(hero_keys, open(os.path.join(OUT, "hero_keys.json"), "w"), indent=1)
    if heroes:
        json.dump(heroes[0], open(os.path.join(OUT, "hero_sample.json"), "w"), indent=1)

    # ---- 2/3/4. item properties ---------------------------------------
    ik = new_keys()
    for it in items:
        walk(it, "", ik)
    item_keys = dump_keys(ik, len(items))
    json.dump(item_keys, open(os.path.join(OUT, "item_keys.json"), "w"), indent=1)

    # property blocks: any dict-or-list field whose name mentions propert*
    prop_paths = [p for p in item_keys if "propert" in p.lower()]
    report["property_paths"] = {p: item_keys[p] for p in prop_paths}

    # vocabulary of provided_property_type wherever it appears
    vocab = Counter()
    per_item_props = Counter()
    prop_shape = new_keys()

    def harvest(node, in_props=False):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "provided_property_type" and isinstance(v, str):
                    vocab[v] += 1
                if "propert" in k.lower() and isinstance(v, (dict, list)):
                    seq = v.values() if isinstance(v, dict) else v
                    for entry in seq:
                        walk(entry, "", prop_shape)
                    harvest(v, True)
                else:
                    harvest(v, in_props)
        elif isinstance(node, list):
            for v in node:
                harvest(v, in_props)

    for it in items:
        before = sum(vocab.values())
        harvest(it)
        per_item_props[sum(vocab.values()) - before] += 1

    report["provided_property_type"] = {
        "distinct": len(vocab),
        "counts": vocab.most_common(),
    }
    report["property_entry_shape"] = dump_keys(prop_shape, sum(vocab.values()) or 1)
    report["properties_per_item"] = dict(sorted(per_item_props.items()))

    # conditional / scaled census
    cond_fields = {p: e for p, e in item_keys.items() if COND_HINT.search(p)}
    scale_fields = {p: e for p, e in item_keys.items() if SCALE_HINT.search(p)}
    blob = [json.dumps(it) for it in items]
    report["conditional"] = {
        "matching_fields": cond_fields,
        "items_with_condition_text": sum(1 for b in blob if COND_HINT.search(b)),
    }
    report["scaled"] = {
        "matching_fields": scale_fields,
        "items_with_scale_text": sum(1 for b in blob if SCALE_HINT.search(b)),
    }

    # a fat sample item — the one with the most properties — for eyeballing
    fattest = max(items, key=lambda it: len(json.dumps(it))) if items else {}
    json.dump(fattest, open(os.path.join(OUT, "item_sample_largest.json"), "w"), indent=1)

    # ---- 5. trimmed payload estimate ----------------------------------
    keep = ("id", "item_id", "name", "class_name", "cost", "item_cost", "tier",
            "item_slot_type", "slot_type", "type", "shop_image", "image")
    trimmed = []
    for it in items:
        t = {k: it[k] for k in keep if k in it}
        for p in ("properties", "item_properties"):
            if p in it:
                t[p] = it[p]
        trimmed.append(t)
    tb = len(json.dumps(trimmed).encode())
    report["payload"]["trimmed_items_kb"] = round(tb / 1024, 1)
    json.dump(trimmed, open(os.path.join(OUT, "items_trimmed.json"), "w"))

    json.dump(report, open(os.path.join(OUT, "report.json"), "w"), indent=1)

    # ---- summary ------------------------------------------------------
    p = report["payload"]
    print("\n=== PAYLOAD ===")
    print("heroes %.1f KB (%d)   items %.1f KB (%d)   trimmed items %.1f KB"
          % (p["heroes_kb"], p["n_heroes"], p["items_kb"], p["n_items"],
             p["trimmed_items_kb"]))

    print("\n=== HERO NUMERIC FIELDS (%d) ===" % len(report["hero_numeric_fields"]))
    for path, e in list(report["hero_numeric_fields"].items())[:60]:
        print("  %-52s cov %.2f  %s..%s" % (path[:52], e["coverage"], e["min"], e["max"]))

    print("\n=== PROPERTY PATHS ===")
    for path, e in report["property_paths"].items():
        print("  %-52s cov %.2f  %s" % (path[:52], e["coverage"], dict(e["types"])))

    v = report["provided_property_type"]
    print("\n=== provided_property_type: %d distinct ===" % v["distinct"])
    for name, n in v["counts"][:60]:
        print("  %-44s %d" % (name, n))
    if v["distinct"] > 60:
        print("  ... %d more in report.json" % (v["distinct"] - 60))

    print("\n=== PROPERTY ENTRY SHAPE ===")
    for path, e in list(report["property_entry_shape"].items())[:40]:
        print("  %-40s seen %-6d %s" % (path[:40], e["seen"], dict(e["types"])))

    print("\n=== CONDITIONAL / SCALED ===")
    print("  items whose JSON mentions a condition: %d / %d"
          % (report["conditional"]["items_with_condition_text"], p["n_items"]))
    print("  items whose JSON mentions scaling:     %d / %d"
          % (report["scaled"]["items_with_scale_text"], p["n_items"]))
    for label, key in (("condition fields", "conditional"), ("scale fields", "scaled")):
        fields = report[key]["matching_fields"]
        print("  %s (%d):" % (label, len(fields)))
        for path, e in list(fields.items())[:20]:
            print("    %-50s cov %.2f" % (path[:50], e["coverage"]))

    print("\nwrote %s/{report,hero_keys,item_keys,hero_sample,"
          "item_sample_largest,items_trimmed}.json" % OUT)


if __name__ == "__main__":
    main()
