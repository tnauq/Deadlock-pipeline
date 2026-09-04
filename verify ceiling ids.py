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

    out, counts = [], {"match": 0, "match_partial": 0, "mismatch": 0,
                       "no_profile": 0}
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
        })

    with open(DEST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]) if out else ["verdict"],
                           extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(out)

    total = sum(counts.values()) or 1
    answered = counts["match"] + counts["match_partial"] + counts["mismatch"]
    ok = counts["match"] + counts["match_partial"]
    print("\n  -> %s (%d rows)" % (DEST, len(out)), file=sys.stderr)
    print("  [check] %d exact, %d partial (clan tag/decoration), %d MISMATCH, "
          "%d no profile (private or gone)"
          % (counts["match"], counts["match_partial"], counts["mismatch"],
             counts["no_profile"]), file=sys.stderr)
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
        if d["verdict"] == "mismatch":
            print("    MISMATCH %-9s %-12s rank %-3s board=%r steam=%r  %s"
                  % (d["region"], d["hero"], d["ceiling_rank"],
                     d["board_name"], d["steam_persona"], d["profile_url"]),
                  file=sys.stderr)
    print("\n  Nothing is dropped on this signal. Check each mismatch by hand "
          "before acting — a renamed persona and a wrong account look "
          "identical here.", file=sys.stderr)


if __name__ == "__main__":
    main()
