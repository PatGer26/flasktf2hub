#!/usr/bin/env python3
"""Fetch the TF2 key price in refined metal from backpack.tf and write keys.json.

keys.json = {"updated": ISO time, "source": ..., "current": {...}, "history": [{"date", "ref"}...]}
Needs the BPTF_API_KEY environment variable (a GitHub Actions secret).
Offline test: CURRENCIES_FILE=path/to/saved_response.json python3 scripts/update_keys.py
"""
import json, os, sys, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

OUT = "keys.json"
KEEP_DAYS = 120


def fetch():
    test_file = os.environ.get("CURRENCIES_FILE")
    if test_file:
        with open(test_file, encoding="utf-8") as f:
            return json.load(f)
    key = os.environ.get("BPTF_API_KEY", "").strip()
    if not key:
        sys.exit("BPTF_API_KEY is not set")
    url = "https://backpack.tf/api/IGetCurrencies/v1?" + urllib.parse.urlencode({"key": key, "appid": 440})
    req = urllib.request.Request(url, headers={"User-Agent": "FlaskTF2Hub/1.0 (hobby site)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # never print the URL: it contains the API key
        sys.exit("backpack.tf returned HTTP %s" % e.code)
    except Exception as e:
        sys.exit("request failed: %s" % type(e).__name__)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    data = fetch()
    resp = data.get("response", {})
    if resp.get("success") != 1:
        sys.exit("backpack.tf did not report success: %s" % str(resp.get("message", "no message"))[:200])
    keys = resp.get("currencies", {}).get("keys")
    if not keys:
        sys.exit("no 'keys' entry in response; keys seen: %s" % list(resp.get("currencies", {}).keys()))
    price = keys.get("price", {})
    value = num(price.get("value"))
    high = num(price.get("value_high"))
    if value is None or not (1 < value < 500):
        sys.exit("key price looks wrong: %r" % price.get("value"))
    ref = round(value if high is None else (value + high) / 2, 2)

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    history = []
    if os.path.exists(OUT):
        try:
            history = json.load(open(OUT, encoding="utf-8")).get("history", [])
        except Exception:
            history = []
    history = [h for h in history if h.get("date") != today]
    history.append({"date": today, "ref": ref})
    history = sorted(history, key=lambda h: h["date"])[-KEEP_DAYS:]

    out = {
        "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "backpack.tf IGetCurrencies",
        "current": {"ref": ref, "low": value, "high": high},
        "history": history,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print("key = %s ref (low %s, high %s), %d history points" % (ref, value, high, len(history)))


if __name__ == "__main__":
    main()
