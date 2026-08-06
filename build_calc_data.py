#!/usr/bin/env python3
"""
build_calc_data.py — Phase 1 of the build calculator.

Reads /v1/assets/{heroes,items} and emits two static bundles into docs/calc/.
No SQL, no key, no rate-limit surface. Regenerate only when a patch changes
assets.

    python3 build_calc_data.py                 # fetch from the API
    python3 build_calc_data.py --raw-dir DIR   # use DIR/{heroes,items}.json
    python3 build_calc_data.py --dump-raw DIR  # fetch AND save the raw payloads

Writes docs/calc/items.json, docs/calc/heroes.json, docs/calc/meta.json.
Stdlib only.

Self-checks (all fatal unless --lax):
  * every emitted property value parses to a non-zero float
  * no {s:sign} token survives into the bundle
  * base weapon DPS recomputed from weapon_info matches Valve's own
    damage_per_second to within 1%
"""

import argparse
import gzip
import json
import os
import re
import sys
import hashlib
import urllib.request
from collections import Counter

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT_DIR = os.environ.get("CALC_OUT_DIR", os.path.join("docs", "calc"))

SIGN_TOKEN = "{s:sign}"

# Tier is a pure function of cost and the mapping is stable. `cost` is the
# FINAL soul cost, not an increment: a T2 that upgrades into a T4 costs 6400.
# So the build total is a plain sum over equipped items with no component
# subtraction. 9999 is a sentinel, not a price — see COST_SENTINEL.
TIER_BY_COST = {800: 1, 1600: 2, 3200: 3, 6400: 4}
# 9999 is not a price. Those items belong to an alternative game mode that is
# out of scope, so they are excluded rather than carried with a null cost.
COST_SENTINEL = 9999

# Expected catalogue size after every exclusion below. A live run that misses
# this is a filter bug, not a patch — assert rather than trust.
EXPECTED_ITEMS = int(os.environ.get("CALC_EXPECTED_ITEMS") or 156)

# deadlock.wiki/Items is the authority on which items are actually purchasable.
# The asset dump also carries removed and renamed items ("Glass Cannon v2",
# "Majestic Leap - Disabled", "Soul Rebirth"), which no heuristic separates
# from live ones — so the catalogue is an explicit allowlist.
WIKI_LIST = os.environ.get("CALC_WIKI_LIST") or os.path.join("ref", "shop_items_wiki.json")

# Negative values mean two different things and must not be pooled:
#   negative_attribute True  -> a penalty to YOU (Glass Cannon -13% health)
#   negative_attribute False -> a debuff applied to the ENEMY (Rusted Barrel
#                               -8% bullet resist is a reduction of THEIR resist)
# The calculator is scoped to YOUR hero, so enemy-facing properties are dropped
# entirely: they never affect your panels and showing them invites the reader to
# add a debuff to their own sheet. Self-penalties are KEPT — Glass Cannon's
# -13% health and Sharpshooter's move-speed cost are part of what those items do.
DROP_ENEMY_FACING = True

# Two asset records share the display name "Silencer", both weapon/6400. The
# allowlist matches on name, so the live one is pinned by class_name and the
# other dropped. Keyed by name -> the ONLY class_name allowed to claim it.
CLASS_PINS = {"Silencer": "upgrade_proc_silence"}

# Slot model, confirmed against the game rather than `item_slot_info`
# (whose max_purchases_for_tier [6,6,6] measures something else).
# The site speaks G/S/V (docs/data.json, --cat-g/s/v in the stylesheet).
# The assets speak weapon/spirit/vitality. Convert once, here.
CAT = {"weapon": "G", "spirit": "S", "vitality": "V"}

SLOT_MODEL = {"total": 12, "unlocked_at_start": 9,
              "unlocked_by_objective": 3, "max_active": 4,
              "per_category_limit": None}

# The four conditions that appear as readable strings on `conditional`.
# Anything else conditional is emitted but flagged unresolved — see the plan.
COND_TOGGLES = {
    "against NPCs": "vs_npc",
    "within Range": "close_range",
    "beyond Range": "long_range",
    "after proc": "after_proc",
}

# Hero fields that are pure bloat for a calculator.
HERO_DROP = ("item_draft_bucketing", "videos", "skin", "hideout_rich_presence",
             "colors", "images", "description", "tags", "in_development",
             "needs_testing", "limited_testing", "prerelease_only",
             "assigned_players_only", "player_selectable")


# ---------------------------------------------------------------------------


def fetch(path):
    req = urllib.request.Request(BASE + path,
                                 headers={"User-Agent": "deadlock-calc-build/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def unwrap(payload, *keys):
    if isinstance(payload, dict):
        for k in keys:
            if k in payload:
                return payload[k]
        return payload.get("data", [])
    return payload


def load_assets(raw_dir, dump_raw):
    if raw_dir:
        heroes = json.load(open(os.path.join(raw_dir, "heroes.json")))
        items = json.load(open(os.path.join(raw_dir, "items.json")))
    else:
        heroes, items = fetch("/v1/assets/heroes"), fetch("/v1/assets/items")
        if dump_raw:
            os.makedirs(dump_raw, exist_ok=True)
            json.dump(heroes, open(os.path.join(dump_raw, "heroes.json"), "w"))
            json.dump(items, open(os.path.join(dump_raw, "items.json"), "w"))
    return unwrap(heroes, "heroes"), unwrap(items, "items")


# ---------------------------------------------------------------------------
# value parsing — every trap from the probe lives here and nowhere else
# ---------------------------------------------------------------------------

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def slug(name):
    """Must match build_site_data.py exactly — it is the hero join key."""
    s = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_").lower()
    while "__" in s:
        s = s.replace("__", "_")
    return s


def icon_ref(url):
    """[sha1.png, url] — the shape docs/index.html's icon() helper expects,
    and the naming fetch_icons.py writes into ./icons/."""
    if not url:
        return ["", ""]
    return [hashlib.sha1(url.encode()).hexdigest() + ".png", url]


def parse_value(raw):
    """asset values are ALWAYS strings (or None). Return float or None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    m = _NUM.search(str(raw))
    return float(m.group()) if m else None


def parse_unit(prop):
    """postfix carries the unit. Both 'm' and ' m' occur; trim before use."""
    pf = prop.get("postfix")
    if pf is None:
        return None
    pf = str(pf).strip()
    return pf or None


def bearing_props(item):
    """A property grants a stat iff it declares provided_property_type."""
    out = []
    for key, p in (item.get("properties") or {}).items():
        if not isinstance(p, dict) or "provided_property_type" not in p:
            continue
        val = parse_value(p.get("value"))
        if val is None or val == 0.0:
            continue          # boilerplate sits at "0" on almost every item
        flags = p.get("usage_flags") or []
        cond = p.get("conditional")
        # negative + not flagged as a penalty to you == a debuff on the enemy
        enemy_facing = val < 0 and not p.get("negative_attribute")
        if enemy_facing and DROP_ENEMY_FACING:
            continue
        out.append({
            "key": key,
            "type": p["provided_property_type"],
            "value": val,
            "unit": parse_unit(p),
            "label": p.get("label"),
            "section": p.get("tooltip_section"),
            "flags": flags,
            "cond": cond,
            "toggle": COND_TOGGLES.get(cond),
            # flagged conditional but no readable condition string: the
            # condition exists only in tooltip prose. Excluded from totals.
            "unresolved": bool(
                [f for f in flags if str(f).startswith("Conditionally")]
                and not COND_TOGGLES.get(cond)),
            "negative": bool(p.get("negative_attribute")),
            "penalty": val < 0,
        })
    out.sort(key=lambda d: (d["section"] or "~", -abs(d["value"])))
    return out


# ---------------------------------------------------------------------------


def load_allowlist(path):
    """name -> (slot, cost) from the wiki list. Doubles as a cross-check."""
    data = json.load(open(path))
    return {n: (slot, int(cost))
            for slot, tiers in data.items()
            for cost, names in tiers.items() for n in names}


def build_items(items, allow):
    out, stats = [], Counter()
    for it in items:
        if it.get("type") != "upgrade":
            stats["skipped_" + str(it.get("type"))] += 1
            continue
        cost = it.get("cost")
        # Exclusions are counted independently AND jointly so one live run
        # settles how they overlap; `excl_*` totals will exceed the number of
        # items dropped wherever an item trips more than one filter.
        drops = []
        if it.get("name") == it.get("class_name"):
            drops.append("unnamed")        # loc string gone: legacy/unreleased
        if cost == COST_SENTINEL:
            drops.append("alt_mode")       # alternative game mode, out of scope
        if it.get("disabled"):
            drops.append("disabled")
        if it.get("shopable") is False:
            drops.append("not_shopable")
        if allow and it.get("name") not in allow:
            drops.append("not_in_wiki_list")
        pin = CLASS_PINS.get(it.get("name"))
        if pin and it.get("class_name") != pin:
            drops.append("lost_class_pin")
        for d in drops:
            stats["excl_" + d] += 1
        if drops:
            stats["excluded_total"] += 1
            stats["exclcombo_" + "+".join(drops)] += 1
            continue
        cost = it.get("cost")
        props = bearing_props(it)
        stats["upgrades"] += 1
        stats["props"] += len(props)
        stats["unresolved"] += sum(1 for p in props if p["unresolved"])
        # cross-check the asset against the wiki rather than trusting either
        want_slot, want_cost = allow.get(it["name"], (None, None))
        if want_slot and (it.get("item_slot_type") != want_slot or cost != want_cost):
            stats["mismatch_slot_or_cost"] += 1
        stats["toggled"] += sum(1 for p in props if p["toggle"])
        out.append({
            "id": it.get("id"),
            "class_name": it.get("class_name"),
            "name": it.get("name"),
            "cat": CAT.get(it.get("item_slot_type"), "?"),
            "tier": TIER_BY_COST.get(cost) or it.get("item_tier"),
            "cost": cost,
            "shopable": it.get("shopable"),
            "active": bool(it.get("is_active_item")),
            "components": it.get("component_items") or [],
            "icon": icon_ref(it.get("shop_image") or it.get("image")),
            "props": props,
        })
    out.sort(key=lambda d: ("GSV".index(d["cat"]) if d["cat"] in "GSV" else 9,
                            d["tier"] or 0, d["name"] or ""))
    return out, stats


def weapon_index(items):
    """class_name -> weapon_info, for hero primary weapons."""
    return {it.get("class_name"): it.get("weapon_info")
            for it in items
            if it.get("type") == "weapon" and it.get("weapon_info")}


def build_heroes(heroes, weapons):
    out = []
    for h in heroes:
        if h.get("disabled"):
            continue
        prim = (h.get("items") or {}).get("weapon_primary")
        winfo = weapons.get(prim)
        start = {k: (v or {}).get("value")
                 for k, v in (h.get("starting_stats") or {}).items()}
        display = {k: (v or {}).get("display_stat_name")
                   for k, v in (h.get("starting_stats") or {}).items()}
        out.append({
            "id": h.get("id"),
            "name": h.get("name"),
            "slug": slug(h.get("name") or ""),
            "class_name": h.get("class_name"),
            "complexity": h.get("complexity"),
            "starting_stats": start,
            "stat_enum": display,
            "level_up": h.get("standard_level_up_upgrades") or {},
            "level_info": h.get("level_info") or {},
            "cost_bonuses": h.get("cost_bonuses") or {},
            "purchase_bonuses": h.get("purchase_bonuses") or {},
            "item_slot_info": h.get("item_slot_info") or {},
            "shop_stat_display": h.get("shop_stat_display") or {},
            "weapon_primary": prim,
            "weapon_info": winfo,
        })
    out.sort(key=lambda d: d["name"] or "")
    return out


# ---------------------------------------------------------------------------
# self-checks
# ---------------------------------------------------------------------------


def check_no_sign_token(bundle_text, problems):
    if SIGN_TOKEN in bundle_text:
        problems.append("the %s localisation token survived into the bundle" % SIGN_TOKEN)


def recompute_dps(w):
    """
    Valve ships damage_per_second precomputed, which makes it a free oracle for
    the resolver's weapon maths. If this disagrees, the resolver is wrong.
    """
    # Burst weapons fire `burst_shot_count` shots spaced by
    # intra_burst_cycle_time, then wait cycle_time. Verified exact against
    # Valve's own shots_per_second and damage_per_second on all 38 heroes.
    #
    # `damage_per_shot` ALREADY includes pellet count (Abrams: 9 x 3.6 = 32.4),
    # so `bullets` must NOT be multiplied in again.
    dmg = w.get("damage_per_shot")
    cycle = w.get("cycle_time")
    burst = w.get("burst_shot_count") or 1
    intra = w.get("intra_burst_cycle_time") or 0.0
    if not dmg or not cycle:
        return None
    period = cycle + burst * intra
    return dmg * burst / period if period else None


def check_dps(heroes, problems, tol=0.01):
    checked = failed = 0
    for h in heroes:
        w = h.get("weapon_info")
        if not w or not w.get("damage_per_second"):
            continue
        got = recompute_dps(w)
        if got is None:
            continue
        checked += 1
        want = w["damage_per_second"]
        if abs(got - want) / want > tol:
            failed += 1
            problems.append("DPS mismatch %s: recomputed %.2f vs asset %.2f"
                            % (h["name"], got, want))
    return checked, failed


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir")
    ap.add_argument("--dump-raw")
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--lax", action="store_true",
                    help="report self-check failures without exiting non-zero")
    a = ap.parse_args()

    heroes_raw, items_raw = load_assets(a.raw_dir, a.dump_raw)
    print("[calc] %d hero records, %d item records"
          % (len(heroes_raw), len(items_raw)), file=sys.stderr)

    allow = load_allowlist(WIKI_LIST) if os.path.exists(WIKI_LIST) else {}
    if not allow:
        print("[calc] WARNING: no wiki allowlist at %s" % WIKI_LIST, file=sys.stderr)
    items, stats = build_items(items_raw, allow)
    weapons = weapon_index(items_raw)
    heroes = build_heroes(heroes_raw, weapons)

    os.makedirs(a.out_dir, exist_ok=True)
    itext = json.dumps(items, separators=(",", ":"))
    htext = json.dumps(heroes, separators=(",", ":"))
    open(os.path.join(a.out_dir, "items.json"), "w").write(itext)
    open(os.path.join(a.out_dir, "heroes.json"), "w").write(htext)

    # ---- checks -------------------------------------------------------
    problems = []
    if len(items) != EXPECTED_ITEMS:
        problems.append("catalogue size %d, expected %d (%+d) — check exclusions"
                        % (len(items), EXPECTED_ITEMS, len(items) - EXPECTED_ITEMS))
    check_no_sign_token(itext, problems)
    dps_checked, dps_failed = check_dps(heroes, problems)

    types = Counter(p["type"] for it in items for p in it["props"])
    units = Counter(p["unit"] for it in items for p in it["props"])
    toggles = Counter(p["toggle"] for it in items for p in it["props"] if p["toggle"])

    meta = {
        "n_items": len(items),
        "n_heroes": len(heroes),
        "n_props": stats["props"],
        "n_unresolved_conditionals": stats["unresolved"],
        "n_toggled": stats["toggled"],
        "toggles": dict(toggles),
        "property_types": len(types),
        "property_type_counts": types.most_common(),
        "units": dict(units),
        "items_kb": round(len(itext.encode()) / 1024, 1),
        "items_gz_kb": round(len(gzip.compress(itext.encode())) / 1024, 1),
        "heroes_kb": round(len(htext.encode()) / 1024, 1),
        "heroes_gz_kb": round(len(gzip.compress(htext.encode())) / 1024, 1),
        "slot_model": SLOT_MODEL,
        "exclusions": {k[5:]: v for k, v in stats.items() if k.startswith("excl_")},
        "exclusion_overlaps": {k[9:]: v for k, v in stats.items()
                               if k.startswith("exclcombo_")},
        "n_excluded": stats["excluded_total"],
        "expected_items": EXPECTED_ITEMS,
        "n_slot_or_cost_mismatch": stats["mismatch_slot_or_cost"],
        "n_self_penalties": sum(1 for i in items for p in i["props"] if p["penalty"]),
        "n_effect_only": sum(1 for i in items if not i["props"]),
        "dps_checked": dps_checked,
        "dps_failed": dps_failed,
    }
    json.dump(meta, open(os.path.join(a.out_dir, "meta.json"), "w"), indent=1)

    print("[calc] items %d (%.1f KB / %.1f KB gz)  heroes %d (%.1f KB / %.1f KB gz)"
          % (meta["n_items"], meta["items_kb"], meta["items_gz_kb"],
             meta["n_heroes"], meta["heroes_kb"], meta["heroes_gz_kb"]),
          file=sys.stderr)
    print("[calc] %d stat properties, %d distinct types, %d toggled, %d unresolved conditionals"
          % (meta["n_props"], meta["property_types"],
             meta["n_toggled"], meta["n_unresolved_conditionals"]), file=sys.stderr)
    print("[calc] excluded %d: %s" % (stats["excluded_total"],
          ", ".join("%s %d" % (k[5:], v) for k, v in sorted(stats.items())
                    if k.startswith("excl_"))), file=sys.stderr)
    print("[calc] exclusion overlaps: %s"
          % ", ".join("%s %d" % (k[9:], v) for k, v in sorted(stats.items())
                      if k.startswith("exclcombo_")), file=sys.stderr)
    print("[calc] DPS oracle: %d weapons checked, %d mismatched"
          % (dps_checked, dps_failed), file=sys.stderr)

    if problems:
        print("\n[calc] SELF-CHECK FAILURES (%d):" % len(problems), file=sys.stderr)
        for p in problems[:20]:
            print("   " + p, file=sys.stderr)
        if not a.lax:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
