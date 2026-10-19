"""
Absan Client Sources Bot
------------------------
يراقب حسابات X اللي حددها كل عميل (ملف sources.json)، ويبعت كل تغريدة جديدة
لجروب تيليغرام أول ما تنزل، مع اسم العميل ورابط التغريدة.

الأوامر داخل الجروب:
  /latest  آخر الأخبار المحفوظة لكل عميل
  /sources الحسابات اللي البوت بيراقبها

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
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

# ======================= الإعدادات (يمكن تعديلها) =======================
FETCH_EVERY_MINUTES = 60          # كل قديش نفحص الحسابات (كل فحص = طلب Apify لكل عميل)
RETRY_AFTER_FAIL_MINUTES = 20     # إذا فشل الفحص، متى نعيد المحاولة
MAX_ITEMS_PER_QUERY = 40          # أقصى عدد تغريدات نطلبها بكل طلب
HANDLES_PER_QUERY = 8             # كم حساب نجمع بطلب واحد
MAX_ALERTS_PER_RUN = 20           # سقف التنبيهات بكل فحص حتى ما ينغرق الجروب
MAX_AGE_HOURS = 12                # ما نبعت تغريدة أقدم من هيك (خبر بارد)
OVERLAP_MINUTES = 15              # نرجع لورا شوي بكل فحص حتى ما تفوتنا تغريدة
LATEST_PER_CLIENT = 8             # كم خبر نعرض لكل عميل بأمر /latest
TEXT_LIMIT = 700                  # أقصى طول لنص التغريدة بالرسالة
MAX_CHARGE_PER_RUN_USD = 0.05     # سقف أمان لتكلفة كل طلب على Apify
APIFY_ACTOR = "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest"
INCLUDE_REPLIES = False           # الردود على الناس ما بتهمنا
INCLUDE_RETWEETS = False          # إعادة النشر ما بتهمنا

STATE_FILE = "state.json"
SOURCES_FILE = "sources.json"
STATE_VERSION = 2                 # النسخة 2 = مراقبة مصادر العملاء
SAUDI_TZ = ZoneInfo("Asia/Riyadh")
# =======================================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


# ----------------------------- أدوات عامة -----------------------------
def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_tweet_time(value):
    """يقبل صيغة X القديمة (Thu Oct 17 09:30:41 +0000 2024) أو ISO"""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / (1000 if value > 1e12 else 1), timezone.utc)
    dt = parse_time(str(value).replace("Z", "+00:00"))
    if dt:
        return dt
    try:
        return datetime.strptime(str(value), "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None


def saudi_time(dt=None, with_date=False):
    """يرجع الوقت بتوقيت السعودية بصيغة مثل: 8:35 مساءً"""
    dt = (dt or now_utc()).astimezone(SAUDI_TZ)
    hour = dt.hour % 12 or 12
    period = "صباحاً" if dt.hour < 12 else "مساءً"
    text = f"{hour}:{dt.minute:02d} {period}"
    if with_date:
        text = f"{dt.day}/{dt.month} · {text}"
    return text


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_sources():
    """يقرأ sources.json ويتأكد إنه سليم. أي غلط بيطلع برسالة واضحة بسجل التشغيل"""
    try:
        data = load_json(SOURCES_FILE, None)
    except json.JSONDecodeError as e:
        sys.exit(f"ملف {SOURCES_FILE} فيه خطأ بالكتابة (سطر {e.lineno}): غالباً فاصلة أو علامة تنصيص ناقصة.")
    if not data or not isinstance(data.get("clients"), list):
        sys.exit(f"ملف {SOURCES_FILE} مش موجود أو ما فيه قائمة clients.")

    clients = []
    for c in data["clients"]:
        name = str(c.get("name") or "").strip()
        handles = []
        for h in c.get("x_accounts") or []:
            h = str(h).strip().lstrip("@")
            if h.startswith("http"):
                h = urllib.parse.urlparse(h).path.strip("/").split("/")[0]
            if HANDLE_RE.match(h):
                handles.append(h)
            else:
                print(f"تجاهلت حساب غير صالح عند {name}: {h!r}")
        if name and handles:
            clients.append({"name": name, "emoji": c.get("emoji") or "📰", "handles": handles})
    if not clients:
        sys.exit(f"ملف {SOURCES_FILE} ما فيه ولا عميل عنده حسابات صالحة.")
    return clients


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


def send_message(text, reply_to=None, preview=False):
    params = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": not preview},
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


def shorten(text, limit=TEXT_LIMIT):
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def format_alert(tweet, client):
    source = html.escape(tweet["author_name"] or tweet["handle"])
    lines = [
        f"{client['emoji']} <b>خبر جديد · {html.escape(client['name'])}</b>",
        "",
        f"🏛 <b>{source}</b> (@{html.escape(tweet['handle'])})",
        "",
        html.escape(shorten(tweet["text"])),
        "",
        f"⏰ نُشر: {saudi_time(parse_time(tweet['created_at']), with_date=True)}",
        f'🔗 <a href="{html.escape(tweet["url"], quote=True)}">فتح التغريدة على X</a>',
    ]
    return "\n".join(lines)


def format_latest(state, clients):
    recent = state.get("recent", {})
    blocks = ["🗂 <b>آخر أخبار مصادر العملاء</b>"]
    if state.get("last_fetch"):
        blocks[0] += f"\n⏰ آخر فحص: {saudi_time(parse_time(state['last_fetch']))}"
    for c in clients:
        items = recent.get(c["name"], [])[:LATEST_PER_CLIENT]
        lines = [f"\n{c['emoji']} <b>{html.escape(c['name'])}</b>"]
        if not items:
            lines.append("ما في أخبار جديدة لسا.")
        for i, t in enumerate(items, 1):
            snippet = html.escape(shorten(t["text"].replace("\n", " "), 90))
            link = html.escape(t["url"], quote=True)
            lines.append(f'{i}. <a href="{link}">@{html.escape(t["handle"])}</a>: {snippet}')
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def format_sources(clients):
    lines = ["📡 <b>الحسابات اللي البوت بيراقبها</b>",
             f"يفحص كل {FETCH_EVERY_MINUTES} دقيقة تقريباً."]
    for c in clients:
        lines.append(f"\n{c['emoji']} <b>{html.escape(c['name'])}</b>")
        for h in c["handles"]:
            lines.append(f'• <a href="https://x.com/{h}">@{h}</a>')
    return "\n".join(lines)


def handle_commands(state, clients, reply_enabled):
    """يقرأ الرسائل الجديدة ويرد على الأوامر داخل الجروب فقط"""
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
        if not text.startswith("/") or not reply_enabled:
            continue
        command = text.split()[0].split("@")[0].lower()
        if command in ("/latest", "/trends", "/start"):
            send_message(format_latest(state, clients), reply_to=msg.get("message_id"))
        elif command == "/sources":
            send_message(format_sources(clients), reply_to=msg.get("message_id"))


# ----------------------------- Apify -----------------------------
def build_query(handles, since):
    query = "(" + " OR ".join(f"from:{h}" for h in handles) + ")"
    if not INCLUDE_REPLIES:
        query += " -filter:replies"
    if since:
        query += f" since_time:{int(since.timestamp())}"
    return query


def run_actor(query):
    payload = json.dumps({
        "twitterContent": query,
        "queryType": "Latest",
        "maxItems": MAX_ITEMS_PER_QUERY,
    }).encode()
    base = (f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
            f"?timeout=120")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {APIFY_TOKEN}"}

    def call(url):
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=200) as resp:
            return json.load(resp)

    try:
        return call(f"{base}&maxTotalChargeUsd={MAX_CHARGE_PER_RUN_USD}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if "max-total-charge" in body or "maxTotalChargeUsd" in body:
            return call(base)  # إذا رفض السقف، نجرب بدونه
        raise RuntimeError(f"Apify HTTP {e.code}: {body[:300]}")


def normalize(item, wanted):
    """يحوّل نتيجة Apify لشكل موحّد، ويرمي البيانات الوهمية والردود"""
    if not isinstance(item, dict):
        return None
    tid = str(item.get("id") or item.get("id_str") or "")
    if not tid.isdigit() or "mock" in str(item.get("type", "")).lower():
        return None  # بيانات وهمية بترجع لما ما في نتائج
    author = item.get("author") or item.get("user") or {}
    handle = (author.get("userName") or author.get("screen_name") or "").lstrip("@")
    if handle.lower() not in wanted:
        return None
    if not INCLUDE_REPLIES and (item.get("isReply") or item.get("inReplyToId")):
        return None
    if not INCLUDE_RETWEETS and (item.get("isRetweet") or item.get("retweeted_tweet")):
        return None
    created = parse_tweet_time(item.get("createdAt") or item.get("created_at"))
    if not created:
        return None
    return {
        "id": tid,
        "handle": wanted[handle.lower()],
        "author_name": (author.get("name") or "").strip(),
        "text": (item.get("text") or item.get("fullText") or item.get("full_text") or "").strip(),
        "created_at": created.isoformat(),
        "url": f"https://x.com/{wanted[handle.lower()]}/status/{tid}",
    }


def fetch_client(client, since):
    wanted = {h.lower(): h for h in client["handles"]}
    tweets = {}
    for i in range(0, len(client["handles"]), HANDLES_PER_QUERY):
        chunk = client["handles"][i:i + HANDLES_PER_QUERY]
        items = run_actor(build_query(chunk, since))
        for item in items or []:
            t = normalize(item, wanted)
            if t:
                tweets[t["id"]] = t
    return sorted(tweets.values(), key=lambda t: t["created_at"])


# ----------------------------- التشغيل -----------------------------
def main():
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
                              ("TELEGRAM_CHAT_ID", CHAT_ID),
                              ("APIFY_TOKEN", APIFY_TOKEN)] if not v]
    if missing:
        sys.exit("Missing secrets: " + ", ".join(missing))

    clients = load_sources()
    state = load_json(STATE_FILE, {})
    now = now_utc()

    # أول تشغيل بالنسخة الجديدة: نمسح ذاكرة الترندات القديمة ونحتفظ بس بمكان قراءة الأوامر
    first_run = state.get("version") != STATE_VERSION
    if first_run:
        state = {"version": STATE_VERSION, "update_offset": state.get("update_offset", 0)}

    next_fetch = parse_time(state.get("next_fetch"))
    if next_fetch is None or now >= next_fetch:
        seen = {k: v for k, v in state.get("seen", {}).items()
                if (parse_time(v) or now) > now - timedelta(days=3)}
        recent = state.get("recent", {})
        last_fetch = parse_time(state.get("last_fetch"))
        oldest_allowed = now - timedelta(hours=MAX_AGE_HOURS)
        since = max(last_fetch - timedelta(minutes=OVERLAP_MINUTES), oldest_allowed) \
            if last_fetch else oldest_allowed

        failed, sent, alerts = [], 0, []
        for c in clients:
            try:
                tweets = fetch_client(c, since)
            except Exception as e:
                print(f"Fetching {c['handles']} failed: {e}")
                failed.append(c["name"])
                continue
            fresh = [t for t in tweets if t["id"] not in seen
                     and parse_time(t["created_at"]) >= oldest_allowed]
            print(f"{c['name']}: {len(tweets)} tweets, {len(fresh)} new.")
            for t in fresh:
                seen[t["id"]] = now.isoformat()
                alerts.append((t, c))
            keep = {t["id"]: t for t in fresh + recent.get(c["name"], [])}
            recent[c["name"]] = sorted(keep.values(), key=lambda t: t["created_at"],
                                       reverse=True)[:LATEST_PER_CLIENT]

        if first_run:
            intro = ("✅ <b>تحديث بوت عبسان: صار يراقب مصادر العملاء</b>\n"
                     "بدل الترندات العامة، رح أبعتلكم كل تغريدة جديدة من الجهات اللي حددها كل عميل، "
                     "أول ما تنزل.\n\n"
                     "• /latest آخر الأخبار المحفوظة لكل عميل\n"
                     "• /sources الحسابات اللي براقبها\n\n")
            send_message(intro + format_sources(clients))
        else:
            alerts.sort(key=lambda a: a[0]["created_at"])
            for t, c in alerts[-MAX_ALERTS_PER_RUN:]:
                if send_message(format_alert(t, c)):
                    sent += 1
                time.sleep(1)
            print(f"Sent {sent} alerts ({len(alerts)} new).")

        state.update({"seen": seen, "recent": recent})
        if len(failed) == len(clients):
            state["next_fetch"] = (now + timedelta(minutes=RETRY_AFTER_FAIL_MINUTES)).isoformat()
            state["fail_streak"] = state.get("fail_streak", 0) + 1
            if state["fail_streak"] == 3:  # تنبيه وحيد حتى ما يضل البوت ساكت وهو معطّل
                send_message("⚠️ <b>البوت ما قدر يجيب الأخبار من 3 محاولات متتالية.</b>\n"
                             "افحصوا تبويب Actions على GitHub ورصيد Apify.")
        else:
            state["fail_streak"] = 0
            # إذا فشل عميل واحد بس، منكمل وبنعيده بالفحص الجاي (since بيرجع لآخر فحص ناجح)
            if not failed:
                state["last_fetch"] = now.isoformat()
            state["next_fetch"] = (now + timedelta(
                minutes=RETRY_AFTER_FAIL_MINUTES if failed else FETCH_EVERY_MINUTES)).isoformat()

        if first_run:
            telegram("setMyCommands", commands=[
                {"command": "latest", "description": "آخر أخبار مصادر العملاء"},
                {"command": "sources", "description": "الحسابات اللي البوت بيراقبها"},
            ])

    # بأول تشغيل نتجاهل الأوامر القديمة
    handle_commands(state, clients, reply_enabled=not first_run)
    save_state(state)


if __name__ == "__main__":
    main()
