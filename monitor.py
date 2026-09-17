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

# Price ceiling per single ticket, in USD.
MAX_PRICE_USD = float(os.environ.get("MAX_PRICE_USD", "300"))
# "Optimal" price — anything at or below this gets the loud alert wording.
GOOD_PRICE_USD = float(os.environ.get("GOOD_PRICE_USD", "200"))
# CNY -> USD. Update if the rate drifts a lot.
CNY_PER_USD = float(os.environ.get("CNY_PER_USD", "7.1"))

# How many tickets you actually need per session.
TARGET_QTY = int(os.environ.get("TARGET_QTY", "2"))

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "90"))
# Slow down between 02:00 and 08:00 Shanghai time (nothing drops at night).
QUIET_HOURS_MULTIPLIER = float(os.environ.get("QUIET_HOURS_MULTIPLIER", "3"))

STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "45000"))

SHANGHAI = timezone(timedelta(hours=8))

# Sessions we care about. Tournament runs 5-18 Oct 2026:
# semi-finals Sat 17 Oct, final Sun 18 Oct.
TARGETS = [
    {
        "key": "semifinal",
        "label": "ПОЛУФИНАЛ (17 окт, сб)",
        "patterns": [
            r"10[-/.月]?17", r"17[-/.]10", r"oct\w*\.?\s*17", r"17\s*oct",
            r"2026-10-17", r"semi[- ]?final", r"半决赛",
        ],
    },
    {
        "key": "final",
        "label": "ФИНАЛ (18 окт, вс)",
        "patterns": [
            r"10[-/.月]?18", r"18[-/.]10", r"oct\w*\.?\s*18", r"18\s*oct",
            r"2026-10-18", r"\bfinal\b", r"决赛",
        ],
    },
]

SOLD_OUT_WORDS = [
    "sold out", "soldout", "售罄", "已售罄", "无票", "缺货",
    "unavailable", "not available", "暂无", "已售完",
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

def matches_target(blob, target):
    low = blob.lower()
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

    def fetch(self):
        """Load the shop, harvest every XHR JSON payload and the page text."""
        context = self.browser.new_context(
            locale="en-US",
            timezone_id="Asia/Shanghai",
            viewport={"width": 430, "height": 900},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
                "Mobile/15E148 Safari/604.1"
            ),
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
            page.goto(TICKET_URL, wait_until="domcontentloaded",
                      timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(6000)
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
            if not uniq and text:
                low_all = text.lower()
                for pat in target["patterns"]:
                    m = re.search(pat, low_all)
                    if not m:
                        continue
                    start = max(0, low_all.rfind("\n\n", 0, m.start()) + 1)
                    nxt = low_all.find("\n\n", m.end())
                    end = nxt if 0 < nxt < m.end() + 600 else m.end() + 400
                    window = low_all[start:end]
                    if any(w in window for w in AVAILABLE_WORDS) and \
                       not any(w in window for w in SOLD_OUT_WORDS):
                        text_hint = text[start:end].strip()[:400]
                        break

            result[target["key"]] = {
                "label": target["label"],
                "offers": uniq,
                "available": bool(available) or bool(text_hint),
                "matched": available,
                "text_hint": text_hint,
            }
        return result


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

def fmt_offer(o, max_usd):
    price_usd = o["price_usd"] or 0
    tag = "🟢" if price_usd <= GOOD_PRICE_USD else (
        "🟡" if price_usd <= max_usd else "⚪️")
    stock = f", осталось ~{o['stock']}" if o["stock"] is not None else ""
    return (f"{tag} <b>{o['name']}</b> — ¥{o['price_cny']:.0f} "
            f"(≈${price_usd:.0f}){stock}")


def build_alert(label, info, max_usd, qty):
    in_budget = [o for o in info["matched"] if (o["price_usd"] or 1e9) <= max_usd]
    over = [o for o in info["matched"] if (o["price_usd"] or 1e9) > max_usd]
    enough = [o for o in in_budget
              if o["stock"] is None or o["stock"] >= qty]

    head = "🎾🚨 <b>ЕСТЬ БИЛЕТЫ</b>" if in_budget else "🎾 <b>Появились билеты (дороже потолка)</b>"
    lines = [f"{head}\n<b>{label}</b>", ""]

    if in_budget:
        lines.append(f"В бюджете (до ${max_usd:.0f}):")
        lines += [fmt_offer(o, max_usd) for o in in_budget[:12]]
        if enough:
            lines.append(f"\n✅ Хватает на {qty} билета минимум в одной категории.")
        else:
            lines.append(f"\n⚠️ Может не хватить на {qty} — проверь при оформлении.")
    if over:
        lines.append(f"\nВыше потолка:")
        lines += [fmt_offer(o, max_usd) for o in over[:6]]
    if not info["matched"] and info["text_hint"]:
        lines.append("Страница показывает доступность, но цены распарсить не вышло:")
        lines.append(f"<code>{info['text_hint']}</code>")

    lines.append(f"\n👉 <a href=\"{TICKET_URL}\">Открыть магазин и купить</a>")
    lines.append("Покупай сам, руками — бот только сигналит.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram command listener (runs in a background thread)
# --------------------------------------------------------------------------

HELP_TEXT = """<b>Shanghai Masters 2026 — монитор билетов</b>

/status — жив ли бот, сколько проверок, текущие настройки
/check — проверить прямо сейчас, не дожидаясь цикла
/last — что было видно на последней проверке
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
            f"Интервал: {CHECK_INTERVAL} сек\n"
            f"Пауза: {'да' if state['paused'] else 'нет'}", chat)
    elif cmd == "/check":
        _commands["force_check"] = True
        tg_send("Проверяю прямо сейчас...", chat)
    elif cmd == "/last":
        tg_send(state.get("last_report") or "Ещё не было ни одной проверки.", chat)
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
    tg_send(
        f"🫀 Жив. За сутки проверок всего: {state['checks']} "
        f"(ошибок {state['errors']}). Билетов в бюджете пока нет.",
        silent=True)
    save_state(state)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        log("FATAL: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        sys.exit(1)

    state = load_state()
    threading.Thread(target=command_loop, args=(state,), daemon=True).start()

    tg_send(
        "🎾 Монитор Shanghai Masters 2026 запущен.\n"
        f"Слежу за полуфиналом (17.10) и финалом (18.10), "
        f"потолок ${state['max_price_usd']:.0f}, нужно {TARGET_QTY} билета.\n"
        "/help — список команд.")

    checker = Checker()
    checker.start()
    consecutive_errors = 0

    try:
        while True:
            if state["paused"] and not _commands["force_check"]:
                time.sleep(10)
                continue
            _commands["force_check"] = False

            try:
                text, payloads = checker.fetch()
                report = checker.analyse(text, payloads)
                state["checks"] += 1
                state["last_ok"] = datetime.now(SHANGHAI).strftime("%d.%m %H:%M")
                consecutive_errors = 0

                summary_lines = []
                for key, info in report.items():
                    n = len(info["matched"])
                    cheapest = min(
                        (o["price_usd"] for o in info["matched"] if o["price_usd"]),
                        default=None)
                    summary_lines.append(
                        f"{info['label']}: "
                        + (f"{n} вариантов, от ${cheapest:.0f}"
                           if n and cheapest else
                           ("есть признаки наличия" if info["available"]
                            else "нет билетов")))
                state["last_report"] = ("Последняя проверка "
                                        f"{state['last_ok']}\n"
                                        + "\n".join(summary_lines))
                log(" | ".join(summary_lines))

                for key, info in report.items():
                    prev = state["available"].get(key) or {}
                    if isinstance(prev, bool):      # migrate old state format
                        prev = {"any": prev, "budget": []}
                    budget_sigs = sorted(
                        f"{o['name']}|{o['price_cny']:.0f}"
                        for o in info["matched"]
                        if (o["price_usd"] or 1e9) <= state["max_price_usd"]
                    )
                    new_budget = [s for s in budget_sigs
                                  if s not in prev.get("budget", [])]
                    was = prev.get("any", False) and not new_budget
                    now = info["available"]
                    info_state = {"any": now, "budget": budget_sigs}
                    if now and not was:
                        tg_send(build_alert(info["label"], info,
                                            state["max_price_usd"], TARGET_QTY))
                        state["last_alert_ts"][key] = time.time()
                    elif now and was:
                        # still available — remind once an hour, quietly
                        last = state["last_alert_ts"].get(key, 0)
                        if time.time() - last > 3600:
                            tg_send("🔁 Всё ещё в продаже:\n\n" +
                                    build_alert(info["label"], info,
                                                state["max_price_usd"],
                                                TARGET_QTY), silent=True)
                            state["last_alert_ts"][key] = time.time()
                    state["available"][key] = info_state

                if _commands["want_snapshot"]:
                    _commands["want_snapshot"] = False
                    dump = {
                        "endpoints": checker.last_endpoints,
                        "page_text": text[:8000],
                        "report": {
                            k: {"available": v["available"],
                                "offers": [
                                    {kk: vv for kk, vv in o.items() if kk != "raw"}
                                    for o in v["offers"][:20]]}
                            for k, v in report.items()},
                    }
                    tg_send_document(
                        "snapshot.json",
                        json.dumps(dump, ensure_ascii=False, indent=2),
                        "Дамп: какие запросы делает сайт и что распарсилось.")

                heartbeat(state)
                save_state(state)

            except Exception as e:
                state["errors"] += 1
                consecutive_errors += 1
                log("check failed: " + repr(e))
                traceback.print_exc()
                if consecutive_errors in (5, 20, 60):
                    tg_send(f"⚠️ {consecutive_errors} проверок подряд с ошибкой: "
                            f"<code>{str(e)[:300]}</code>")
                if consecutive_errors % 5 == 0:
                    checker.restart()
                save_state(state)

            time.sleep(interval_now())
    finally:
        checker.stop()


if __name__ == "__main__":
    main()
