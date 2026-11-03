#!/usr/bin/env python3
"""Refresh data/games.json, the facts the sortable list cannot read from README.md.

README.md stays the only place entries are written. This script adds what the
README does not say:

  added       the date the entry first appeared, from the edit history
  price       regular US price, from the Steam store and the App Store
  released    release date, from the same stores
  genres      store genres
  rating      percent of positive ratings, and ratingCount (SteamSpy, App Store)

Anything the stores cannot answer goes in data/overrides.json, which is written
by hand and always wins. This script only reads that file, for the steamId and
appleId of games whose README link does not point at their store page.

Usage:
  python tools/update_data.py              refresh every entry
  python tools/update_data.py --new-only   only entries with no data yet
  python tools/update_data.py --no-stores  dates only, no network calls to stores
  python tools/update_data.py --gist DIR   also read a clone of the original gist
                                           when working out dates
  python tools/update_data.py --sales FILE write current Steam sale prices to FILE
                                           and change nothing else

Standard library only. Keep parse_entries in step with parseEntries in
assets/list.js: both must produce the same keys.
"""

import argparse
import datetime
import difflib
import functools
import json
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DATA = ROOT / "data" / "games.json"
OVERRIDES = ROOT / "data" / "overrides.json"

SECTIONS = {
    "games": "games",
    "accidentally accessible games": "accidental",
    "ios games": "ios",
}
LINK = re.compile(r"\[([^\]]*)\]\(((?:[^()\s]|\([^()\s]*\))+)\)")
USER_AGENT = "blind-accessible-games-list data updater"
STEAM_DELAY = 1.6  # the store allows about 200 calls per 5 minutes
# Store labels that describe how a game is sold rather than what it is.
STEAM_NOT_GENRES = {"Free To Play", "Free to Play", "Early Access", "Indie"}
APPLE_NOT_GENRES = {"Games", "Entertainment", "Education", "Utilities", "Lifestyle"}
GENRE_NAMES = {"Roleplaying": "RPG", "Massively Multiplayer": "MMO"}


def repair_line(text):
    """Turn the few malformed link shapes in the list into ordinary Markdown."""
    text = re.sub(r"^\(([^\]]+)\]\(", r"[\1](", text)  # (Osu!](url)
    text = re.sub(r"\[([^\]\[()]+)\[\(", r"[\1](", text)  # [text,[(url)
    text = re.sub(r"\[([^\]\[()]+)\((https?://)", r"[\1](\2", text)  # [text(url)
    text = re.sub(r"\[([^\]\[()]+)\[\)", r"[\1]", text)  # [text[)
    text = re.sub(r"^([^\[(]+?) \((https?://[^\s)]+)\)", r"[\1](\2)", text)  # text (url)
    return text


def entry_title(text):
    match = re.match(r"\[([^\]]*)\]", text)
    title = match.group(1) if match else re.split(r"[\[(]", text, maxsplit=1)[0]
    title = re.split(r"\s+e\.g\.", title, maxsplit=1, flags=re.I)[0]
    return title.strip(" ,.;:")


def slug(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_entries(markdown):
    """Return the top-level entries of the three game lists, with their keys."""
    entries = []
    section = None
    for raw in markdown.splitlines():
        heading = re.match(r"^#+\s+(.*\S)\s*$", raw)
        if heading:
            section = SECTIONS.get(heading.group(1).lower())
            continue
        if section and raw.startswith("- "):
            text = repair_line(raw[2:].strip())
            links = LINK.findall(text)
            entries.append({
                "section": section,
                "title": entry_title(text),
                "urls": [url for _, url in links],
                "second": links[1][0] if len(links) > 1 else "",
            })
    counts = {}
    for entry in entries:
        base = f"{entry['section']}/{slug(entry['title'])}"
        entry["base"] = base
        counts[base] = counts.get(base, 0) + 1
    for entry in entries:
        key = entry["base"]
        entry["duplicate"] = counts[key] > 1
        if entry["duplicate"]:
            key += "/" + slug(entry["second"])
        entry["key"] = key
    return entries


def canonical_url(url):
    match = re.search(r"store\.steampowered\.com/(?:agecheck/)?app/(\d+)", url)
    if match:
        return "steam:" + match.group(1)
    match = re.search(r"apps\.apple\.com/.*?id(\d+)", url)
    if match:
        return "apple:" + match.group(1)
    parts = urllib.parse.urlparse(url)
    return (parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/").lower())


# --- date added ---------------------------------------------------------------

def history_items(markdown):
    """Every bullet of one old revision. Old revisions have looser formats."""
    items = []
    is_ios = False
    for raw in markdown.splitlines():
        heading = re.match(r"^#+\s+(.*\S)\s*$", raw)
        if heading:
            is_ios = "ios" in heading.group(1).lower()
            continue
        if raw.startswith("- "):
            text = repair_line(raw[2:].strip())
            urls = [canonical_url(url) for _, url in LINK.findall(text)]
            title = entry_title(text)
            slugs = {slug(title)}
            if not urls:
                # Unlinked lines ran straight on: "A Dark Room - fully accessible".
                for mark in ("- ", " – ", ": ", ", "):
                    slugs.add(slug(title.split(mark, 1)[0]))
            items.append({"ios": is_ios, "slugs": slugs, "urls": urls})
    return items


def git_revisions(repo, path):
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%H %ad", "--date=short", "--follow", "--", path],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    revisions = []
    for line in log.splitlines():
        commit, date = line.split()
        shown = subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{path}"],
            capture_output=True, encoding="utf-8", errors="replace",
        )
        if shown.returncode == 0:
            revisions.append((date, history_items(shown.stdout)))
    return revisions


def numerals(text):
    return re.findall(r"\b(?:\d+|i{1,3}|iv|v|vi{0,3}|ix|x{1,3})\b", text.replace("-", " "))


@functools.lru_cache(maxsize=None)
def same_spelling(old_slug, slug_now):
    return numerals(old_slug) == numerals(slug_now) and \
        difflib.SequenceMatcher(None, old_slug, slug_now).ratio() >= 0.86


def is_rename(old_slugs, known, current_slugs):
    """An old title that was only respelled, such as Brock and Brok."""
    if old_slugs & current_slugs:
        return False  # a different game that is still on the list
    return any(same_spelling(old, now) for old in old_slugs for now in known)


def compute_added(entries, revisions, today):
    """Walk back through the revisions, following each entry through renames."""
    revisions = sorted(revisions, key=lambda rev: rev[0], reverse=True)
    current_slugs = {slug(e["title"]) for e in entries}
    first_url_uses = {}
    for entry in entries:
        if entry["urls"]:
            first = canonical_url(entry["urls"][0])
            first_url_uses[first] = first_url_uses.get(first, 0) + 1
    added = {}
    for entry in entries:
        is_ios = entry["section"] == "ios"
        if entry["duplicate"]:
            # Same title twice (World of Warcraft): only the mod link tells them apart.
            slugs = set()
            urls = {canonical_url(u) for u in entry["urls"][1:2]}
        else:
            slugs = {slug(entry["title"])}
            urls = set()
            if entry["urls"]:
                first = canonical_url(entry["urls"][0])
                if first_url_uses[first] == 1:
                    urls.add(first)
        earliest = None
        for date, items in revisions:
            for item in items:
                if item["ios"] != is_ios:
                    continue
                same_url = item["urls"] and item["urls"][0] in urls
                if entry["duplicate"]:
                    same_url = bool(urls.intersection(item["urls"]))
                renamed = not entry["duplicate"] and is_rename(item["slugs"], slugs, current_slugs)
                if item["slugs"] & slugs or same_url or renamed:
                    earliest = date
                    if not entry["duplicate"] and item["urls"]:
                        slugs |= item["slugs"]
                        urls.add(item["urls"][0])
        added[entry["key"]] = earliest or today
    return added


# --- stores -------------------------------------------------------------------

def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise
            time.sleep(30 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("giving up on " + url)


def parse_steam_date(text):
    text = (text or "").replace(",", "").strip()
    for layout in ("%d %b %Y", "%b %d %Y", "%d %B %Y", "%B %d %Y"):
        try:
            return datetime.datetime.strptime(text, layout).date().isoformat()
        except ValueError:
            pass
    match = re.search(r"\b(19|20)\d\d\b", text)
    return match.group(0) if match else None


def steam_facts(app_id):
    reply = get_json(
        f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=us&l=en"
    )[app_id]
    if not reply.get("success"):
        return None
    game = reply["data"]
    facts = {"source": "steam"}
    released = game.get("release_date") or {}
    if released.get("coming_soon"):
        facts["released"] = "upcoming"
    else:
        facts["released"] = parse_steam_date(released.get("date"))
    if game.get("is_free"):
        facts["price"] = 0
    elif game.get("price_overview", {}).get("currency") == "USD":
        facts["price"] = game["price_overview"]["initial"] / 100
    names = [g["description"] for g in game.get("genres", [])]
    genres = [GENRE_NAMES.get(n, n) for n in names if n not in STEAM_NOT_GENRES]
    facts["genres"] = genres or [n for n in names if n == "Indie"]
    time.sleep(STEAM_DELAY)
    try:
        # Steam's own reviews address redirects scripts to its front page.
        counts = get_json(f"https://steamspy.com/api.php?request=appdetails&appid={app_id}")
        positive, negative = counts.get("positive") or 0, counts.get("negative") or 0
        if positive + negative:
            facts["rating"] = round(100 * positive / (positive + negative))
            facts["ratingCount"] = positive + negative
    except Exception as error:  # store details are still worth keeping
        print(f"  ratings for {app_id}: {error}", file=sys.stderr)
    time.sleep(1)
    return facts


def apple_facts(app_ids):
    """Look up App Store games in batches; a lookup takes up to 200 ids."""
    found = {}
    for start in range(0, len(app_ids), 150):
        batch = ",".join(app_ids[start:start + 150])
        reply = get_json(f"https://itunes.apple.com/lookup?id={batch}&country=us&entity=software")
        for game in reply.get("results", []):
            facts = {"source": "apple", "price": game.get("price")}
            facts["released"] = (game.get("releaseDate") or "")[:10] or None
            names = [GENRE_NAMES.get(n, n) for n in game.get("genres", [])
                     if n not in APPLE_NOT_GENRES]
            facts["genres"] = names
            if game.get("userRatingCount"):
                facts["rating"] = round(20 * game.get("averageUserRating", 0))
                facts["ratingCount"] = game["userRatingCount"]
            found[str(game["trackId"])] = facts
        time.sleep(1)
    return found


def first_store_id(entry, kind, overrides):
    named = overrides.get(entry["key"], {}).get(kind + "Id")
    if named:
        return str(named)
    for url in entry["urls"]:
        link = canonical_url(url)
        if link.startswith(kind + ":"):
            return link.split(":", 1)[1]
    return None


def steam_sales(entries, overrides):
    """Today's Steam discounts for every entry whose regular price comes from Steam."""
    ids = {}
    for entry in entries:
        # A hand-set price may not be the price of the Steam page linked.
        if "price" not in overrides.get(entry["key"], {}):
            app_id = first_store_id(entry, "steam", overrides)
            if app_id:
                ids[entry["key"]] = app_id
    wanted = sorted(set(ids.values()))
    found = {}
    for start in range(0, len(wanted), 100):
        batch = ",".join(wanted[start:start + 100])
        reply = get_json(
            f"https://store.steampowered.com/api/appdetails?appids={batch}&cc=us&filters=price_overview"
        )
        for app_id, answer in reply.items():
            if answer.get("success"):
                found[app_id] = answer.get("data") or {}
        time.sleep(STEAM_DELAY)
    if len(found) < len(wanted) / 2:
        raise SystemExit(f"Steam answered for only {len(found)} of {len(wanted)} games")
    sales = {}
    for key, app_id in ids.items():
        if app_id in found:
            price = found[app_id].get("price_overview") or {}
            percent = price.get("discount_percent", 0)
            sales[key] = {"percent": percent, "price": price["final"] / 100} if percent else {"percent": 0}
    return dict(sorted(sales.items()))


# --- main ---------------------------------------------------------------------

def write(data):
    DATA.parent.mkdir(exist_ok=True)
    data["entries"] = dict(sorted(data["entries"].items()))
    DATA.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--new-only", action="store_true", help="skip entries that already have data")
    parser.add_argument("--no-stores", action="store_true", help="do not call the stores")
    parser.add_argument("--gist", type=Path, help="clone of the original gist, for older dates")
    parser.add_argument("--redo-dates", action="store_true", help="work out every date again")
    parser.add_argument("--sales", type=Path, help="write current Steam sale prices here, and do nothing else")
    args = parser.parse_args()

    today = datetime.date.today().isoformat()
    entries = parse_entries(README.read_text(encoding="utf-8"))
    overrides = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.exists() else {}
    if args.sales:
        report = {"checked": today, "entries": steam_sales(entries, overrides)}
        args.sales.write_text(json.dumps(report, indent=0) + "\n", encoding="utf-8")
        return
    data = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {}
    known = data.get("entries", {})
    data = {
        "updated": data.get("updated") or today,  # the date the stores were last checked
        "historyStart": data.get("historyStart"),
        "entries": {e["key"]: known.get(e["key"], {}) for e in entries},
    }

    undated = [e for e in entries if args.redo_dates or "added" not in data["entries"][e["key"]]]
    if undated:
        revisions = git_revisions(ROOT, "README.md")
        if args.gist:
            revisions += git_revisions(args.gist, "index.md")
        # A run without --gist sees less history, so never move this date forward.
        data["historyStart"] = min(filter(None, [data["historyStart"], *(rev[0] for rev in revisions)]))
        added = compute_added(entries, revisions, today)
        for entry in undated:
            data["entries"][entry["key"]]["added"] = added[entry["key"]]
    write(data)

    if args.no_stores:
        return
    if not args.new_only:
        data["updated"] = today
    pending = [e for e in entries
               if not (args.new_only and "source" in data["entries"][e["key"]])]

    apple_ids = {e["key"]: first_store_id(e, "apple", overrides) for e in pending}
    wanted = sorted({i for i in apple_ids.values() if i})
    if wanted:
        found = apple_facts(wanted)
        for key, app_id in apple_ids.items():
            if app_id in found:
                data["entries"][key].update(found[app_id])
        write(data)

    # Several entries can share a Steam page, so ask for each page once.
    steam_ids = {e["key"]: first_store_id(e, "steam", overrides) for e in pending}
    steam_ids = {key: i for key, i in steam_ids.items()
                 if i and "apple" != data["entries"][key].get("source")}
    done = {}
    for number, (key, app_id) in enumerate(sorted(steam_ids.items()), 1):
        if app_id not in done:
            try:
                done[app_id] = steam_facts(app_id)
            except Exception as error:  # keep what we have; one bad page must not stop the run
                print(f"  {key}: {error}", file=sys.stderr)
                done[app_id] = None
        if done[app_id]:
            data["entries"][key].update(done[app_id])
        if number % 20 == 0:
            print(f"Steam: {number} of {len(steam_ids)}", flush=True)
            write(data)
    write(data)


if __name__ == "__main__":
    main()
