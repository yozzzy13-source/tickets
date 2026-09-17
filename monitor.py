#!/usr/bin/env python3
"""
Rolex Shanghai Masters 2026 — ticket availability monitor.

Watches the official English ticket shop (ztmen.jussyun.com) for the
semi-final and final sessions, and pings a Telegram chat the moment
seats flip from sold-out to available.

It does NOT log in, does NOT buy anything and does NOT touch payment.
It only reads what any visitor sees, and tells you to go buy manually.
"""

import json
import os
import re
import sys
import time
import threading
import traceback
from datetime import datetime, timezone, timedelta

import requests
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------
# Config (all via environment variables on Railway)
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TICKET_URL = os.environ.get("TICKET_URL", "https://ztmen.jussyun.com/").strip()


def parse_sites(raw, group):
    """'Label|https://a,Label2|https://b' -> [{'label','url','group'}]"""
    sites = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" in chunk:
            label, url = chunk.split("|", 1)
        else:
            label, url = chunk, chunk
        label, url = label.strip(), url.strip()
        if not url.startswith("http"):
            continue
        sites.append({"label": label or url, "url": url, "group": group})
    return sites


SITES = (parse_sites(os.environ.get("MONITOR_URLS_FAST"), "fast")
         + parse_sites(os.environ.get("MONITOR_URLS_SLOW"), "slow"))
if not SITES:                       # fallback to the old single-URL setup
    SITES = [{"label": "Juss EN Shop", "url": TICKET_URL, "group": "fast"}]


# Price ceiling per single ticket, in USD.
MAX_PRICE_USD = float(os.environ.get("MAX_PRICE_USD", "300"))
# "Optimal" price — anything at or below this gets the loud alert wording.
GOOD_PRICE_USD = float(os.environ.get("GOOD_PRICE_USD", "200"))
# CNY -> USD. Update if the rate drifts a lot.
CNY_PER_USD = float(os.environ.get("CNY_PER_USD", "7.1"))

# How many tickets you actually need per session.
TARGET_QTY = int(os.environ.get("TARGET_QTY", "2"))

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "90"))
SLOW_INTERVAL = int(os.environ.get("SLOW_INTERVAL_SECONDS", "600"))
# Slow down between 02:00 and 08:00 Shanghai time (nothing drops at night).
QUIET_HOURS_MULTIPLIER = float(os.environ.get("QUIET_HOURS_MULTIPLIER", "3"))

STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "45000"))

SHANGHAI = timezone(timedelta(hours=8))

# Nothing counts unless it clearly belongs to THIS tournament. The shop's
# landing page lists concerts and other events with overlapping dates.
EVENT_KEYWORDS = [
    "shanghai masters", "rolex shanghai", "上海大师赛", "劳力士大师赛",
    "qi zhong", "qizhong", "旗忠",
]
# Text that means "this is some other event on the same dates".
EVENT_BLOCKLIST = [
    "tomorrowland", "music festival", "concert", "演唱会", "音乐节",
    "planaxis", "dome",
]
# Sessions we do NOT want even though they are Shanghai Masters.
SESSION_BLOCKLIST = ["qualifying", "资格赛", "carnival", "嘉年华", "museum"]


# Only real dates end a session block. Words like "Final" must not, because
# "Semi-Finals" contains one and would cut the block before its prices.
SESSION_BOUNDARY_RE = re.compile(
    r"\d{1,2}\s*oct\w*\.?\s*,?\s*2026|2026[-/.]10[-/.]\d{1,2}|10[-/.月]\d{1,2}",
    re.I)

RANGE_RE = re.compile(
    r"(\d{1,2})\s*oct\w*\.?\s*,?\s*2026\s*(?:to|-|–|~|至)\s*(\d{1,2})\s*oct",
    re.I)


def is_multi_day_range(blob):
    """'7 Oct 2026 to 18 Oct 2026' is the whole tournament card, not a session."""
    m = RANGE_RE.search(blob)
    return bool(m and m.group(1) != m.group(2))


def has_event_keyword(blob):
    low = blob.lower()
    return any(k in low for k in EVENT_KEYWORDS)


def is_other_event(blob):
    low = blob.lower()
    return any(b in low for b in EVENT_BLOCKLIST)


def mentions_event(blob):
    return has_event_keyword(blob) and not is_other_event(blob)

# Sessions we care about. Tournament runs 5-18 Oct 2026:
# semi-finals Sat 17 Oct, final Sun 18 Oct.
TARGETS = [
    {
        "key": "semifinal",
        "label": "ПОЛУФИНАЛ (17 окт, сб)",
        "patterns": [
            r"2026[-/.]10[-/.]17", r"10[-/.月]17", r"17\s*oct\w*\s*2026",
            r"oct\w*\.?\s*17,?\s*2026", r"semi[- ]?final", r"半决赛",
        ],
    },
    {
        "key": "final",
        "label": "ФИНАЛ (18 окт, вс)",
        "patterns": [
            r"2026[-/.]10[-/.]18", r"10[-/.月]18", r"18\s*oct\w*\s*2026",
            r"oct\w*\.?\s*18,?\s*2026", r"\bfinals?\b", r"决赛",
        ],
        # "Semi-Finals" also contains "final" — never let it count here.
        "reject": [r"semi[- ]?final", r"quarter[- ]?final", r"半决赛",
                   r"2026[-/.]10[-/.]17", r"10[-/.月]17"],
    },
]

SOLD_OUT_WORDS = [
    "sold out", "soldout", "售罄", "已售罄", "无票", "缺货",
    "unavailable", "not available", "暂无", "已售完",
    # Juss marks a sold-out category "Replenishment" (waitlist for a restock).
    "replenishment", "补货", "候补", "缺货登记",
    "we will inform you",
]
AVAILABLE_WORDS = [
    "buy", "purchase", "book now", "select", "add to cart",
    "购买", "立即购买", "抢购", "选座", "有票", "预订",
]

PRICE_KEY_HINTS = ("price", "amount", "money", "facevalue", "fee", "cost")
STOCK_KEY_HINTS = ("stock", "remain", "inventory", "quantity", "qty", "surplus",
                   "available", "count")
STATUS_KEY_HINTS = ("status", "state", "soldout", "sale", "onsale", "saleable")

# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
_send_lock = threading.Lock()


def log(msg):
    stamp = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} SH] {msg}", flush=True)


def tg_send(text, chat_id=None, silent=False):
    if not BOT_TOKEN or not (chat_id or CHAT_ID):
        log("Telegram not configured, message dropped:\n" + text)
        return
    payload = {
        "chat_id": chat_id or CHAT_ID,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    with _send_lock:
        for attempt in range(3):
            try:
                r = requests.post(f"{API}/sendMessage", json=payload, timeout=20)
                if r.ok:
                    return
                log(f"Telegram error {r.status_code}: {r.text[:200]}")
            except Exception as e:
                log(f"Telegram send failed: {e}")
            time.sleep(2 * (attempt + 1))


def tg_send_document(filename, content, caption=""):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"{API}/sendDocument",
            data={"chat_id": CHAT_ID, "caption": caption[:900]},
            files={"document": (filename, content.encode("utf-8", "ignore"))},
            timeout=60,
        )
    except Exception as e:
        log(f"sendDocument failed: {e}")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

DEFAULT_STATE = {
    "available": {},        # key -> bool (was it available on last check)
    "last_alert_ts": {},    # key -> epoch
    "checks": 0,
    "errors": 0,
    "last_ok": None,
    "last_heartbeat_day": None,
    "max_price_usd": MAX_PRICE_USD,
    "paused": False,
}


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULT_STATE)


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Cannot persist state ({e}) — keeping it in memory only.")


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

TEXT_PRICE_RE = re.compile(
    r"(?:(usd|us\$|\$)\s*([\d,]+(?:\.\d{2})?)"
    r"|(cny|rmb|¥|￥)\s*([\d,]+)"
    r"|([\d,]+)\s*(?:yuan|元))", re.I)


def offers_from_text(window_raw):
    """Pull 'CAT 3  Buy  USD 240'-style rows out of rendered page text."""
    offers = []
    for line in window_raw.splitlines():
        m = TEXT_PRICE_RE.search(line)
        if not m:
            continue
        if m.group(2):
            usd = float(m.group(2).replace(",", ""))
            cny = usd * CNY_PER_USD
        else:
            raw = (m.group(4) or m.group(5) or "").replace(",", "")
            if not raw:
                continue
            cny = float(raw)
            usd = cny / CNY_PER_USD
        name = TEXT_PRICE_RE.sub("", line)
        name = re.sub(r"\b(buy|book|select|purchase|from|tickets?)\b", "",
                      name, flags=re.I)
        name = re.sub(r"\s{2,}", " ", name).strip(" -·|,") or "категория не указана"
        offers.append({
            "name": name[:60], "price_cny": cny, "price_usd": usd,
            "stock": None, "available": True,
        })
    # de-duplicate, cheapest first
    seen, uniq = set(), []
    for o in sorted(offers, key=lambda x: x["price_usd"]):
        sig = (o["name"], round(o["price_usd"]))
        if sig not in seen:
            seen.add(sig)
            uniq.append(o)
    return uniq[:10]


def matches_target(blob, target):
    low = blob.lower()
    if any(re.search(p, low) for p in target.get("reject", [])):
        return False
    return any(re.search(p, low) for p in target["patterns"])


def walk_json(node, out, depth=0, ctx=""):
    """Flatten every dict in a JSON tree, carrying ancestors' text along.

    The session date usually sits on a parent object while the price and
    stock sit on child rows, so each row needs its parents' text to be
    matchable against a target date.
    """
    if depth > 12:
        return
    if isinstance(node, dict):
        own = " ".join(
            f"{k}={v}" for k, v in node.items()
            if isinstance(v, (str, int, float)) and not isinstance(v, bool)
        )[:600]
        out.append((node, (ctx + " " + own).strip()[:1500]))
        child_ctx = (ctx + " " + own).strip()[:1500]
        for v in node.values():
            walk_json(v, out, depth + 1, child_ctx)
    elif isinstance(node, list):
        for v in node[:400]:
            walk_json(v, out, depth + 1, ctx)


def pick_number(d, hints):
    for k, v in d.items():
        kl = str(k).lower()
        if any(h in kl for h in hints):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return k, float(v)
            if isinstance(v, str):
                m = re.search(r"\d+(?:\.\d+)?", v.replace(",", ""))
                if m:
                    return k, float(m.group())
    return None, None


def normalise_price_cny(raw):
    """Chinese ticketing APIs often store price in fen (cents)."""
    if raw is None:
        return None
    if raw > 20000:          # 20 000 CNY is way past any realistic ticket
        return raw / 100.0
    return raw


def cny_to_usd(cny):
    return cny / CNY_PER_USD if cny else None


def dict_looks_like_ticket(d):
    keys = " ".join(str(k).lower() for k in d.keys())
    has_price = any(h in keys for h in PRICE_KEY_HINTS)
    has_name = any(h in keys for h in ("name", "title", "seat", "zone", "area",
                                       "cat", "level", "session", "show"))
    return has_price and has_name


def describe_offer(d):
    """Build a short human line out of an unknown ticket-ish dict."""
    name_parts = []
    for k, v in d.items():
        kl = str(k).lower()
        if any(h in kl for h in ("name", "title", "seat", "zone", "area",
                                 "cat", "level", "desc")):
            if isinstance(v, str) and 0 < len(v) < 80:
                name_parts.append(v.strip())
    name = " / ".join(dict.fromkeys(name_parts))[:120] or "сектор не указан"

    _, price_raw = pick_number(d, PRICE_KEY_HINTS)
    price_cny = normalise_price_cny(price_raw)
    price_usd = cny_to_usd(price_cny)

    _, stock = pick_number(d, STOCK_KEY_HINTS)

    return {
        "name": name,
        "price_cny": price_cny,
        "price_usd": price_usd,
        "stock": int(stock) if stock is not None else None,
        "raw": d,
    }


def offer_is_available(d, offer):
    blob = json.dumps(d, ensure_ascii=False).lower()
    if any(w in blob for w in SOLD_OUT_WORDS):
        # explicit sold-out flag wins, unless stock clearly says otherwise
        if not offer["stock"]:
            return False
    if offer["stock"] is not None:
        return offer["stock"] > 0
    for k, v in d.items():
        kl = str(k).lower()
        if any(h in kl for h in STATUS_KEY_HINTS):
            sv = str(v).lower()
            if sv in ("1", "true", "onsale", "on_sale", "available", "normal"):
                return True
            if sv in ("0", "false", "soldout", "sold_out", "off", "end"):
                return False
    return any(w in blob for w in AVAILABLE_WORDS)


# --------------------------------------------------------------------------
# The actual check
# --------------------------------------------------------------------------

class Checker:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.last_snapshot = ""
        self.last_endpoints = []

    def start(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        log("Browser started.")

    def stop(self):
        try:
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
        except Exception:
            pass

    def restart(self):
        log("Restarting browser...")
        self.stop()
        self.start()


    def fetch_booking(self, url):
        """Open the booking page and read each target day of the calendar."""
        context = self.browser.new_context(
            locale="en-US", timezone_id="Asia/Shanghai",
            viewport={"width": 430, "height": 900},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
                "Mobile/15E148 Safari/604.1"
            ),
        )
        payloads, endpoints = [], []

        def on_response(resp):
            try:
                if "json" not in (resp.header_value("content-type") or "").lower():
                    return
                endpoints.append(resp.url.split("?")[0])
                payloads.append({"url": resp.url, "body": resp.json()})
            except Exception:
                pass

        page = context.new_page()
        page.on("response", on_response)
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        day_texts = {}
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(6000)
            for day, key in BOOKING_DAYS:
                try:
                    page.get_by_text(day, exact=True).first.click(timeout=10000)
                    page.wait_for_timeout(4000)
                    day_texts[key] = page.inner_text("body")
                    log(f"booking: read {day} Oct ({len(day_texts[key])} chars)")
                except Exception as e:
                    log(f"booking: cannot open {day} Oct — {e!r}")
        finally:
            try:
                context.close()
            except Exception:
                pass

        self.last_endpoints = sorted(set(endpoints))[:40]
        self.last_text = "\n\n===== ".join(
            f"{k}\n{v}" for k, v in day_texts.items())
        return day_texts, payloads

    def fetch(self, url, mobile=True):
        """Load a shop page, harvest its XHR JSON payloads and visible text."""
        # The Chinese shops are mobile-first; western resale sites serve a
        # stripped-down stub to a phone user agent, so they need desktop.
        if mobile:
            profile = {
                "viewport": {"width": 430, "height": 900},
                "user_agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
                    "Mobile/15E148 Safari/604.1"
                ),
            }
        else:
            profile = {
                "viewport": {"width": 1440, "height": 900},
                "user_agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
            }
        context = self.browser.new_context(
            locale="en-US",
            timezone_id="Asia/Shanghai",
            **profile,
        )
        payloads = []
        endpoints = []

        def on_response(resp):
            try:
                url = resp.url
                ctype = (resp.header_value("content-type") or "").lower()
                if "json" not in ctype:
                    return
                endpoints.append(url.split("?")[0])
                body = resp.json()
                payloads.append({"url": url, "body": body})
            except Exception:
                pass

        page = context.new_page()
        page.on("response", on_response)
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        text = ""
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(6000)

            # The landing page is a list of every event the shop sells
            # (concerts included). Step into the tournament itself.
            hints = (("Center Court", "SHANGHAI MASTERS", "上海大师赛")
                     if mobile else ())
            for hint in hints:
                try:
                    loc = page.get_by_text(hint, exact=False).first
                    if loc.count() == 0:
                        continue
                    loc.click(timeout=8000)
                    page.wait_for_timeout(6000)
                    log(f"Stepped into '{hint}'")
                    break
                except Exception:
                    continue
            # Nudge lazy lists into loading.
            for _ in range(4):
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(1200)
            text = page.inner_text("body")
        finally:
            try:
                context.close()
            except Exception:
                pass

        self.last_endpoints = sorted(set(endpoints))[:40]
        self.last_text = text
        return text, payloads

    def analyse(self, text, payloads):
        """Return {target_key: {'available': bool, 'offers': [...]}}"""
        dicts = []
        for p in payloads:
            walk_json(p["body"], dicts)

        result = {}
        for target in TARGETS:
            offers = []
            for d, ctx in dicts:
                if not isinstance(d, dict) or not dict_looks_like_ticket(d):
                    continue
                blob = json.dumps(d, ensure_ascii=False) + " " + ctx
                if not mentions_event(blob):
                    continue
                low = blob.lower()
                if any(b in low for b in SESSION_BLOCKLIST):
                    continue
                if is_multi_day_range(blob):
                    continue
                if not matches_target(blob, target):
                    continue
                offer = describe_offer(d)
                if offer["price_cny"] is None:
                    continue
                offer["available"] = offer_is_available(d, offer)
                offers.append(offer)

            # de-duplicate by name+price
            seen, uniq = set(), []
            for o in offers:
                sig = (o["name"], round(o["price_cny"] or 0, 2))
                if sig in seen:
                    continue
                seen.add(sig)
                uniq.append(o)
            uniq.sort(key=lambda o: o["price_cny"] or 0)

            available = [o for o in uniq if o["available"]]

            # Fallback: nothing usable in JSON -> read the rendered text.
            text_hint = None
            text_offers = []
            if not uniq and text:
                low_all = text.lower()
                for pat in target["patterns"]:
                    for m in re.finditer(pat, low_all):
                        # Start at the beginning of the matched line...
                        start = low_all.rfind("\n", 0, m.start()) + 1
                        # ...and stop before the next session heading, so one
                        # session never inherits the neighbouring one's prices.
                        end = min(
                            [len(low_all), m.end() + 400]
                            + [mm.start()
                               for mm in SESSION_BOUNDARY_RE.finditer(low_all)
                               if mm.start() > m.end()]
                        )
                        blank = low_all.find("\n\n", m.end())
                        if 0 < blank < end:
                            end = blank
                        window = low_all[start:end]
                        # The tournament name often sits in the page header,
                        # outside the price block — accept either scope.
                        if is_other_event(window):
                            continue
                        if not (has_event_keyword(window)
                                or has_event_keyword(text)):
                            continue
                        if any(b in window for b in SESSION_BLOCKLIST):
                            continue
                        if is_multi_day_range(window):
                            continue
                        if any(w in window for w in AVAILABLE_WORDS) and \
                           not any(w in window for w in SOLD_OUT_WORDS):
                            raw_window = text[start:end]
                            text_offers = offers_from_text(raw_window)
                            text_hint = " / ".join(
                                ln.strip() for ln in raw_window.splitlines()
                                if ln.strip())[:180]
                            break
                    if text_hint:
                        break

            result[target["key"]] = {
                "label": target["label"],
                "offers": uniq,
                "available": bool(available) or bool(text_hint),
                "matched": available or text_offers,
                "text_hint": text_hint,
                "from_text": bool(text_offers) and not available,
                "low_confidence": (not available and bool(text_hint)
                                   and not text_offers),
            }
        return result



# --------------------------------------------------------------------------
# Juss booking page: calendar -> session -> category tiles
# --------------------------------------------------------------------------

BOOKING_DAYS = [("17", "semifinal"), ("18", "final")]
CATEGORY_TOKENS = ["S", "A+", "A", "B"]


def is_booking_page(url):
    return "/booking/" in url


def parse_category_tiles(text):
    """Read the S / A+ / A / B tiles under the 'Price' heading.

    A tile carrying 'Replenishment' is sold out. A tile carrying a number is
    on sale, and that number is its price in CNY.
    """
    low = text.lower()
    i = low.rfind("price")
    seg_raw = text[i:] if i >= 0 else text
    for stop in ("we will inform", "replenishment registered", "select seats"):
        j = seg_raw.lower().find(stop)
        if j > 0:
            seg_raw = seg_raw[:j]
            break

    lines = [ln.strip() for ln in seg_raw.splitlines()]
    offers = []
    for idx, ln in enumerate(lines):
        if ln not in CATEGORY_TOKENS:
            continue
        window = " ".join(lines[max(0, idx - 2): idx + 3]).lower()
        if any(w in window for w in SOLD_OUT_WORDS):
            continue
        m = re.search(r"(?:cny|rmb|¥|￥)?\s*([\d][\d,]{1,6})", window)
        if not m:
            continue
        cny = float(m.group(1).replace(",", ""))
        if cny < 30 or cny > 20000:      # sanity: real tickets are 60-1920
            continue
        offers.append({
            "name": f"Категория {ln}", "price_cny": cny,
            "price_usd": cny / CNY_PER_USD, "stock": None, "available": True,
        })
    uniq, seen = [], set()
    for o in sorted(offers, key=lambda x: x["price_cny"]):
        if o["name"] in seen:
            continue
        seen.add(o["name"])
        uniq.append(o)
    return uniq


def analyse_booking(day_texts):
    result = {}
    for target in TARGETS:
        key = target["key"]
        text = day_texts.get(key, "")
        offers = parse_category_tiles(text) if text else []
        result[key] = {
            "label": target["label"],
            "offers": offers,
            "matched": offers,
            "available": bool(offers),
            "text_hint": None,
            "from_text": False,
            "low_confidence": False,
            "sold_out_note": (None if offers else
                              ("все категории в Replenishment" if text
                               else "страница не открылась")),
        }
    return result


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

SESSION_DATE = {
    "semifinal": "сб 17 октября 2026",
    "final": "вс 18 октября 2026",
}


def fmt_offer(o, max_usd):
    price_usd = o["price_usd"] or 0
    tag = "🟢" if price_usd <= GOOD_PRICE_USD else (
        "🟡" if price_usd <= max_usd else "⚪️")
    stock = f" · {o['stock']} шт" if o["stock"] is not None else ""
    return (f"{tag} {o['name']} — <b>${price_usd:.0f}</b> "
            f"(¥{o['price_cny']:.0f}){stock}")


def build_alert(site, tkey, info, max_usd, qty):
    """Short, scannable message: what, when, how much, where."""
    in_budget = [o for o in info["matched"] if (o["price_usd"] or 1e9) <= max_usd]
    over = [o for o in info["matched"] if (o["price_usd"] or 1e9) > max_usd]
    enough = [o for o in in_budget if o["stock"] is None or o["stock"] >= qty]
    date = SESSION_DATE.get(tkey, "")

    if info.get("low_confidence"):
        # We saw something that looks available but could not read prices.
        return ("🔎 <b>ВОЗМОЖНО ЕСТЬ — нужна ручная проверка</b>\n"
                f"{info['label']} · {date}\n"
                f"{site['label']}\n\n"
                "Цены прочитать не удалось, страница показывает доступность.\n"
                f"<a href=\"{site['url']}\">Открыть и проверить</a>")

    if in_budget:
        head = "🎾🚨 <b>ЕСТЬ БИЛЕТЫ В БЮДЖЕТЕ</b>"
    else:
        head = "🎾 <b>Есть билеты, но дороже потолка</b>"

    lines = [head, f"{info['label']} · {date}", f"Площадка: {site['label']}", ""]

    if in_budget:
        for o in in_budget[:8]:
            lines.append(fmt_offer(o, max_usd))
        lines.append("")
        lines.append(f"✅ На {qty} билета хватает" if enough
                     else f"⚠️ На {qty} билета может не хватить")
    if over:
        cheapest_over = min(o["price_usd"] or 0 for o in over)
        word = "вариант" if len(over) == 1 else "варианта" if len(over) < 5 else "вариантов"
        lines.append(f"Выше ${max_usd:.0f}: {len(over)} {word}, "
                     f"от ${cheapest_over:.0f}")

    if info.get("from_text"):
        lines.append("ℹ️ Цены считаны со страницы — сверь при оформлении.")
    lines.append(f"\n<a href=\"{site['url']}\">Купить на {site['label']}</a>")
    if site["group"] == "slow":
        lines.append("⚠️ Перепродажа: вход по паспорту покупателя.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram command listener (runs in a background thread)
# --------------------------------------------------------------------------

HELP_TEXT = """<b>Shanghai Masters 2026 — монитор билетов</b>

/status — жив ли бот, сколько проверок, текущие настройки
/check — проверить прямо сейчас, не дожидаясь цикла
/last — что было видно на последней проверке\n/sites — список площадок и статус по каждой
/price 300 — поменять потолок цены в долларах
/qty 2 — сколько билетов нужно
/snapshot — прислать сырой дамп страницы и найденных запросов (для отладки)
/pause — приостановить проверки
/resume — возобновить
/help — этот список"""

_commands = {
    "force_check": False,
    "want_snapshot": False,
}


def command_loop(state):
    if not BOT_TOKEN:
        return
    offset = None
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"timeout": 50, "offset": offset},
                             timeout=60)
            data = r.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                if CHAT_ID and chat != str(CHAT_ID):
                    continue  # ignore strangers
                handle_command(text, state, chat)
        except Exception as e:
            log(f"command loop: {e}")
            time.sleep(5)


def handle_command(text, state, chat):
    global TARGET_QTY
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else None

    if cmd in ("/start", "/help"):
        tg_send(HELP_TEXT, chat)
    elif cmd == "/status":
        last = state.get("last_ok") or "—"
        tg_send(
            f"✅ Работаю.\nПроверок: {state['checks']} (ошибок {state['errors']})\n"
            f"Последняя удачная: {last}\n"
            f"Потолок: ${state['max_price_usd']:.0f} | нужно билетов: {TARGET_QTY}\n"
            f"Площадок: {len(SITES)} | интервал {CHECK_INTERVAL}/{SLOW_INTERVAL} сек\n"
            f"Пауза: {'да' if state['paused'] else 'нет'}", chat)
    elif cmd == "/check":
        _commands["force_check"] = True
        tg_send("Проверяю прямо сейчас...", chat)
    elif cmd == "/last":
        reps = list((state.get("reports") or {}).values())
        tg_send("\n\n".join(reps) if reps
                else "Ещё не было ни одной проверки.", chat)
    elif cmd == "/sites":
        lines = []
        for s_ in SITES:
            st = (state.get("per_site") or {}).get(s_["label"], {})
            mark = "✅" if st.get("ok") else ("❌" if st else "…")
            tail = f" — {st.get('err')}" if st and not st.get("ok") else ""
            lines.append(f"{mark} <b>{s_['label']}</b> "
                         f"({'часто' if s_['group'] == 'fast' else 'редко'})"
                         f" {st.get('ts', '')}{tail}")
        tg_send("\n".join(lines), chat)
    elif cmd == "/snapshot":
        _commands["want_snapshot"] = True
        _commands["force_check"] = True
        tg_send("Соберу дамп на ближайшей проверке.", chat)
    elif cmd == "/price" and arg:
        try:
            state["max_price_usd"] = float(arg)
            save_state(state)
            tg_send(f"Потолок теперь ${state['max_price_usd']:.0f}.", chat)
        except ValueError:
            tg_send("Формат: /price 300", chat)
    elif cmd == "/qty" and arg:
        try:
            TARGET_QTY = int(arg)
            tg_send(f"Нужно билетов: {TARGET_QTY}.", chat)
        except ValueError:
            tg_send("Формат: /qty 2", chat)
    elif cmd == "/pause":
        state["paused"] = True
        save_state(state)
        tg_send("⏸ Проверки на паузе. /resume чтобы вернуть.", chat)
    elif cmd == "/resume":
        state["paused"] = False
        save_state(state)
        tg_send("▶️ Продолжаю.", chat)
    else:
        tg_send("Не знаю такой команды.\n\n" + HELP_TEXT, chat)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def interval_now():
    hour = datetime.now(SHANGHAI).hour
    if 2 <= hour < 8:
        return int(CHECK_INTERVAL * QUIET_HOURS_MULTIPLIER)
    return CHECK_INTERVAL


def heartbeat(state):
    today = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    if state.get("last_heartbeat_day") == today:
        return
    if datetime.now(SHANGHAI).hour < 9:
        return
    state["last_heartbeat_day"] = today
    hits = sum(1 for v in state.get("available", {}).values()
               if isinstance(v, dict) and v.get("budget"))
    tg_send(
        f"🫀 Жив. Проверок: {state['checks']} (ошибок {state['errors']}).\n"
        + ("Билетов в бюджете сейчас нет." if not hits
           else f"Сейчас в продаже в бюджете: {hits} позиций — /last"),
        silent=True)
    save_state(state)


def check_site(checker, site, state, forced=False):
    """Run one site, compare with last state, alert on changes."""
    key_prefix = site["label"]
    try:
        if is_booking_page(site["url"]):
            day_texts, payloads = checker.fetch_booking(site["url"])
            text = checker.last_text
            report = analyse_booking(day_texts)
        else:
            text, payloads = checker.fetch(site["url"],
                                           mobile=(site["group"] == "fast"))
            report = checker.analyse(text, payloads)
        state["checks"] += 1
        state["last_ok"] = datetime.now(SHANGHAI).strftime("%d.%m %H:%M")
        state.setdefault("per_site", {})[key_prefix] = {
            "ts": state["last_ok"], "ok": True}
        checker.consecutive_errors = 0
    except Exception as e:
        state["errors"] += 1
        checker.consecutive_errors = getattr(checker, "consecutive_errors", 0) + 1
        state.setdefault("per_site", {})[key_prefix] = {
            "ts": datetime.now(SHANGHAI).strftime("%d.%m %H:%M"),
            "ok": False, "err": str(e)[:120]}
        log(f"[{key_prefix}] check failed: {e!r}")
        if checker.consecutive_errors in (6, 30, 90):
            tg_send(f"⚠️ {checker.consecutive_errors} проверок подряд с ошибкой "
                    f"(последняя — {key_prefix}):\n<code>{str(e)[:300]}</code>")
        if checker.consecutive_errors % 6 == 0:
            checker.restart()
        save_state(state)
        return

    summary = []
    for tkey, info in report.items():
        n = len(info["matched"])
        cheapest = min((o["price_usd"] for o in info["matched"] if o["price_usd"]),
                       default=None)
        summary.append(
            f"{info['label']}: "
            + (f"{n} вар., от ${cheapest:.0f}" if n and cheapest else
               ("признаки наличия" if info["available"] else
                info.get("sold_out_note") or "нет")))
    log(f"[{key_prefix}] " + " | ".join(summary))
    state.setdefault("reports", {})[key_prefix] = (
        f"<b>{key_prefix}</b> ({state['last_ok']})\n" + "\n".join(summary))

    for tkey, info in report.items():
        key = f"{key_prefix}::{tkey}"
        prev = state["available"].get(key) or {}
        if isinstance(prev, bool):
            prev = {"any": prev, "budget": []}
        budget_sigs = sorted(
            f"{o['name']}|{o['price_cny']:.0f}"
            for o in info["matched"]
            if (o["price_usd"] or 1e9) <= state["max_price_usd"]
        )
        new_budget = [x for x in budget_sigs if x not in prev.get("budget", [])]
        was = prev.get("any", False) and not new_budget
        now = info["available"]
        cooldown = 43200 if info.get("low_confidence") else 0
        last = state["last_alert_ts"].get(key, 0)
        if now and not was and time.time() - last >= cooldown:
            tg_send(build_alert(site, tkey, info,
                                state["max_price_usd"], TARGET_QTY))
            state["last_alert_ts"][key] = time.time()
        elif now and was and not info.get("low_confidence"):
            if time.time() - last > 3600:
                tg_send("🔁 Всё ещё в продаже:\n\n" +
                        build_alert(site, tkey, info,
                                    state["max_price_usd"], TARGET_QTY),
                        silent=True)
                state["last_alert_ts"][key] = time.time()
        state["available"][key] = {"any": now, "budget": budget_sigs}

    if _commands["want_snapshot"]:
        dump = {
            "site": site,
            "endpoints": checker.last_endpoints,
            "page_text": text[:8000],
            "report": {
                k: {"available": v["available"],
                    "offers": [{kk: vv for kk, vv in o.items() if kk != "raw"}
                               for o in v["offers"][:20]]}
                for k, v in report.items()},
        }
        tg_send_document(
            f"snapshot-{re.sub(r'[^a-zA-Z0-9]+', '-', key_prefix)}.json",
            json.dumps(dump, ensure_ascii=False, indent=2),
            f"Дамп: {key_prefix}")

    save_state(state)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        log("FATAL: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        sys.exit(1)

    state = load_state()
    threading.Thread(target=command_loop, args=(state,), daemon=True).start()

    fast = [s_["label"] for s_ in SITES if s_["group"] == "fast"]
    slow = [s_["label"] for s_ in SITES if s_["group"] == "slow"]
    tg_send(
        "🎾 Монитор Shanghai Masters 2026 запущен.\n"
        f"Полуфинал (17.10) и финал (18.10), потолок "
        f"${state['max_price_usd']:.0f}, нужно {TARGET_QTY} билета.\n\n"
        f"Часто ({CHECK_INTERVAL} сек): {', '.join(fast) or '—'}\n"
        f"Редко ({SLOW_INTERVAL} сек): {', '.join(slow) or '—'}\n\n"
        "/help — список команд.")

    checker = Checker()
    checker.start()
    consecutive_errors = 0

    site_due = {s["url"]: 0.0 for s in SITES}

    try:
        while True:
            if state["paused"] and not _commands["force_check"]:
                time.sleep(5)
                continue
            forced = _commands["force_check"]
            _commands["force_check"] = False

            due = [s for s in SITES
                   if forced or time.time() >= site_due[s["url"]]]
            if not due:
                time.sleep(5)
                continue

            for site in due:
                gap = (interval_now() if site["group"] == "fast"
                       else SLOW_INTERVAL)
                site_due[site["url"]] = time.time() + gap
                check_site(checker, site, state, forced)

            if _commands["want_snapshot"]:
                _commands["want_snapshot"] = False

            heartbeat(state)
            save_state(state)
            time.sleep(3)
    finally:
        checker.stop()


if __name__ == "__main__":
    main()
