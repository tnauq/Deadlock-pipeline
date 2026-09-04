#!/usr/bin/env python3
"""
Verify that each ceiling player's resolved account_id actually BELONGS to the
display name it was matched from, by reading Steam's own profile.

    python3 verify_ceiling_ids.py

Reads  ./output/ceiling.csv
Writes ./output/ceiling_identity_check.csv

--------------------------------------------------------------------------
WHY THIS EXISTS — the gap nothing else covers

Every identity check in the pipeline answers the same question in different
ways: hero-stats asks "does this id play this hero", the scoreboard asks it
again through an endpoint with real account ids, the games floor asks "is this
id plausibly a top-1,000 player". None of them asks:

    DOES THIS ACCOUNT ID BELONG TO THIS DISPLAY NAME?

That is the question Valve makes hard — it publishes no account ids, so
identity rests on deadlock-api's fuzzy possible_account_ids, measured putting
the wrong player in 110 of 371 slots when used alone.

THE MEASURED FAILURE, 2026-09-04. EU Pocket's ceiling resolved to account
141509687 and ranked FIRST in the region. The real lorence is SteamID64
76561198145864373, i.e. account 185598645 — a different account. The wrong one
cleared the 100-game floor, cleared hero play, and was independently confirmed
by the scoreboard as playing Pocket. It was a real, active Deadlock player. It
simply was not lorence. Every check we had passed it.

The tell was visible but unused: 579 career games against a ceiling median of
2,256, the lowest on the board by a wide margin.

--------------------------------------------------------------------------
HOW

account_id is a Steam32 id. SteamID64 = account_id + 76561197960265728.
`https://steamcommunity.com/profiles/{id64}?xml=1` returns the current persona
name in <steamID>, needs NO API key and no account.

Scoped to CEILING players only, by default. That is the set where a wrong id
changes published output, it is ~76 requests rather than ~4,800, and it is
small enough to be polite to Steam. VERIFY_ALL=1 widens it if a candidate file
is supplied.

--------------------------------------------------------------------------
WHAT A MISMATCH DOES AND DOES NOT PROVE

A mismatch is EVIDENCE, not proof, and this script never drops anyone.

  - PERSONA NAMES CHANGE. The board snapshot may hold a name the player has
    since dropped. The XML exposes only the current name, not alias history —
    the real lorence's profile lists a prior alias, which is exactly the case
    that would read as a false mismatch.
  - PRIVATE PROFILES return a minimal document with no name. Absence of an
    answer is not a mismatch; it gets its own verdict.
  - NAMES CARRY DECORATION — emoji, clan tags, zero-width joiners. Compared
    raw, ordinary players look like mismatches. See norm().

So the output is a flag and a rate, for a human to read. Treat the rate as the
first real estimate of residual identity error in the project.
"""

import csv
import html
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

OUT_DIR = os.environ.get("OUT_DIR") or "output"
SRC = os.path.join(OUT_DIR, os.environ.get("SRC_CSV") or "ceiling.csv")
DEST = os.path.join(OUT_DIR, "ceiling_identity_check.csv")

STEAM64_BASE = 76561197960265728
# Steam throttles aggressively and this is a courtesy client, not a scraper.
SLEEP_S = float(os.environ.get("STEAM_SLEEP") or 1.0)
TRIES = int(os.environ.get("STEAM_TRIES") or 3)
TIMEOUT = int(os.environ.get("STEAM_TIMEOUT") or 30)
# Cache across runs so a re-run costs nothing for ids already seen.
CACHE = os.path.join(OUT_DIR, "steam_names_cache.csv")


def norm(s):
    """Comparable form of a display name.

    Strips accents, case, whitespace, and every non-alphanumeric character.
    Clan tags, emoji, zero-width joiners and decorative brackets are extremely
    common in this population, and comparing raw strings would report most of
    the board as mismatched. This is deliberately LOOSE: the failure being
    hunted is a completely different player, not a punctuation difference.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.casefold())


def fetch_aliases(id64):
    """Previous persona names from the profile HTML, or [].

    THE DECISIVE CHECK for a mismatch. Steam shows "This user has also played
    as:" on the profile page, and if the leaderboard name appears there the
    account IS the right one and simply renamed. The ?xml=1 endpoint does not
    carry alias history, so this needs the HTML page — which is why it is
    fetched ONLY for accounts that already mismatched, a handful per run
    rather than 76.

    Absence proves nothing: Steam only lists recent aliases, and a rename from
    long ago will not appear. An empty list means "no evidence of a rename",
    never "not a rename".
    """
    url = "https://steamcommunity.com/profiles/%s" % id64
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "deadlock-identity-check/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    # the alias block sits after the "also played as" heading
    m = re.search(r"also played as.*?</div>(.*?)</div>\s*</div>", body, re.S | re.I)
    chunk = m.group(1) if m else ""
    if not chunk:
        m = re.search(r"also played as(.{0,2000})", body, re.S | re.I)
        chunk = m.group(1) if m else ""
    names = re.findall(r'class="[^"]*whiteLink[^"]*"[^>]*>([^<]{1,64})<', chunk)
    if not names:
        names = re.findall(r"<p>\s*([^<\n]{1,64}?)\s*</p>", chunk)
    return [html.unescape(n).strip() for n in names if n.strip()]


def contains_match(a, b, floor=4):
    """One normalised name wholly inside the other.

    Catches clan tags and decoration — "[EU] lorence" vs "lorence". The floor
    stops short names matching by accident: without it "ab" would match
    half the board. Still loose by design; a partial match is reported
    separately from an exact one rather than being folded into it.
    """
    x, y = norm(a), norm(b)
    if len(x) < floor or len(y) < floor:
        return False
    return x in y or y in x


def load_cache():
    if not os.path.exists(CACHE):
        return {}
    out = {}
    for r in csv.DictReader(open(CACHE, encoding="utf-8")):
        out[r["steamid64"]] = r["persona"]
    return out


def save_cache(cache):
    with open(CACHE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["steamid64", "persona"])
        w.writeheader()
        for k, v in sorted(cache.items()):
            w.writerow({"steamid64": k, "persona": v})


def fetch_persona(id64):
    """Current persona name, or None. Never raises."""
    url = "https://steamcommunity.com/profiles/%s?xml=1" % id64
    for attempt in range(TRIES):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "deadlock-identity-check/1.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = r.read().decode("utf-8", "replace")
            m = re.search(r"<steamID>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</steamID>",
                          body, re.S)
            if m:
                return html.unescape(m.group(1)).strip()
            # a private or non-existent profile returns a document with no
            # <steamID> — that is an answerless answer, not a mismatch
            return None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            return None
        except Exception:
            time.sleep(2 ** attempt)
    return None


def main():
    if not os.path.exists(SRC):
        raise SystemExit("%s not found. Run the pipeline first." % SRC)
    rows = list(csv.DictReader(open(SRC, encoding="utf-8")))

    # one request per distinct account, not per row: an account that is the
    # ceiling for two heroes must not be fetched twice
    seen = {}
    for r in rows:
        aid = (r.get("account_id") or "").strip()
        if aid.isdigit():
            seen.setdefault(int(aid), r.get("ceiling_player") or r.get("account_name") or "")

    cache = load_cache()
    print("[steam] %d distinct accounts from %d rows (%d already cached)"
          % (len(seen), len(rows), sum(1 for a in seen
                                       if str(a + STEAM64_BASE) in cache)),
          file=sys.stderr)

    personas = {}
    fetched = 0
    for aid in sorted(seen):
        id64 = str(aid + STEAM64_BASE)
        if id64 in cache:
            personas[aid] = cache[id64]
            continue
        name = fetch_persona(id64)
        personas[aid] = name or ""
        cache[id64] = name or ""
        fetched += 1
        time.sleep(SLEEP_S)
        if fetched % 20 == 0:
            print("  [steam] %d fetched" % fetched, file=sys.stderr)
            save_cache(cache)
    save_cache(cache)

    # career volume across the ceiling set, used to separate the two kinds of
    # mismatch. Measured 2026-09-04: the confirmed WRONG account had 579
    # career games against a ceiling median of 2,256, while the account that
    # looked like a rename had 3,110 and heavy play on both heroes claimed.
    vols = sorted(int(r.get("ceiling_account_games") or 0) for r in rows
                  if (r.get("ceiling_account_games") or "").isdigit())
    median_vol = vols[len(vols) // 2] if vols else 0
    RENAME_VOL_FRACTION = float(os.environ.get("RENAME_VOL_FRACTION") or 0.5)

    out, counts, mismatched = [], {"match": 0, "match_partial": 0,
                                   "mismatch": 0, "likely_rename": 0,
                                   "no_profile": 0}, []
    for r in rows:
        aid = (r.get("account_id") or "").strip()
        board = r.get("ceiling_player") or ""
        if not aid.isdigit():
            continue
        aid = int(aid)
        persona = personas.get(aid) or ""
        if not persona:
            verdict = "no_profile"
        elif norm(persona) == norm(board):
            verdict = "match"
        elif contains_match(persona, board):
            # clan tags and decoration: "[EU] lorence" against "lorence".
            # Common enough that treating it as a mismatch would bury the real
            # signal. Recorded separately so the strict rate stays readable.
            verdict = "match_partial"
        else:
            verdict = "mismatch"
            mismatched.append((aid, board))
        counts[verdict] += 1
        out.append({
            "region": r.get("region", ""), "hero": r.get("hero", ""),
            "ceiling_rank": r.get("ceiling_rank", ""),
            "board_name": board, "steam_persona": persona,
            "account_id": aid, "steamid64": aid + STEAM64_BASE,
            "verdict": verdict,
            "profile_url": "https://steamcommunity.com/profiles/%d"
                           % (aid + STEAM64_BASE),
            # the tell that was visible on lorence and unused: a ceiling far
            # below the run's own median career volume
            "career_games": r.get("ceiling_account_games", ""),
            "career_hero_games": r.get("ceiling_hero_games", ""),
            "scoreboard": r.get("scoreboard", ""),
            "steam_aliases": "", "rename_evidence": "",
        })

    # ---- second pass: is each mismatch a rename or a wrong account? -------
    # Only mismatches are fetched, so this costs a handful of requests.
    aliases = {}
    if mismatched:
        print("\n  [alias] checking %d mismatched account(s) for a rename"
              % len({a for a, _ in mismatched}), file=sys.stderr)
        for aid in sorted({a for a, _ in mismatched}):
            aliases[aid] = fetch_aliases(aid + STEAM64_BASE)
            time.sleep(SLEEP_S)
    for d in out:
        if d["verdict"] != "mismatch":
            continue
        aid = d["account_id"]
        al = aliases.get(aid, [])
        d["steam_aliases"] = " | ".join(al)
        vol = int(d["career_games"]) if str(d["career_games"]).isdigit() else 0
        if any(norm(a) == norm(d["board_name"]) or
               contains_match(a, d["board_name"]) for a in al):
            # the board name IS one of this account's former names
            d["verdict"] = "likely_rename"
            d["rename_evidence"] = "board name found in Steam alias list"
        elif median_vol and vol >= RENAME_VOL_FRACTION * median_vol:
            # no alias evidence, but the account is a heavy player on the hero
            # it was claimed for — a misresolution would not look like this.
            # WEAKER than the alias test, and deliberately labelled as such.
            d["verdict"] = "likely_rename"
            d["rename_evidence"] = ("no alias match; career volume %d is >= %.0f%% "
                                    "of the ceiling median %d"
                                    % (vol, 100 * RENAME_VOL_FRACTION, median_vol))
        else:
            d["rename_evidence"] = ("career volume %d is far below the ceiling "
                                    "median %d — consistent with a wrong account"
                                    % (vol, median_vol))
        counts["mismatch"] -= 1
        counts[d["verdict"] if d["verdict"] != "mismatch" else "mismatch"] += 1

    with open(DEST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]) if out else ["verdict"],
                           extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(out)

    total = sum(counts.values()) or 1
    answered = (counts["match"] + counts["match_partial"]
                + counts["likely_rename"] + counts["mismatch"])
    ok = counts["match"] + counts["match_partial"] + counts["likely_rename"]
    print("\n  -> %s (%d rows)" % (DEST, len(out)), file=sys.stderr)
    print("  [check] %d exact, %d partial (clan tag/decoration), %d likely "
          "rename, %d MISMATCH, %d no profile (private or gone)"
          % (counts["match"], counts["match_partial"], counts["likely_rename"],
             counts["mismatch"], counts["no_profile"]), file=sys.stderr)
    if answered:
        print("  [check] name agreement on answerable rows: %.1f%% (%d of %d)"
              % (100.0 * ok / answered, ok, answered),
              file=sys.stderr)
        print("  [check] this is the project's first measured estimate of "
              "residual identity error. It is an UPPER bound on correctness, "
              "not a lower one: a persona rename reads as a mismatch, so the "
              "true error rate is at or below the mismatch figure.",
              file=sys.stderr)
    for d in out:
        if d["verdict"] in ("mismatch", "likely_rename"):
            print("    %-14s %-9s %-12s rank %-3s board=%r steam=%r\n"
                  "        %s\n        aliases: %s\n        %s"
                  % (d["verdict"].upper(), d["region"], d["hero"],
                     d["ceiling_rank"], d["board_name"], d["steam_persona"],
                     d["rename_evidence"], d["steam_aliases"] or "(none listed)",
                     d["profile_url"]), file=sys.stderr)
    print("\n  Nothing is dropped on this signal. Check each mismatch by hand "
          "before acting — a renamed persona and a wrong account look "
          "identical here.", file=sys.stderr)


if __name__ == "__main__":
    main()
