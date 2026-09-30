#!/usr/bin/env python3
"""Builds items.json (every TF2 item name) from Valve's item schema.

Needs a Steam Web API key in the STEAM_API_KEY environment variable.
For offline testing, set SCHEMA_FILE to a saved GetSchemaItems response.
Only writes items.json if the result looks sane, so a bad run can never
replace a good file with an empty one.
"""
import json, os, sys, time, datetime, urllib.request, urllib.parse

URL = "https://api.steampowered.com/IEconItems_440/GetSchemaItems/v1/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "items.json")
MIN_ITEMS = 1000  # the real schema is far larger; below this something went wrong


def get(url, tries=4):
    for n in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            print(f"request failed ({e}), retry {n + 1}/{tries}", file=sys.stderr)
            time.sleep(5 * (n + 1))
    sys.exit("giving up: Steam API not reachable")


def fetch_all(key):
    items, start = [], 0
    while True:
        q = urllib.parse.urlencode({"key": key, "language": "en", "start": start})
        res = get(f"{URL}?{q}").get("result", {})
        if res.get("status") != 1:
            sys.exit(f"Steam returned an error: {res.get('status')}")
        items += res.get("items", [])
        if "next" not in res:
            return items
        start = res["next"]


def clean(items):
    names = set()
    for it in items:
        n = (it.get("item_name") or "").strip()
        # skip empty names, untranslated tokens and internal class names
        if not n or n.startswith("#") or "TF_" in n or n.startswith("Upgradeable"):
            continue
        # the schema also holds system messages stored as items
        # ("Your account has been flagged...", "Congratulations! ..."); real
        # item names are short and are never full sentences
        low = n.lower()
        if (len(n) > 60 or len(n.split()) > 8 or "!" in n or "?" in n
                or " has been " in low or "we have " in low or "your account" in low):
            continue
        names.add(n)
    return sorted(names, key=str.lower)


def main():
    if os.environ.get("SCHEMA_FILE"):
        with open(os.environ["SCHEMA_FILE"], encoding="utf-8") as f:
            raw = json.load(f)["result"]["items"]
    else:
        key = os.environ.get("STEAM_API_KEY")
        if not key:
            sys.exit("STEAM_API_KEY is not set")
        raw = fetch_all(key)

    names = clean(raw)
    print(f"{len(raw)} schema entries -> {len(names)} unique names")
    if len(names) < MIN_ITEMS:
        sys.exit(f"only {len(names)} names, refusing to overwrite items.json")

    out = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
        "count": len(names),
        "items": names,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
