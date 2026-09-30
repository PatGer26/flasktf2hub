#!/usr/bin/env python3
"""Build keys.json: the TF2 key price in refined metal from several sources.

Sources (each one is fetched separately; a failure only skips that source):
  - backpack.tf IGetCurrencies API (needs BPTF_API_KEY, a GitHub Actions secret)
  - STN Trading and Quicksell: buy/sell price read from their public key pages

keys.json = {"updated", "current": {"ref", "low", "high"}, "shops": [...], "history": [...]}

Offline tests (no network): CURRENCIES_FILE, STN_FILE, QUICKSELL_FILE point at saved responses/pages.
"""
import html, json, os, re, sys, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

OUT = "keys.json"
KEEP_DAYS = 120
UA = "FlaskTF2Hub/1.0 (hobby site; daily check; contact via github.com/PatGer26/flasktf2hub)"

SHOPS = [
    {"id": "stn", "name": "STN Trading", "url": "https://stntrading.eu/tf2/keys", "env": "STN_FILE"},
    {"id": "quicksell", "name": "Quicksell", "url": "https://quicksell.store/keys", "env": "QUICKSELL_FILE"},
]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def sane(v):
    return v is not None and 1 < v < 500


# ---------- backpack.tf ----------
def bptf():
    test_file = os.environ.get("CURRENCIES_FILE")
    if test_file:
        data = json.load(open(test_file, encoding="utf-8"))
    else:
        key = os.environ.get("BPTF_API_KEY", "").strip()
        if not key:
            raise RuntimeError("BPTF_API_KEY is not set")
        url = "https://backpack.tf/api/IGetCurrencies/v1?" + urllib.parse.urlencode({"key": key, "appid": 440})
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError("backpack.tf returned HTTP %s" % e.code)  # never print the URL (it has the key)
        except Exception as e:
            raise RuntimeError("backpack.tf request failed: %s" % type(e).__name__)
    resp = data.get("response", {})
    if resp.get("success") != 1:
        raise RuntimeError("backpack.tf did not report success: %s" % str(resp.get("message", ""))[:200])
    keys = resp.get("currencies", {}).get("keys")
    if not keys:
        raise RuntimeError("no 'keys' entry in response")
    price = keys.get("price", {})
    low, high = num(price.get("value")), num(price.get("value_high"))
    if not sane(low):
        raise RuntimeError("key price looks wrong: %r" % price.get("value"))
    ref = round(low if high is None else (low + high) / 2, 2)
    return {"ref": ref, "low": low, "high": high}


# ---------- shop pages ----------
def page_text(shop):
    f = os.environ.get(shop["env"])
    if f:
        raw = open(f, encoding="utf-8").read()
    else:
        req = urllib.request.Request(shop["url"], headers={"User-Agent": UA, "Accept-Language": "en"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise RuntimeError("HTTP %s from %s" % (e.code, shop["name"]))
        except Exception as e:
            raise RuntimeError("request to %s failed: %s" % (shop["name"], type(e).__name__))
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text)


# The wording differs a bit between sites ("We're buying for: 62.22 ref",
# "We buy keys for: 62.77 refined each"), so match loosely.
BUY = re.compile(r"\bbuy(?:ing)?(?:\s+keys)?\s+for\s*:?\s*(\d+(?:\.\d+)?)", re.I)
SELL = re.compile(r"\bsell(?:ing)?(?:\s+keys)?\s+for\s*:?\s*(\d+(?:\.\d+)?)", re.I)


def shop_price(shop):
    text = page_text(shop)
    b, s = BUY.search(text), SELL.search(text)
    if not b or not s:
        raise RuntimeError("%s: buy/sell price not found in page (layout changed, or blocked?)" % shop["name"])
    buy, sell = float(b.group(1)), float(s.group(1))
    if not (sane(buy) and sane(sell)) or buy >= sell:
        raise RuntimeError("%s: prices look wrong (buy %s, sell %s)" % (shop["name"], buy, sell))
    return buy, sell


def main():
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    problems, got = [], 0

    current = {"ref": None, "low": None, "high": None}
    try:
        current = bptf()
        got += 1
    except Exception as e:
        problems.append(str(e))

    shops, today_shops = [], {}
    for sh in SHOPS:
        entry = {"id": sh["id"], "name": sh["name"], "url": sh["url"], "unit": "ref", "buy": None, "sell": None}
        try:
            entry["buy"], entry["sell"] = shop_price(sh)
            today_shops[sh["id"]] = {"b": entry["buy"], "s": entry["sell"]}
            got += 1
        except Exception as e:
            problems.append(str(e))
        shops.append(entry)

    for p in problems:
        print("WARNING:", p)
    if got == 0:
        sys.exit("no source worked, keys.json left unchanged")

    history = []
    if os.path.exists(OUT):
        try:
            history = json.load(open(OUT, encoding="utf-8")).get("history", [])
        except Exception:
            history = []
    history = [h for h in history if h.get("date") != today]
    point = {"date": today}
    if current["ref"] is not None:
        point["ref"] = current["ref"]
    if today_shops:
        point["shops"] = today_shops
    history.append(point)
    history = sorted(history, key=lambda h: h["date"])[-KEEP_DAYS:]

    out = {"updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "current": current, "shops": shops, "history": history}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print("ok: backpack.tf ref=%s; shops: %s; %d history points" %
          (current["ref"], {k: (v["b"], v["s"]) for k, v in today_shops.items()}, len(history)))


if __name__ == "__main__":
    main()
