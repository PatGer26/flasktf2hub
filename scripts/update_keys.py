#!/usr/bin/env python3
"""Build keys.json: the TF2 key price in refined metal from several sources.

Sources (each one is fetched separately; a failure only skips that source):
  - backpack.tf IGetCurrencies API (needs BPTF_API_KEY, a GitHub Actions secret)
  - STN Trading: buy/sell price in ref, read from its public key page
  - Scrap.tf (trial): buy/sell price in ref, read from its public key item page (one request per run)
  - Steam Community Market: lowest listing price in USD (public priceoverview JSON, no key)
  - Mannco.store: lowest sale price and buy-order price in USD (official API; MANNCO_API_KEY secret)
  - Skinport: lowest listing price in USD (official public API, no key; uses curl because the API only answers in Brotli)

keys.json = {"updated", "current": {"ref", "low", "high"}, "shops": [...], "history": [...]}

Offline tests (no network): CURRENCIES_FILE, STN_FILE, SCRAP_FILE, STEAM_FILE, MANNCO_FILE, SKINPORT_FILE point at saved responses/pages.
"""
import html, json, os, re, subprocess, sys, tempfile, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

OUT = "keys.json"
KEEP_DAYS = 120
UA = "FlaskTF2Hub/1.0 (hobby site; daily check; contact via github.com/PatGer26/flasktf2hub)"

SHOPS = [
    {"id": "stn", "name": "STN Trading", "url": "https://stntrading.eu/tf2/keys", "env": "STN_FILE"},
    # Scrap.tf words its price from the CUSTOMER's side: "Sell for 62.33 refined, Buy for 66.33 refined"
    # means the shop pays 62.33 for your key (its buy price) and charges 66.33 (its sell price).
    # 'rx' captures (what the shop pays, what the shop charges), so the order is already corrected.
    {"id": "scrap", "name": "Scrap.tf", "url": "https://scrap.tf/item/mann-co-supply-crate-key", "env": "SCRAP_FILE",
     "rx": r"Sell for (\d+(?:\.\d+)?) refined,\s*Buy for (\d+(?:\.\d+)?) refined"},
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
    text = re.sub(r"\s+", " ", text).strip()
    return text, len(raw)


# The wording differs a bit between sites ("We're buying for: 62.22 ref",
# "We buy keys for: 62.77 refined each"), so match loosely.
BUY = re.compile(r"\bbuy(?:ing)?(?:\s+keys)?\s+for\s*:?\s*(\d+(?:\.\d+)?)", re.I)
SELL = re.compile(r"\bsell(?:ing)?(?:\s+keys)?\s+for\s*:?\s*(\d+(?:\.\d+)?)", re.I)


def shop_price(shop):
    text, raw_len = page_text(shop)
    if shop.get("rx"):
        m = re.search(shop["rx"], text, re.I)
        b, s = (m, m) if m else (None, None)
    else:
        b, s = BUY.search(text), SELL.search(text)
    if not b or not s:
        # Diagnostic: show what the server actually sent (public page text, no secrets)
        raise RuntimeError("%s: buy/sell price not found in page (layout changed, or blocked?). "
                           "Received %d bytes of HTML, %d chars of text. Text starts: %r"
                           % (shop["name"], raw_len, len(text), text[:300]))
    if shop.get("rx"):
        buy, sell = float(b.group(1)), float(b.group(2))
    else:
        buy, sell = float(b.group(1)), float(s.group(1))
    if not (sane(buy) and sane(sell)) or buy >= sell:
        raise RuntimeError("%s: prices look wrong (buy %s, sell %s)" % (shop["name"], buy, sell))
    return buy, sell


# ---------- Steam Community Market (USD) ----------
STEAM_URL = ("https://steamcommunity.com/market/priceoverview/?"
             + urllib.parse.urlencode({"appid": 440, "currency": 1, "market_hash_name": "Mann Co. Supply Crate Key"}))
STEAM_PAGE = "https://steamcommunity.com/market/listings/440/Mann%20Co.%20Supply%20Crate%20Key"


def usd(text):
    """'$2.30' or '$1,234.50' -> float"""
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)", str(text or ""))
    return float(m.group(1).replace(",", "")) if m else None


def steam():
    f = os.environ.get("STEAM_FILE")
    if f:
        data = json.load(open(f, encoding="utf-8"))
    else:
        req = urllib.request.Request(STEAM_URL, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError("Steam Market returned HTTP %s (429 = rate limited or blocked)" % e.code)
        except Exception as e:
            raise RuntimeError("Steam Market request failed: %s" % type(e).__name__)
    if not data.get("success"):
        raise RuntimeError("Steam Market did not report success: %s" % str(data)[:200])
    low, med = usd(data.get("lowest_price")), usd(data.get("median_price"))
    if not (1 < (low or 0) < 50):
        raise RuntimeError("Steam Market price looks wrong: %r" % data.get("lowest_price"))
    return low, (med if med and 1 < med < 50 else None)


# ---------- Mannco.store official API (USD) ----------
MANNCO_API = "https://api.mannco.store"
MANNCO_ITEM = "440-mann-co-supply-crate-key"
MANNCO_PAGE = "https://mannco.store/item/440-mann-co-supply-crate-key"


def cents(v):
    v = num(v)
    return round(v / 100.0, 2) if v else None


def mannco():
    f = os.environ.get("MANNCO_FILE")
    if f:
        data = json.load(open(f, encoding="utf-8"))
    else:
        key = os.environ.get("MANNCO_API_KEY", "").strip()
        if not key:
            raise RuntimeError("MANNCO_API_KEY is not set")
        step = "login"
        try:
            # 1) exchange the API key for a short-lived token (the token is tied to this machine's IP)
            body = json.dumps({"apiKey": key}).encode()
            req = urllib.request.Request(MANNCO_API + "/user/login", data=body, method="POST",
                                         headers={"User-Agent": UA, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                jwt = json.load(r)["content"]["jwt"]
            auth = {"User-Agent": UA, "Authorization": "Bearer " + jwt}
            # 2) look up the numeric item id from the URL slug (the pricing endpoint wants the number)
            step = "item lookup"
            req = urllib.request.Request(MANNCO_API + "/item/details/" + MANNCO_ITEM, headers=auth)
            with urllib.request.urlopen(req, timeout=30) as r:
                details = json.load(r)
            item_id = ((details.get("content") or {}).get("informations") or {}).get("id")
            if not isinstance(item_id, int):
                raise RuntimeError("Mannco item lookup gave no numeric id")
            # 3) read the pricing for that id
            step = "pricing"
            req = urllib.request.Request(MANNCO_API + "/item/pricing/" + str(item_id), headers=auth)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            # Never print the login response (it holds the token) or any URL. Item lookup and pricing
            # responses only hold public item data, so their first 400 characters help diagnosis.
            msg = "Mannco API returned HTTP %s at the %s step (content-type: %s)" % (e.code, step, e.headers.get("Content-Type"))
            if e.code == 429:
                msg += "; rate limited, Retry-After: %s seconds" % e.headers.get("Retry-After")
            if step != "login":
                try:
                    msg += "; body starts: %r" % e.read(400).decode("utf-8", "replace")
                except Exception:
                    pass
            raise RuntimeError(msg)
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError("Mannco API request failed at the %s step: %s" % (step, type(e).__name__))
    if not data.get("success"):
        raise RuntimeError("Mannco API reported an error")
    pricing = (data.get("content") or {}).get("pricing") or {}
    sell, buy = cents(pricing.get("lowest_sale_price")), cents(pricing.get("lowest_buy_order"))
    if not (sell and 0.5 < sell < 50):
        raise RuntimeError("Mannco price looks wrong: %r" % pricing.get("lowest_sale_price"))
    if buy is not None and not (0.5 < buy < 50 and buy < sell):
        buy = None  # out of range, or not below the sell price: treat as unavailable
    return buy, sell


# ---------- Skinport official public API (USD) ----------
# GET /v1/items is public (no key), limited to 8 requests per 5 minutes, and only answers with
# "Accept-Encoding: br" (Brotli). Python's standard library cannot decode Brotli, so curl does it.
SKINPORT_URL = "https://api.skinport.com/v1/items?" + urllib.parse.urlencode({"app_id": 440, "currency": "USD", "tradable": 1})
SKINPORT_PAGE = "https://skinport.com/tf2/market/tool?item=Mann+Co.+Supply+Crate+Key"
SKINPORT_NAME = "Mann Co. Supply Crate Key"


def skinport():
    f = os.environ.get("SKINPORT_FILE")
    if f:
        data = json.load(open(f, encoding="utf-8"))
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        try:
            try:
                res = subprocess.run(["curl", "-sS", "--compressed", "--max-time", "90", "-A", UA,
                                      "-H", "Accept-Encoding: br", "-o", tmp.name, "-w", "%{http_code}", SKINPORT_URL],
                                     capture_output=True, text=True, timeout=120)
            except Exception as e:
                raise RuntimeError("Skinport request could not run curl: %s" % type(e).__name__)
            code = (res.stdout or "").strip()
            if res.returncode != 0:
                raise RuntimeError("Skinport request failed (curl exit %s): %s" % (res.returncode, (res.stderr or "").strip()[:200]))
            raw = open(tmp.name, "rb").read()
            if code != "200":
                raise RuntimeError("Skinport returned HTTP %s (403 = blocked by its bot protection, 429 = rate limited); "
                                   "received %d bytes, starts: %r" % (code, len(raw), raw[:200].decode("utf-8", "replace")))
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                raise RuntimeError("Skinport answer was not JSON (%d bytes, starts: %r)" % (len(raw), raw[:200].decode("utf-8", "replace")))
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
    if not isinstance(data, list):
        raise RuntimeError("Skinport answer has an unexpected shape: %s" % str(data)[:200])
    rows = [x for x in data if isinstance(x, dict) and x.get("market_hash_name") == SKINPORT_NAME and num(x.get("min_price")) is not None]
    if not rows:
        raise RuntimeError("Skinport list (%d items) has no priced '%s'" % (len(data), SKINPORT_NAME))
    best = min(rows, key=lambda x: num(x["min_price"]))
    low, med = num(best.get("min_price")), num(best.get("median_price"))
    if not (1 < low < 50):
        raise RuntimeError("Skinport price looks wrong: %r" % best.get("min_price"))
    return low, (med if med and 1 < med < 50 else None)


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

    # Steam Market: USD, lowest listing = what a buyer pays. No buy side.
    st = {"id": "steam", "name": "Steam Market", "url": STEAM_PAGE, "unit": "usd", "buy": None, "sell": None, "median": None}
    try:
        st["sell"], st["median"] = steam()
        today_shops["steam"] = {"s": st["sell"], "m": st["median"]}
        got += 1
    except Exception as e:
        problems.append(str(e))
    shops.append(st)

    # Mannco.store: USD via its official API. buy = buy-order price, sell = lowest sale price.
    mc = {"id": "mannco", "name": "Mannco.store", "url": MANNCO_PAGE, "unit": "usd", "buy": None, "sell": None}
    try:
        mc["buy"], mc["sell"] = mannco()
        today_shops["mannco"] = {"b": mc["buy"], "s": mc["sell"]}
        got += 1
    except Exception as e:
        problems.append(str(e))
    shops.append(mc)

    # Skinport: USD via its public API. Lowest listing = what a buyer pays. No buy side.
    sk = {"id": "skinport", "name": "Skinport", "url": SKINPORT_PAGE, "unit": "usd", "buy": None, "sell": None, "median": None}
    try:
        sk["sell"], sk["median"] = skinport()
        today_shops["skinport"] = {"s": sk["sell"], "m": sk["median"]}
        got += 1
    except Exception as e:
        problems.append(str(e))
    shops.append(sk)

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
          (current["ref"], {k: (v.get("b"), v.get("s")) for k, v in today_shops.items()}, len(history)))


if __name__ == "__main__":
    main()
