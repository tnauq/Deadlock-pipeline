#!/usr/bin/env python3
"""
Render the tier-list image and one item-frequency image per hero from the CSVs
the pipeline writes. Run after deadlock_pipeline.py; needs Pillow and network
(icons are fetched from the deadlock-api asset bucket).

    python3 make_images.py

Reads  ./output/tierlist.csv, ./output/item_frequency.csv
Writes ./output/images/tierlist.png
       ./output/images/items/<hero>.png   (one per hero)
"""

import csv
import hashlib
import json
import os
import sys
from collections import defaultdict

from PIL import Image, ImageDraw, ImageFont

ICONS_DIR = "icons"
_index = {}
if os.path.exists(os.path.join(ICONS_DIR, "index.json")):
    _index = json.load(open(os.path.join(ICONS_DIR, "index.json")))

OUT = "output"
IMG = os.path.join(OUT, "images")
ITEMS_DIR = os.path.join(IMG, "items")

SNAPSHOT_ORDER = ["4.8k", "9.6k", "14.4k", "20.8k", "postgame"]
TOP_PER_COLUMN = int(os.environ.get("ITEMS_TOP_PER_COLUMN") or 12)

# palette — matches the composition diagram
BG      = (20, 22, 28)
PANEL   = (26, 29, 37)
FRAME   = (46, 52, 65)
TEXT    = (230, 233, 240)
DIM     = (153, 161, 179)
CAT     = {"G": (232, 117, 42), "S": (139, 79, 214), "V": (63, 166, 92), "?": (90, 96, 110)}
TIERCOL = {"S": (223, 90, 90), "A": (224, 145, 60), "B": (120, 160, 70),
           "C": (70, 130, 150), "D": (90, 96, 110)}

_icon_cache = {}


def font(sz, bold=True):
    base = "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else "")
    return ImageFont.truetype(base, sz)


def fetch_icon(url):
    """Read the icon from ./icons/ (populated by fetch_icons.py). No network."""
    if not url:
        return None
    if url in _icon_cache:
        return _icon_cache[url]
    fn = _index.get(url) or (hashlib.sha1(url.encode()).hexdigest() + ".png")
    path = os.path.join(ICONS_DIR, fn)
    im = None
    if os.path.exists(path):
        try:
            im = Image.open(path).convert("RGBA")
        except Exception as e:
            print("  [img] bad icon file %s (%s)" % (fn, e), file=sys.stderr)
    else:
        print("  [img] missing icon %s — run fetch_icons.py" % url.split('/')[-1],
              file=sys.stderr)
    _icon_cache[url] = im
    return im


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def icon_tile(icon, size, tint):
    """Icon centered on a tinted rounded square."""
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    r = int(size * 0.18)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r,
                        fill=tint + (60,), outline=tint + (255,), width=2)
    if icon:
        pad = int(size * 0.12)
        ic = icon.resize((size - 2 * pad, size - 2 * pad), Image.LANCZOS)
        tile.alpha_composite(ic, (pad, pad))
    return tile


# --------------------------------------------------------------------------
# TIER LIST
# --------------------------------------------------------------------------


def build_tierlist():
    rows = [r for r in csv.DictReader(open(os.path.join(OUT, "tierlist.csv")))
            if r["elite_winrate"]]
    for r in rows:
        r["w"] = float(r["elite_winrate"])
    rows.sort(key=lambda r: -r["w"])

    bands = [("S", 59.0, 200), ("A", 57.0, 59.0), ("B", 55.0, 57.0),
             ("C", 53.0, 55.0), ("D", 0, 53.0)]
    tiers = [(name, [r for r in rows if lo <= r["w"] < hi]) for name, lo, hi in bands]

    SZ, GAP, PAD = 96, 14, 28
    LABEL_W = 132
    per_row_max = max(len(m) for _, m in tiers) or 1
    row_h = SZ + GAP * 2 + 16
    W = LABEL_W + PAD * 2 + per_row_max * (SZ + GAP) + GAP
    H = PAD * 3 + 60 + len(tiers) * (row_h + GAP)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((PAD, PAD), "DEADLOCK TIER LIST", font=font(34), fill=TEXT)
    d.text((PAD, PAD + 42), "elite one-trick win rate  ·  NA + EU", font=font(16, False), fill=DIM)

    y = PAD * 2 + 60
    for name, members in tiers:
        rounded(d, [PAD, y, W - PAD, y + row_h], 10, PANEL)
        d.rounded_rectangle([PAD, y, PAD + LABEL_W - GAP, y + row_h], radius=10,
                            fill=TIERCOL[name])
        lf = font(46)
        tb = d.textbbox((0, 0), name, font=lf)
        d.text((PAD + (LABEL_W - GAP) / 2 - (tb[2] - tb[0]) / 2,
                y + row_h / 2 - (tb[3] - tb[1]) / 2 - tb[1]), name, font=lf, fill=(15, 16, 20))

        x = PAD + LABEL_W + GAP
        for r in members:
            tile = icon_tile(fetch_icon(r["icon_url"]), SZ, (60, 66, 80))
            img.paste(tile, (x, y + GAP), tile)
            wf = font(15)
            label = "%.1f" % r["w"]
            tb = d.textbbox((0, 0), label, font=wf)
            bw = tb[2] - tb[0]
            d.rounded_rectangle([x + SZ / 2 - bw / 2 - 5, y + GAP + SZ - 20,
                                 x + SZ / 2 + bw / 2 + 5, y + GAP + SZ - 1],
                                radius=5, fill=(15, 16, 20))
            d.text((x + SZ / 2 - bw / 2, y + GAP + SZ - 19), label, font=wf, fill=TEXT)
            nm = r["hero"][:10]
            nf = font(12, False)
            tb = d.textbbox((0, 0), nm, font=nf)
            d.text((x + SZ / 2 - (tb[2] - tb[0]) / 2, y + GAP + SZ + 3), nm, font=nf, fill=DIM)
            x += SZ + GAP
        y += row_h + GAP

    os.makedirs(IMG, exist_ok=True)
    path = os.path.join(IMG, "tierlist.png")
    img.save(path)
    print("  -> %s (%dx%d)" % (path, W, H), file=sys.stderr)


# --------------------------------------------------------------------------
# ITEM FREQUENCY (one per hero)
# --------------------------------------------------------------------------


def build_item_images():
    rows = list(csv.DictReader(open(os.path.join(OUT, "item_frequency.csv"))))
    by_hero = defaultdict(lambda: defaultdict(list))
    hero_icon = {}
    for r in rows:
        c = int(r["count"]) if r.get("count") else int(r.get("builds_with_item", 0))
        if c < 2:
            continue
        by_hero[r["hero"]][r["snapshot"]].append(
            (c, r["item"], r["category"], r["icon_url"]))
    # hero card icons come from the tier list csv
    for r in csv.DictReader(open(os.path.join(OUT, "tierlist.csv"))):
        hero_icon[r["hero"]] = r.get("icon_url", "")

    os.makedirs(ITEMS_DIR, exist_ok=True)
    for hero, cols in sorted(by_hero.items()):
        _one_hero(hero, cols, hero_icon.get(hero, ""))
    print("  -> %d hero item images in %s" % (len(by_hero), ITEMS_DIR), file=sys.stderr)


def _one_hero(hero, cols, card_url):
    SZ, GAP, PAD = 72, 10, 26
    COL_W = SZ + 104
    HEAD = 92
    rows_max = min(TOP_PER_COLUMN,
                   max((len(cols.get(s, [])) for s in SNAPSHOT_ORDER), default=0)) or 1
    col_h = rows_max * (SZ + GAP) + GAP
    W = PAD * 2 + len(SNAPSHOT_ORDER) * COL_W + GAP
    H = PAD * 2 + HEAD + col_h + 30

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    card = fetch_icon(card_url)
    if card:
        c = card.resize((64, 64), Image.LANCZOS)
        img.paste(c, (PAD, PAD), c)
    d.text((PAD + (78 if card else 0), PAD + 6), hero.upper(), font=font(30), fill=TEXT)
    d.text((PAD + (78 if card else 0), PAD + 44),
           "items held by net worth  ·  count of 20 elite builds", font=font(14, False), fill=DIM)

    x = PAD
    for snap in SNAPSHOT_ORDER:
        items = sorted(cols.get(snap, []), reverse=True)[:TOP_PER_COLUMN]
        hf = font(16)
        tb = d.textbbox((0, 0), snap, font=hf)
        d.text((x + COL_W / 2 - (tb[2] - tb[0]) / 2, PAD + HEAD - 26), snap, font=hf, fill=TEXT)
        d.line([(x + 8, PAD + HEAD - 2), (x + COL_W - 8, PAD + HEAD - 2)], fill=FRAME, width=2)

        y = PAD + HEAD + GAP
        for cnt, name, cat, url in items:
            tile = icon_tile(fetch_icon(url), SZ, CAT.get(cat, CAT["?"]))
            img.paste(tile, (x + 6, y), tile)
            cf = font(22)
            d.text((x + 6 + SZ + 10, y + SZ / 2 - 20), str(cnt), font=cf, fill=TEXT)
            nf = font(11, False)
            nm = name if len(name) <= 16 else name[:15] + "…"
            d.text((x + 6 + SZ + 10, y + SZ / 2 + 6), nm, font=nf, fill=DIM)
            y += SZ + GAP
        x += COL_W

    safe = "".join(ch if ch.isalnum() else "_" for ch in hero).strip("_").lower()
    img.save(os.path.join(ITEMS_DIR, "%s.png" % safe))


if __name__ == "__main__":
    print("[img] tier list", file=sys.stderr)
    build_tierlist()
    print("[img] item frequency images", file=sys.stderr)
    build_item_images()
    print("[img] done, %d icons loaded from disk" % len(_icon_cache), file=sys.stderr)
