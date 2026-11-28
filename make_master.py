#!/usr/bin/env python3
"""
Master build image: every hero's 12-slot elite build, EU beside NA, in the
site's grid style. One PNG that fits an image-board upload.

    python3 make_master.py

Reads  docs/data.json, docs/calc/items.json, docs/icons/, docs/index.html
Writes output/images/master_builds.png
       output/master_builds.json   (the builds, from master_builds.js)

THE BUILDS ARE THE SITE'S. master_builds.js runs loadEliteBuild() lifted from
docs/index.html, so every grid is what the calculator loads for that hero and
region. Nothing about build selection lives in this file.

SIZE FIT. 4chan takes PNG/JPG/GIF (no WebP), at most 4 MiB and 10000 px a
side. The largest scale that fits both wins:
  * 256-colour octree palette. The layout is flat UI plus stylised art, so
    indexed PNG beats JPEG q80 by about 2x here at every scale tested.
  * Edge-preserving smoothing (bilateral) on the ICON ART ONLY. The paper
    grain in Valve's item art is high-frequency noise that costs the most
    bytes and carries nothing; text, borders and badges are untouched. Worth
    ~20% at the same scale. Needs opencv; without it the fit just lands a
    step smaller (logged).
  * Scale 1.7 is the 10000 px ceiling for 44 heroes. If a day's builds don't
    fit in MASTER_MAX_BYTES it steps down 0.05 at a time, logging each try.
Lossless recompression (strip chunks, other zlib strategies) was measured at
~0.05% and is not attempted.

Settings (env): MASTER_MAX_BYTES (4 MiB minus 16 KiB), MASTER_MAX_PX (10000),
MASTER_SCALE (start scale, 1.7), MASTER_MIN_SCALE (1.0), MASTER_SMOOTH
("6,28,6" = cv2.bilateralFilter d, sigmaColor, sigmaSpace; "0" turns it off),
MASTER_COLS (4 heroes per row).
"""

import io
import json
import os
import subprocess
import sys
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
ICONS = os.path.join(DOCS, "icons")
OUT_DIR = os.path.join(ROOT, "output")
OUT_PNG = os.path.join(OUT_DIR, "images", "master_builds.png")
OUT_JSON = os.path.join(OUT_DIR, "master_builds.json")


def _env(name, default, cast=float):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default


MAX_BYTES = _env("MASTER_MAX_BYTES", 4 * 2 ** 20 - 16 * 2 ** 10, int)
MAX_PX = _env("MASTER_MAX_PX", 10000, int)
START_SCALE = _env("MASTER_SCALE", 1.7)
MIN_SCALE = _env("MASTER_MIN_SCALE", 1.0)
STEP = 0.05
COLS = _env("MASTER_COLS", 4, int)
_smooth = os.environ.get("MASTER_SMOOTH", "6,28,6")
SMOOTH = None if _smooth.strip() in ("", "0") else tuple(int(v) for v in _smooth.split(","))

Image.MAX_IMAGE_PIXELS = None          # the canvas is ~65 MP by design

# palette, sampled from the site's grid
BG = (17, 19, 25)
PANEL = (23, 26, 33)
PANEL_EDGE = (44, 49, 61)
CARD = (27, 30, 38)
TEXT = (236, 238, 243)
DIM = (140, 148, 165)
BADGE = (28, 31, 40)
BADGE_TEXT = (205, 210, 222)
CAT = {"G": (219, 121, 54), "V": (82, 168, 96), "S": (146, 98, 214)}
REGIONS = [("Europe", "EU", (214, 88, 72)), ("NAmerica", "NA", (64, 168, 112))]


def log(msg):
    print("  [master] " + msg, file=sys.stderr, flush=True)


# ---- fonts: vendored Inter first (fonts/, OFL), then system Inter, then DejaVu
def _font_path(weight):
    for p in (os.path.join(ROOT, "fonts", "Inter-%s.otf" % weight),
              "/usr/share/fonts/opentype/inter/Inter-%s.otf" % weight):
        if os.path.exists(p):
            return p
    log("Inter-%s not found, falling back to DejaVu" % weight)
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % (
        "" if weight == "Medium" else "-Bold")


# ---- icon art, smoothed once per size --------------------------------------
_cv2 = None
if SMOOTH:
    try:
        import cv2 as _cv2
        import numpy as _np
    except ImportError:
        log("opencv not installed: icon smoothing off, the fit will land a "
            "step or two smaller (pip install opencv-python-headless)")
        SMOOTH = None

_icon_cache = {}


def icon(fn, size):
    key = (fn, size)
    if key in _icon_cache:
        return _icon_cache[key]
    path = os.path.join(ICONS, fn) if fn else ""
    im = None
    if os.path.exists(path):
        im = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
        if SMOOTH:
            a = _np.asarray(im)
            rgb = _cv2.bilateralFilter(_np.ascontiguousarray(a[:, :, :3]), *SMOOTH)
            im = Image.fromarray(_np.dstack([rgb, a[:, :, 3]]), "RGBA")
    elif fn:
        log("missing icon %s (fetch_icons.py copies new ones into docs/icons)" % fn)
    _icon_cache[key] = im
    return im


def rounded_mask(w, h, r):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), r, fill=255)
    return m


class Layout:
    """All geometry at one scale. Base units are the 1x prototype's pixels."""

    def __init__(self, scale, n_heroes):
        s = self.s = lambda v: int(round(v * scale))
        self.scale = scale
        self.T, self.G, self.BORDER = s(92), s(9), s(3)
        grid_w, grid_h = 4 * self.T + 3 * self.G, 3 * self.T + 2 * self.G
        self.PANEL_PAD = s(14)
        self.PANEL_W, self.PANEL_H = grid_w + 2 * self.PANEL_PAD, grid_h + 2 * self.PANEL_PAD
        self.LABEL_H, self.HEAD_H = s(46), s(76)
        self.CARD_PAD, self.REGION_GAP = s(22), s(26)
        self.BLOCK_W = 2 * self.PANEL_W + self.REGION_GAP + 2 * self.CARD_PAD
        self.BLOCK_H = self.HEAD_H + self.LABEL_H + self.PANEL_H + 2 * self.CARD_PAD - s(8)
        self.OUTER, self.BLOCK_GAP, self.TITLE_H = s(56), s(28), s(170)
        rows = -(-n_heroes // COLS)
        self.W = 2 * self.OUTER + COLS * self.BLOCK_W + (COLS - 1) * self.BLOCK_GAP
        self.H = (2 * self.OUTER + self.TITLE_H + rows * self.BLOCK_H
                  + (rows - 1) * self.BLOCK_GAP)
        f = lambda w, px: ImageFont.truetype(_font_path(w), s(px))
        self.F_TITLE, self.F_SUB = f("ExtraBold", 64), f("Medium", 26)
        self.F_HERO, self.F_META = f("ExtraBold", 34), f("Medium", 19)
        self.F_REG, self.F_TIER, self.F_LEG = f("ExtraBold", 34), f("Bold", 22), f("Medium", 21)
        self.tile_mask = rounded_mask(self.T - 2 * self.BORDER, self.T - 2 * self.BORDER, s(4))
        self.portrait_mask = rounded_mask(s(58), s(58), s(10))


def draw_tile(img, d, L, x, y, it):
    s, T = L.s, L.T
    d.rounded_rectangle((x, y, x + T - 1, y + T - 1), s(6), fill=CAT.get(it["cat"], DIM))
    art = icon(it["icon"], T - 2 * L.BORDER)
    if art:
        img.paste(art, (x + L.BORDER, y + L.BORDER), L.tile_mask)
    bw, bh = s(28), s(30)
    bx, by = x + T - bw - s(5), y + s(5)
    d.rounded_rectangle((bx, by, bx + bw, by + bh), s(4), fill=BADGE)
    txt = str(it.get("tier", ""))
    tb = d.textbbox((0, 0), txt, font=L.F_TIER)
    d.text((bx + (bw - (tb[2] - tb[0])) / 2 - tb[0], by + (bh - (tb[3] - tb[1])) / 2 - tb[1]),
           txt, font=L.F_TIER, fill=BADGE_TEXT)


def draw_hero(img, d, L, x, y, hero, builds):
    s = L.s
    d.rounded_rectangle((x, y, x + L.BLOCK_W, y + L.BLOCK_H), s(14),
                        fill=CARD, outline=PANEL_EDGE, width=s(2))
    cx, cy = x + L.CARD_PAD, y + L.CARD_PAD
    card = (hero.get("icon") or [None])[0]
    path = os.path.join(ICONS, card) if card else ""
    if os.path.exists(path):
        c = Image.open(path).convert("RGBA")
        side = min(c.size)                         # top square of the card art
        c = c.crop((0, 0, side, side)).resize((s(58), s(58)), Image.LANCZOS)
        img.paste(c, (cx, cy), L.portrait_mask)
    d.text((cx + s(74), cy + s(2)), hero["name"].upper(), font=L.F_HERO, fill=TEXT)
    if hero.get("winrate") is not None:
        d.text((cx + s(76), cy + s(40)), "%.1f%% win rate  ·  #%s"
               % (hero["winrate"], hero.get("winrate_rank", "–")), font=L.F_META, fill=DIM)
    py = cy + L.HEAD_H
    for k, (rg, label, accent) in enumerate(REGIONS):
        px = cx + k * (L.PANEL_W + L.REGION_GAP)
        d.rectangle((px, py + s(2), px + s(4), py + s(38)), fill=accent)
        d.text((px + s(14), py - s(2)), label, font=L.F_REG, fill=TEXT)
        gy = py + L.LABEL_H
        d.rounded_rectangle((px, gy, px + L.PANEL_W, gy + L.PANEL_H), s(8),
                            fill=PANEL, outline=PANEL_EDGE, width=s(2))
        for i, it in enumerate((builds.get(rg) or {}).get(hero["slug"], [])[:12]):
            if it:
                r, c = divmod(i, 4)
                draw_tile(img, d, L, px + L.PANEL_PAD + c * (L.T + L.G),
                          gy + L.PANEL_PAD + r * (L.T + L.G), it)


def render(scale, data, builds):
    heroes = sorted(data["heroes"].values(), key=lambda h: h["name"].lower())
    L = Layout(scale, len(heroes))
    s = L.s
    img = Image.new("RGB", (L.W, L.H), BG)
    d = ImageDraw.Draw(img)
    O = L.OUTER
    d.text((O, O - s(8)), "DEADLOCK  ·  BUILDS", font=L.F_TITLE, fill=TEXT)
    when = datetime.strptime(data["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
    d.text((O + s(2), O + s(74)),
           "Full 12-slot build per hero, from the top 20 builds in each region  ·  data %s"
           % when.strftime("%b %-d, %Y  %H:%M UTC"), font=L.F_SUB, fill=DIM)
    lx, ly = L.W - O, O + s(78)                   # legend, right-aligned
    for label, key in reversed([("Weapon", "G"), ("Vitality", "V"), ("Spirit", "S")]):
        lx -= d.textlength(label, font=L.F_LEG)
        d.text((lx, ly), label, font=L.F_LEG, fill=TEXT)
        lx -= s(34)
        d.rounded_rectangle((lx, ly, lx + s(24), ly + s(24)), s(4), outline=CAT[key], width=s(4))
        lx -= s(36)
    d.text((lx - d.textlength("number = item tier", font=L.F_LEG), ly),
           "number = item tier", font=L.F_LEG, fill=DIM)
    for n, hero in enumerate(heroes):
        r, c = divmod(n, COLS)
        draw_hero(img, d, L, O + c * (L.BLOCK_W + L.BLOCK_GAP),
                  O + L.TITLE_H + r * (L.BLOCK_H + L.BLOCK_GAP), hero, builds)
    return img


def encode(img):
    p = img.quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    b = io.BytesIO()
    p.save(b, "PNG", optimize=True)
    return b.getvalue()


def main():
    subprocess.run(["node", os.path.join(ROOT, "master_builds.js"), OUT_JSON], check=True)
    data = json.load(open(os.path.join(DOCS, "data.json")))
    builds = json.load(open(OUT_JSON))["regions"]
    n = len(data["heroes"])
    if not n:
        raise SystemExit("docs/data.json has no heroes")

    # largest scale under the pixel ceiling, rounded down to the step grid
    scale = START_SCALE
    while scale > MIN_SCALE and max(Layout(scale, n).W, Layout(scale, n).H) > MAX_PX:
        scale = round(scale - STEP, 2)
    log("%d heroes; start %.2fx; limits %d bytes, %d px; smoothing %s"
        % (n, scale, MAX_BYTES, MAX_PX, "x".join(map(str, SMOOTH)) if SMOOTH else "off"))

    while True:
        img = render(scale, data, builds)
        png = encode(img)
        fits = len(png) <= MAX_BYTES and max(img.size) <= MAX_PX
        log("%.2fx  %dx%d  %d bytes  %s" % (scale, img.size[0], img.size[1], len(png),
                                            "fits" if fits else "too big"))
        if fits:
            break
        if scale - STEP < MIN_SCALE - 1e-9:
            raise SystemExit("master image does not fit %d bytes even at %.2fx"
                             % (MAX_BYTES, scale))
        scale = round(scale - STEP, 2)
        _icon_cache.clear()

    os.makedirs(os.path.dirname(OUT_PNG), exist_ok=True)
    with open(OUT_PNG, "wb") as f:
        f.write(png)
    log("wrote %s (%dx%d, %.2f MiB, %.2fx)"
        % (os.path.relpath(OUT_PNG, ROOT), img.size[0], img.size[1], len(png) / 2 ** 20, scale))


if __name__ == "__main__":
    main()
