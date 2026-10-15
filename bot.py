"""
Absan Saudi Trends Bot
----------------------
يجلب ترندات X في السعودية (عبر Apify) مرة كل ساعة، ويرسل الترندات الجديدة
لجروب تيليغرام، ويرد على أمر /trends بآخر قائمة محفوظة.

يشتغل على GitHub Actions. الأسرار تُقرأ من Secrets ولا تُكتب في الكود أبداً.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ======================= الإعدادات (يمكن تعديلها) =======================
FETCH_EVERY_MINUTES = 60          # كل قديش نجيب ترندات جديدة من Apify
RETRY_AFTER_FAIL_MINUTES = 20     # إذا فشل الجلب، متى نعيد المحاولة
MAX_TRENDS = 15                   # عدد الترندات في كل جلب
REMEMBER_DAYS = 3                 # ما نعيد إرسال نفس الترند خلال هالمدة
MAX_CHARGE_PER_RUN_USD = 0.03     # سقف أمان لتكلفة كل تشغيل على Apify
APIFY_ACTOR = "labrat011~x-trends-scraper"
LOCATION = "SA"                   # السعودية

# كلمات تدل على ترندات إعلانية/مزعجة (يمكن الإضافة عليها)
SPAM_WORDS = ["تسديد", "قروض", "تشليح", "تشاليح", "واتساب", "واتس_اب"]
SPAM_DIGITS = re.compile(r"[0-9٠-٩]{7,}")  # أرقام طويلة = غالباً رقم تلفون

STATE_FILE = "state.json"
SAUDI_TZ = ZoneInfo("Asia/Riyadh")
# =======================================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()


# ----------------------------- أدوات عامة -----------------------------
def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def saudi_time(dt=None):
    """يرجع الوقت بتوقيت السعودية بصيغة مثل: 8:35 مساءً"""
    dt = (dt or now_utc()).astimezone(SAUDI_TZ)
    hour = dt.hour % 12 or 12
    period = "صباحاً" if dt.hour < 12 else "مساءً"
    return f"{hour}:{dt.minute:02d} {period}"


def trend_key(name):
    """مفتاح موحّد لمنع التكرار (بدون # وبدون فرق بين _ والمسافة)"""
    return name.strip().lstrip("#").replace("_", " ").casefold()


def is_spam(name):
    if SPAM_DIGITS.search(name):
        return True
    return any(word in name for word in SPAM_WORDS)


def x_link(trend):
    url = trend.get("url") or ""
    if url.startswith("https://twitter.com/"):
        url = "https://x.com/" + url[len("https://twitter.com/"):]
    if not url.startswith("https://x.com/"):
        url = "https://x.com/search?q=" + urllib.parse.quote(trend["name"])
    return url


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ----------------------------- تيليغرام -----------------------------
def telegram(method, **params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            body = {"ok": False, "description": f"HTTP {e.code}"}
        print(f"Telegram {method} failed: {body.get('description')}")
        return body
    except Exception as e:  # مشاكل شبكة
        print(f"Telegram {method} network error: {type(e).__name__}")
        return {"ok": False}


def send_message(text, reply_to=None):
    params = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    for _ in range(3):
        res = telegram("sendMessage", **params)
        if res.get("ok"):
            return True
        retry = (res.get("parameters") or {}).get("retry_after")
        if not retry:
            return False
        time.sleep(min(int(retry), 30))
    return False


def format_alert(trend, found_at):
    name = html.escape(trend["name"])
    label = "الهاشتاق" if trend["name"].startswith("#") else "الترند"
    lines = [
        "🚨 <b>ترند جديد في السعودية</b> 🇸🇦",
        "",
        f"🔥 {label}: <b>{name}</b>",
    ]
    if trend.get("rank"):
        lines.append(f"📊 الترتيب: {trend['rank']}")
    lines += [
        f"⏰ وقت الاكتشاف: {saudi_time(found_at)}",
        f'🔗 <a href="{html.escape(x_link(trend), quote=True)}">رابط الترند على X</a>',
    ]
    return "\n".join(lines)


def format_list(trends, updated_at, title="🇸🇦 <b>الترندات الحالية في السعودية</b>"):
    if not trends:
        return "⏳ ما في قائمة ترندات محفوظة لسا. جرّب بعد شوي."
    lines = [title, f"⏰ آخر تحديث: {saudi_time(updated_at)}", ""]
    for i, t in enumerate(trends, 1):
        link = html.escape(x_link(t), quote=True)
        lines.append(f'{i}. <a href="{link}">{html.escape(t["name"])}</a>')
    return "\n".join(lines)


def handle_commands(state, reply_enabled):
    """يقرأ الرسائل الجديدة ويرد على /trends داخل الجروب فقط"""
    res = telegram("getUpdates", offset=state.get("update_offset", 0), timeout=0,
                   allowed_updates=["message"])
    if not res.get("ok"):
        return
    for update in res.get("result", []):
        state["update_offset"] = update["update_id"] + 1
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != CHAT_ID:
            continue  # تجاهل أي محادثة غير جروب الوكالة
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        command = text.split()[0].split("@")[0].lower()
        if command in ("/trends", "/start") and reply_enabled:
            updated = parse_time(state.get("last_fetch"))
            send_message(format_list(state.get("trends", []), updated),
                         reply_to=msg.get("message_id"))


# ----------------------------- Apify -----------------------------
def fetch_trends():
    payload = json.dumps({"locations": [LOCATION], "maxTrendsPerLocation": MAX_TRENDS}).encode()
    base = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?timeout=120"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {APIFY_TOKEN}"}

    def call(url):
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=200) as resp:
            return json.load(resp)

    try:
        items = call(f"{base}&maxTotalChargeUsd={MAX_CHARGE_PER_RUN_USD}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if "max-total-charge" in body:  # إذا رفض السقف، نجرب بدونه
            items = call(base)
        else:
            raise RuntimeError(f"Apify HTTP {e.code}: {body[:300]}")

    trends = []
    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        trends.append({
            "name": name,
            "rank": item.get("rank"),
            "url": item.get("searchUrl"),
        })
    trends.sort(key=lambda t: t["rank"] or 999)
    return trends


# ----------------------------- التشغيل -----------------------------
def main():
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
                              ("TELEGRAM_CHAT_ID", CHAT_ID),
                              ("APIFY_TOKEN", APIFY_TOKEN)] if not v]
    if missing:
        print("Missing secrets:", ", ".join(missing))
        sys.exit(1)

    state = load_state()
    now = now_utc()
    first_run = not state.get("initialized")

    next_fetch = parse_time(state.get("next_fetch"))
    if next_fetch is None or now >= next_fetch:
        try:
            trends = fetch_trends()
            clean = [t for t in trends if not is_spam(t["name"])]
            print(f"Fetched {len(trends)} trends, {len(trends) - len(clean)} filtered as spam.")

            # ننسى الترندات القديمة جداً
            seen = {k: v for k, v in state.get("seen", {}).items()
                    if (parse_time(v) or now) > now - timedelta(days=REMEMBER_DAYS)}
            new = [t for t in clean if trend_key(t["name"]) not in seen]

            if first_run:
                intro = ("✅ <b>بوت ترندات عبسان اشتغل!</b>\n"
                         "رح أبعتلكم كل ترند جديد بالسعودية أول ما يطلع.\n"
                         "واكتبوا /trends بأي وقت لتشوفوا القائمة الحالية.\n\n")
                send_message(intro + format_list(clean, now))
            else:
                for t in new:
                    send_message(format_alert(t, now))
                    time.sleep(1)
                print(f"Sent {len(new)} new trend alerts.")

            for t in clean:
                seen[trend_key(t["name"])] = now.isoformat()

            state.update({
                "trends": clean,
                "seen": seen,
                "last_fetch": now.isoformat(),
                "next_fetch": (now + timedelta(minutes=FETCH_EVERY_MINUTES)).isoformat(),
            })
            if first_run:
                telegram("setMyCommands", commands=[
                    {"command": "trends", "description": "عرض ترندات السعودية الحالية"}])
                state["initialized"] = True
        except Exception as e:
            print(f"Fetching trends failed: {e}")
            state["next_fetch"] = (now + timedelta(minutes=RETRY_AFTER_FAIL_MINUTES)).isoformat()

    # بأول تشغيل نتجاهل الأوامر القديمة لأن القائمة انبعتت أصلاً
    handle_commands(state, reply_enabled=not first_run)
    save_state(state)


if __name__ == "__main__":
    main()
