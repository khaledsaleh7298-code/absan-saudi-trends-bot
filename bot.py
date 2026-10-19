"""
Absan Client Sources Bot
------------------------
يراقب حسابات X وصفحات لينكدإن اللي حددها كل عميل (ملف sources.json)،
ويبعت كل منشور جديد لجروب تيليغرام أول ما ينزل، مع اسم العميل والرابط.

الأوامر داخل الجروب:
  /latest  آخر الأخبار المحفوظة لكل عميل
  /sources الحسابات والصفحات اللي البوت بيراقبها

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
FETCH_EVERY_MINUTES = 60          # كل قديش نفحص المصادر
RETRY_AFTER_FAIL_MINUTES = 20     # إذا فشل الفحص، متى نعيد المحاولة
MAX_ALERTS_PER_RUN = 20           # سقف التنبيهات بكل فحص حتى ما ينغرق الجروب
MAX_AGE_HOURS = 12                # ما نبعت خبر أقدم من هيك (خبر بارد)
OVERLAP_MINUTES = 15              # نرجع لورا شوي بكل فحص حتى ما يفوتنا شي
LATEST_PER_CLIENT = 8             # كم خبر نعرض لكل عميل بأمر /latest
TEXT_LIMIT = 700                  # أقصى طول لنص الخبر بالرسالة

# --- X (تويتر) ---
X_ACTOR = "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest"
X_MAX_ITEMS_PER_QUERY = 40        # أقصى عدد تغريدات بكل طلب
X_HANDLES_PER_QUERY = 8           # كم حساب نجمع بطلب واحد
X_MAX_CHARGE_USD = 0.05           # سقف أمان لتكلفة كل طلب
X_INCLUDE_REPLIES = False         # الردود على الناس ما بتهمنا
X_INCLUDE_RETWEETS = False        # إعادة النشر ما بتهمنا

# --- لينكدإن ---
LI_ACTOR = "harvestapi~linkedin-company-posts"
LI_MAX_POSTS_PER_PAGE = 10        # أقصى عدد منشورات لكل صفحة بكل فحص
LI_MAX_CHARGE_USD = 0.05          # سقف أمان لتكلفة كل طلب (= 25 منشور تقريباً)
LI_INCLUDE_REPOSTS = False        # إعادة النشر بدون تعليق ما بتهمنا

STATE_FILE = "state.json"
SOURCES_FILE = "sources.json"
STATE_VERSION = 3                 # النسخة 3 = X + لينكدإن
SAUDI_TZ = ZoneInfo("Asia/Riyadh")
# =======================================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
PLATFORMS = {"x": "X", "li": "لينكدإن"}


# ----------------------------- أدوات عامة -----------------------------
def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_any_time(value):
    """يقبل صيغة X (Thu Oct 17 09:30:41 +0000 2024) أو ISO أو timestamp"""
    if value in (None, ""):
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


def li_slug(url):
    """يرجع اسم الصفحة من رابط لينكدإن: .../company/awptco/ → awptco"""
    parts = urllib.parse.unquote(urllib.parse.urlparse(url).path).strip("/").split("/")
    return parts[1] if len(parts) >= 2 and parts[0] in ("company", "showcase", "school") else ""


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
                print(f"تجاهلت حساب X غير صالح عند {name}: {h!r}")
        pages = []
        for u in c.get("linkedin_pages") or []:
            u = str(u).strip()
            slug = li_slug(u)
            if slug:
                pages.append({"url": "https://www.linkedin.com/company/"
                                     + urllib.parse.quote(slug) + "/", "slug": slug})
            else:
                print(f"تجاهلت صفحة لينكدإن غير صالحة عند {name}: {u!r}")
        if name and (handles or pages):
            clients.append({"name": name, "emoji": c.get("emoji") or "📰",
                            "handles": handles, "pages": pages})
    if not clients:
        sys.exit(f"ملف {SOURCES_FILE} ما فيه ولا عميل عنده مصادر صالحة.")
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


def send_message(text, reply_to=None):
    params = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
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


def format_alert(item, client):
    source = html.escape(item["author_name"] or item["label"])
    if item["platform"] == "x":
        where = f"🏛 <b>{source}</b> (@{html.escape(item['label'])})"
        link_text = "فتح التغريدة على X"
    else:
        where = f"🏛 <b>{source}</b> · لينكدإن"
        link_text = "فتح المنشور على لينكدإن"
    lines = [
        f"{client['emoji']} <b>خبر جديد · {html.escape(client['name'])}</b>",
        "",
        where,
        "",
        html.escape(shorten(item["text"])),
        "",
        f"⏰ نُشر: {saudi_time(parse_time(item['created_at']), with_date=True)}",
        f'🔗 <a href="{html.escape(item["url"], quote=True)}">{link_text}</a>',
    ]
    return "\n".join(lines)


def short_label(item):
    if item["platform"] == "x":
        return "@" + item["label"]
    return "in · " + shorten(item["author_name"] or item["label"], 25)


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
            lines.append(f'{i}. <a href="{link}">{html.escape(short_label(t))}</a>: {snippet}')
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def format_sources(clients):
    lines = ["📡 <b>المصادر اللي البوت بيراقبها</b>",
             f"يفحص كل {FETCH_EVERY_MINUTES} دقيقة تقريباً."]
    for c in clients:
        lines.append(f"\n{c['emoji']} <b>{html.escape(c['name'])}</b>")
        if c["handles"]:
            lines.append("X:")
            for h in c["handles"]:
                lines.append(f'• <a href="https://x.com/{h}">@{h}</a>')
        if c["pages"]:
            lines.append("لينكدإن:")
            for p in c["pages"]:
                lines.append(f'• <a href="{html.escape(p["url"], quote=True)}">'
                             f'{html.escape(p["slug"])}</a>')
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
def run_actor(actor, payload, max_charge):
    data = json.dumps(payload).encode()
    base = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?timeout=120"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {APIFY_TOKEN}"}

    def call(url):
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=200) as resp:
            return json.load(resp)

    try:
        return call(f"{base}&maxTotalChargeUsd={max_charge}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if "max-total-charge" in body or "maxTotalChargeUsd" in body:
            return call(base)  # إذا رفض السقف، نجرب بدونه
        raise RuntimeError(f"Apify HTTP {e.code}: {body[:300]}")


# --- X ---
def x_query(handles, since):
    query = "(" + " OR ".join(f"from:{h}" for h in handles) + ")"
    if not X_INCLUDE_REPLIES:
        query += " -filter:replies"
    return query + f" since_time:{int(since.timestamp())}"


def x_normalize(item, wanted):
    """يحوّل نتيجة Apify لشكل موحّد، ويرمي البيانات الوهمية والردود"""
    if not isinstance(item, dict):
        return None
    tid = str(item.get("id") or item.get("id_str") or "")
    if not tid.isdigit() or "mock" in str(item.get("type", "")).lower():
        return None  # بيانات وهمية بترجع لما ما في نتائج
    author = item.get("author") or item.get("user") or {}
    handle = wanted.get((author.get("userName") or author.get("screen_name") or "")
                        .lstrip("@").lower())
    if not handle:
        return None
    if not X_INCLUDE_REPLIES and (item.get("isReply") or item.get("inReplyToId")):
        return None
    if not X_INCLUDE_RETWEETS and (item.get("isRetweet") or item.get("retweeted_tweet")):
        return None
    created = parse_any_time(item.get("createdAt") or item.get("created_at"))
    if not created:
        return None
    return {
        "key": "x:" + tid,
        "platform": "x",
        "label": handle,
        "author_name": (author.get("name") or "").strip(),
        "text": (item.get("text") or item.get("fullText") or item.get("full_text") or "").strip(),
        "created_at": created.isoformat(),
        "url": f"https://x.com/{handle}/status/{tid}",
    }


def fetch_x(client, since):
    wanted = {h.lower(): h for h in client["handles"]}
    found = {}
    for i in range(0, len(client["handles"]), X_HANDLES_PER_QUERY):
        chunk = client["handles"][i:i + X_HANDLES_PER_QUERY]
        payload = {"twitterContent": x_query(chunk, since), "queryType": "Latest",
                   "maxItems": X_MAX_ITEMS_PER_QUERY}
        for item in run_actor(X_ACTOR, payload, X_MAX_CHARGE_USD) or []:
            t = x_normalize(item, wanted)
            if t:
                found[t["key"]] = t
    return list(found.values())


# --- لينكدإن ---
def li_normalize(item):
    if not isinstance(item, dict):
        return None
    pid = str(item.get("id") or "")
    url = item.get("linkedinUrl") or ""
    text = (item.get("content") or "").strip()
    if not pid or not url.startswith("https://www.linkedin.com/") or not text:
        return None
    if not LI_INCLUDE_REPOSTS and (item.get("repostedBy") or item.get("type") == "repost"):
        return None
    posted = item.get("postedAt") or {}
    created = parse_any_time(posted.get("timestamp") or posted.get("date")) \
        if isinstance(posted, dict) else parse_any_time(posted)
    if not created:
        return None
    author = item.get("author") or {}
    return {
        "key": "li:" + pid,
        "platform": "li",
        "label": author.get("universalName") or "",
        "author_name": (author.get("name") or "").strip(),
        "text": text,
        "created_at": created.isoformat(),
        "url": url,
    }


def fetch_li(client, since):
    payload = {
        "targetUrls": [p["url"] for p in client["pages"]],
        "maxPosts": LI_MAX_POSTS_PER_PAGE,
        # بس المنشورات بعد آخر فحص (لازم نص، مش رقم)
        "postedLimitDate": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "includeQuotePosts": True,
        "includeReposts": LI_INCLUDE_REPOSTS,
        "scrapeReactions": False,   # التفاعلات بتنحسب كل وحدة كأنها منشور
        "scrapeComments": False,    # نفس الشي للتعليقات
    }
    found = {}
    for item in run_actor(LI_ACTOR, payload, LI_MAX_CHARGE_USD) or []:
        p = li_normalize(item)
        if p:
            found[p["key"]] = p
    return list(found.values())


def jobs_for(clients):
    """كل (عميل × منصة) هو "مهمة" لحالها بوقت آخر فحص خاص فيها"""
    for c in clients:
        if c["handles"]:
            yield c, "x", fetch_x
        if c["pages"]:
            yield c, "li", fetch_li


# ----------------------------- التشغيل -----------------------------
def migrate(state):
    """يرجع (state, رسالة التحديث أو None)"""
    version = state.get("version")
    if version == STATE_VERSION:
        return state, None
    if version == 2:  # كان يراقب X بس: نحتفظ بذاكرته ونضيف لينكدإن
        state["version"] = STATE_VERSION
        state.setdefault("baselined", [])
        state.setdefault("last_ok", {})
        # نحوّل الذاكرة القديمة للشكل الجديد
        state["seen"] = {(k if ":" in k else "x:" + k): v
                         for k, v in state.get("seen", {}).items()}
        for name, items in state.get("recent", {}).items():
            state["recent"][name] = [
                {"key": "x:" + t["id"], "platform": "x", "label": t["handle"],
                 "author_name": t.get("author_name", ""), "text": t.get("text", ""),
                 "created_at": t["created_at"], "url": t["url"]}
                if "platform" not in t else t
                for t in items if "platform" in t or ("id" in t and "handle" in t)]
        return state, "✅ <b>تحديث بوت عبسان: صار يراقب صفحات لينكدإن كمان</b>\n\n"
    # نسخة قديمة جداً (الترندات العامة): نبلّش ذاكرة جديدة ونحتفظ بس بمكان قراءة الأوامر
    return ({"version": STATE_VERSION, "update_offset": state.get("update_offset", 0),
             "baselined": [], "last_ok": {}},
            "✅ <b>تحديث بوت عبسان: صار يراقب مصادر العملاء</b>\n"
            "بدل الترندات العامة، رح أبعتلكم كل منشور جديد من الجهات اللي حددها كل عميل، "
            "أول ما ينزل.\n\n")


def main():
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
                              ("TELEGRAM_CHAT_ID", CHAT_ID),
                              ("APIFY_TOKEN", APIFY_TOKEN)] if not v]
    if missing:
        sys.exit("Missing secrets: " + ", ".join(missing))

    clients = load_sources()
    old_version = load_json(STATE_FILE, {}).get("version")
    state, announcement = migrate(load_json(STATE_FILE, {}))
    state.setdefault("baselined", [])
    state.setdefault("last_ok", {})
    fresh_start = old_version not in (2, STATE_VERSION)  # ذاكرة جديدة كلياً
    now = now_utc()

    # النسخة 2 كانت مخلصة الفحص الأول لـ X، فما بدنا نعيده
    if old_version == 2:
        for c in clients:
            key = f"{c['name']}|x"
            if c["handles"] and key not in state["baselined"]:
                state["baselined"].append(key)
                if state.get("last_fetch"):
                    state["last_ok"].setdefault(key, state["last_fetch"])

    next_fetch = parse_time(state.get("next_fetch"))
    if announcement or next_fetch is None or now >= next_fetch:
        seen = {k: v for k, v in state.get("seen", {}).items()
                if (parse_time(v) or now) > now - timedelta(days=3)}
        recent = state.get("recent", {})
        oldest_allowed = now - timedelta(hours=MAX_AGE_HOURS)

        jobs = list(jobs_for(clients))
        failed, alerts = [], []
        for c, platform, fetch in jobs:
            key = f"{c['name']}|{platform}"
            last_ok = parse_time(state["last_ok"].get(key))
            # بإعادة المحاولة منعيد بس اللي فشل، مش اللي نجح من شوي
            if last_ok and now - last_ok < timedelta(minutes=FETCH_EVERY_MINUTES - 5):
                continue
            since = max(last_ok - timedelta(minutes=OVERLAP_MINUTES), oldest_allowed) \
                if last_ok else oldest_allowed
            try:
                items = fetch(c, since)
            except Exception as e:
                print(f"{c['name']} / {PLATFORMS[platform]} failed: {e}")
                failed.append(key)
                continue
            fresh = [t for t in items if t["key"] not in seen
                     and parse_time(t["created_at"]) >= oldest_allowed]
            first_time = key not in state["baselined"]
            print(f"{c['name']} / {PLATFORMS[platform]}: {len(items)} items, {len(fresh)} new"
                  + (" (first check: saved silently)" if first_time else "") + ".")
            for t in fresh:
                seen[t["key"]] = now.isoformat()
                if not first_time:
                    alerts.append((t, c))
            if first_time:
                state["baselined"].append(key)
            state["last_ok"][key] = now.isoformat()
            keep = {t["key"]: t for t in fresh + recent.get(c["name"], [])}
            recent[c["name"]] = sorted(keep.values(), key=lambda t: t["created_at"],
                                       reverse=True)[:LATEST_PER_CLIENT]

        if announcement:
            send_message(announcement + "• /latest آخر الأخبار المحفوظة لكل عميل\n"
                                        "• /sources المصادر اللي براقبها\n\n"
                         + format_sources(clients))
        alerts.sort(key=lambda a: a[0]["created_at"])
        sent = 0
        for t, c in alerts[-MAX_ALERTS_PER_RUN:]:
            if send_message(format_alert(t, c)):
                sent += 1
            time.sleep(1)
        print(f"Sent {sent} alerts ({len(alerts)} new).")

        state.update({"seen": seen, "recent": recent})
        # عدّاد فشل لكل مهمة لحالها، وتنبيه وحيد إذا فشلت 3 مرات ورا بعض
        streaks = state.setdefault("fail_streaks", {})
        state.pop("fail_streak", None)
        for c, platform, _ in jobs:
            key = f"{c['name']}|{platform}"
            if key in failed:
                streaks[key] = streaks.get(key, 0) + 1
                if streaks[key] == 3:
                    send_message(f"⚠️ <b>البوت ما قدر يجيب أخبار {PLATFORMS[platform]} "
                                 f"لـ {html.escape(c['name'])} من 3 محاولات متتالية.</b>\n"
                                 "افحصوا تبويب Actions على GitHub ورصيد Apify.")
            elif state["last_ok"].get(key) == now.isoformat():
                streaks.pop(key, None)
        if len(failed) < len(jobs):
            state["last_fetch"] = now.isoformat()
        # الفحص الجاي: أول مهمة بيصير دورها، أو بعد 20 دقيقة إذا في شي فشل
        due = [parse_time(state["last_ok"][f"{c['name']}|{p}"]) + timedelta(minutes=FETCH_EVERY_MINUTES)
               for c, p, _ in jobs if state["last_ok"].get(f"{c['name']}|{p}")]
        if failed:
            due.append(now + timedelta(minutes=RETRY_AFTER_FAIL_MINUTES))
        state["next_fetch"] = (min(due) if due else now + timedelta(
            minutes=RETRY_AFTER_FAIL_MINUTES)).isoformat()

        if announcement:
            telegram("setMyCommands", commands=[
                {"command": "latest", "description": "آخر أخبار مصادر العملاء"},
                {"command": "sources", "description": "المصادر اللي البوت بيراقبها"},
            ])

    # بذاكرة جديدة كلياً منتجاهل الأوامر القديمة
    handle_commands(state, clients, reply_enabled=not fresh_start)
    save_state(state)


if __name__ == "__main__":
    main()
