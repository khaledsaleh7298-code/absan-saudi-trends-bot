"""
Absan Trends Bot
----------------
كل ساعة: بيجيب ترندات X بالسعودية، وبيحكم على كل ترند جديد:
هل هو فرصة حقيقية لواحد من عملاء عبسان، أو ترند وطني مهم؟
- إذا آه: بيبعت رسالة بصوت 🔔، فيها لمين وليش، وخبر سبق عنه، وأبرز التغريدات.
- إذا لأ: بيبعت رسالة صامتة 🔕 قصيرة، بس حتى تعرفوا إنه شغّال.

«العقل» اللي بيحكم إله نسختين:
  1) كلمات (sources.json): شغّالة دايماً.
  2) ذكاء اصطناعي (Claude): بيشتغل لحاله لما ينضاف ANTHROPIC_API_KEY
     بـ GitHub Secrets. وإذا تعطّل، البوت بيرجع للكلمات.

الأوامر داخل الجروب:
  /latest  آخر الترندات المهمة اللي انبعتت
  /sources شو البوت بيراقب وكيف بيحكم

يشتغل على GitHub Actions. الأسرار تُقرأ من Secrets ولا تُكتب في الكود أبداً.
"""

import hashlib
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
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

# ======================= الإعدادات (يمكن تعديلها) =======================
TRENDS_EVERY_MINUTES = 60         # كل قديش نفحص الترندات
RETRY_AFTER_FAIL_MINUTES = 20     # إذا فشل الفحص، متى نعيد المحاولة
MAX_ALERTS_PER_CHECK = 5          # سقف الترندات المهمة بكل فحص
SILENT_CHECK_MESSAGE = True       # رسالة صامتة لما ما يلاقي شي (حتى تعرفوا إنه شغّال)
JUDGED_MEMORY_DAYS = 3            # قديش بيتذكر الترندات اللي حكم عليها
LATEST_COUNT = 8                  # كم ترند بيعرض أمر /latest
TEXT_LIMIT = 220                  # أقصى طول لنص التغريدة بالرسالة

# --- ترندات X (Apify) ---
TRENDS_ACTOR = "labrat011~x-trends-scraper"
TRENDS_MAX_CHARGE_USD = 0.02      # سقف أمان لتكلفة كل فحص

# --- أبرز التغريدات عن الترند المهم (Apify، أداة Kaito) ---
# الأداة بتحاسب 15 نتيجة كحد أدنى بكل طلب (حوالي ثلث سنت)، فمنطلبها بس للترند المهم.
TWEETS_ACTOR = "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest"
TWEETS_PER_TREND = 3              # كم تغريدة نعرض
MAX_TWEET_LOOKUPS_PER_CHECK = 3   # سقف طلبات التغريدات بكل فحص
TWEETS_MAX_CHARGE_USD = 0.02

# --- مواقع الأخبار (RSS، مجانية): بتعطي «سياق» الترند، يعني ليش طالع ---
NEWS_HOURS = 6                    # منستعمل أخبار آخر كم ساعة
NEWS_PER_TREND = 2                # كم خبر منعرض مع كل ترند
RSS_USER_AGENT = "Mozilla/5.0 (compatible; AbsanNewsBot/1.0)"

# --- الذكاء الاصطناعي (مطفي لحد ما ينضاف ANTHROPIC_API_KEY) ---
AI_MODEL = "claude-haiku-5-5"
AI_MAX_TOKENS = 4000

STATE_FILE = "state.json"
SOURCES_FILE = "sources.json"
STATE_VERSION = 4                 # النسخة 4 = ترندات السعودية
SAUDI_TZ = ZoneInfo("Asia/Riyadh")
NATIONAL = "وطني"
# =======================================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
AI_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()


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
    """يقبل صيغة X (Thu Oct 17 09:30:41 +0000 2024) أو ISO أو صيغة RSS"""
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


def hours_text(hours):
    """1 → من ساعة، 2 → من ساعتين، 5 → من 5 ساعات"""
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        return ""
    if hours <= 1:
        return "طالع من أقل من ساعة" if hours < 1 else "ترند من ساعة"
    if hours == 2:
        return "ترند من ساعتين"
    return f"ترند من {hours} ساعات" if hours <= 10 else f"ترند من {hours} ساعة"


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


def shorten(text, limit=TEXT_LIMIT):
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def compact_number(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return str(n)


# ----------------------------- الكلمات -----------------------------
AR_MARKS = re.compile("[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")  # التشكيل والتطويل


def normalize(text):
    """يوحّد شكل النص العربي حتى تنلقط الكلمة بكل أشكالها:
    بيشيل التشكيل، وبيوحّد أ/إ/آ → ا، و ة → ه، و ى → ي، والهاشتاق بيصير كلام عادي"""
    text = AR_MARKS.sub("", text or "")
    text = re.sub("[إأآٱ]", "ا", text).replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", re.sub(r"[#_]", " ", text)).strip().lower()


def word_pattern(word):
    """يحوّل كلمة لنمط بحث بيلقطها بأشكالها:
    «مياه» بتلقط: مياه، المياه، والمياه، بالمياه، للمياه
    «الري» بتلقط: الري، للري... بس مش «الرياض» (لازم الكلمة كاملة)
    الكلمات الإنجليزية: كلمة كاملة بس، فـ AI ما بتلقط Dubai"""
    w = normalize(word)
    if not w:
        return None
    if re.fullmatch(r"[a-z0-9 .&+-]+", w):
        body = r"\s+".join(map(re.escape, w.split()))
        return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")
    parts = w.split()
    if parts[0].startswith("ال") and len(parts[0]) > 3:
        parts[0] = parts[0][2:]
        prefix = r"[وف]?(?:[بك]?ال|لل)"          # الكلمة لازم تكون معرّفة
    else:
        prefix = r"[وف]?(?:[بك]?ال|لل|ل)?"       # معرّفة أو لأ
    body = r"\s+".join(map(re.escape, parts))
    return re.compile(rf"(?<!\w){prefix}{body}(?!\w)")


def compile_words(words):
    """يرجع [(الكلمة، النمط)] حتى نعرف أي كلمة انلقطت"""
    out = []
    for w in words or []:
        p = word_pattern(str(w))
        if p:
            out.append((str(w), p))
    return out


def first_match(compiled, text):
    for word, p in compiled:
        if p.search(text):
            return word
    return None


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
        if not name:
            continue
        clients.append({
            "name": name,
            "emoji": c.get("emoji") or "📰",
            "about": str(c.get("about") or "").strip(),
            "words": compile_words(c.get("news_keywords")),
            "raw_words": [str(w) for w in c.get("news_keywords") or []],
        })
    if not clients:
        sys.exit(f"ملف {SOURCES_FILE} ما فيه ولا عميل.")

    feeds = []
    for f in data.get("news_feeds") or []:
        fname = str((f or {}).get("name") or "").strip()
        furl = str((f or {}).get("url") or "").strip()
        if fname and furl.startswith("http"):
            feeds.append({"name": fname, "url": furl})
        else:
            print(f"تجاهلت موقع أخبار غير صالح: {f!r}")

    settings = data.get("trend_settings") or {}
    try:
        per_check = max(5, min(50, int(settings.get("trends_per_check") or 15)))
    except (TypeError, ValueError):
        per_check = 15
    # بصمة القوائم: إذا عدّلت كلمة، البوت بيعيد الحكم على الترندات اللي لسا ما انبعتت
    rules = json.dumps([data.get(k) for k in ("national_keywords", "ignore_trend_words", "blocked_words")]
                       + [[c.get("news_keywords"), c.get("about")] for c in data["clients"]],
                       ensure_ascii=False, sort_keys=True)
    return {
        "rules": hashlib.sha1(rules.encode("utf-8")).hexdigest()[:12],
        "clients": clients,
        "feeds": feeds,
        "country": str(settings.get("country") or "SA").strip(),
        "per_check": per_check,
        "national": compile_words(data.get("national_keywords")),
        "raw_national": [str(w) for w in data.get("national_keywords") or []],
        "ignore": compile_words(data.get("ignore_trend_words")),
        "blocked": compile_words(data.get("blocked_words")),
    }


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


def send_message(text, reply_to=None, silent=False):
    params = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
        "disable_notification": silent,
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


def fetch_trends(cfg):
    """ترندات X الحالية بالسعودية، مرتبة حسب المركز"""
    payload = {"locations": [cfg["country"]], "maxTrendsPerLocation": cfg["per_check"],
               "includeHistory": False}
    trends = {}
    for item in run_actor(TRENDS_ACTOR, payload, TRENDS_MAX_CHARGE_USD) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        key = normalize(name)
        if not key or key in trends:
            continue
        try:
            rank = int(item.get("rank") or 99)
        except (TypeError, ValueError):
            rank = 99
        trends[key] = {
            "key": key,
            "name": name,
            "rank": rank,
            "hours": item.get("hoursTrending24h"),
            "best_rank": item.get("bestRank24h"),
            "is_new": bool(item.get("isNew")),
            "url": item.get("searchUrl") or
                   "https://x.com/search?q=" + urllib.parse.quote(name),
        }
    return sorted(trends.values(), key=lambda t: t["rank"])


def fetch_top_tweets(trend, blocked):
    """أبرز التغريدات عن الترند (طلب واحد، بس للترند المهم)"""
    query = trend["name"] if trend["name"].startswith("#") else f'"{trend["name"]}"'
    since = int((now_utc() - timedelta(hours=24)).timestamp())
    payload = {"twitterContent": f"{query} -filter:replies since_time:{since}",
               "queryType": "Top", "maxItems": 20}
    tweets = []
    for item in run_actor(TWEETS_ACTOR, payload, TWEETS_MAX_CHARGE_USD) or []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "")
        if not tid.isdigit() or "mock" in str(item.get("type", "")).lower():
            continue  # بيانات وهمية بترجع لما ما في نتائج
        if item.get("isRetweet") or item.get("retweeted_tweet"):
            continue
        author = item.get("author") or {}
        user = (author.get("userName") or "").lstrip("@")
        text = (item.get("text") or item.get("fullText") or "").strip()
        if not user or not text:
            continue
        if first_match(blocked, normalize(text)):
            continue  # ما منعرض تغريدة فيها كلمة ممنوعة
        tweets.append({
            "user": user,
            "text": text,
            "likes": item.get("likeCount") or 0,
            "url": item.get("url") or f"https://x.com/{user}/status/{tid}",
        })
    tweets.sort(key=lambda t: int(t["likes"] or 0), reverse=True)
    return tweets[:TWEETS_PER_TREND]


# ----------------------------- مواقع الأخبار -----------------------------
def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def rss_items_from_xml(raw):
    """يقرأ عناصر الخبر من ملف RSS. إذا الملف مقطوع، بيقرأ الأخبار الكاملة بس"""
    try:
        return list(ElementTree.fromstring(raw).iter("item"))
    except ElementTree.ParseError:
        items = []
        for block in re.findall(rb"<item\b.*?</item>", raw, flags=re.S):
            try:
                items.append(ElementTree.fromstring(block))
            except ElementTree.ParseError:
                continue
        return items


def fetch_news(feeds):
    """عناوين آخر كم ساعة من مواقع الأخبار. إذا موقع ما فتح، منكمّل بدونه"""
    oldest = now_utc() - timedelta(hours=NEWS_HOURS)
    news = []
    for feed in feeds:
        req = urllib.request.Request(feed["url"], headers={"User-Agent": RSS_USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except Exception as e:
            code = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__
            print(f"موقع {feed['name']} ما فتح ({code}): منكمّل بدون أخباره.")
            continue
        count = 0
        for el in rss_items_from_xml(raw):
            title = strip_html(el.findtext("title"))
            link = (el.findtext("link") or "").strip()
            created = parse_any_time((el.findtext("pubDate") or "").strip())
            if not title or not link.startswith("http") or not created or created < oldest:
                continue
            desc = strip_html(el.findtext("description"))
            news.append({"source": feed["name"], "title": title, "link": link,
                         "norm": normalize(title + " " + desc),
                         "created_at": created.astimezone(timezone.utc).isoformat()})
            count += 1
        print(f"موقع {feed['name']}: {count} خبر بآخر {NEWS_HOURS} ساعات.")
    return news


STOP_WORDS = {"في", "من", "الي", "علي", "عن", "مع", "هذا", "هذه", "التي", "الذي",
              "او", "ثم", "كل", "بعد", "قبل", "يوم", "اليوم"}


def related_news(trend, news):
    """الأخبار اللي بتحكي عن الترند: لازم يكون فيها معظم كلمات اسم الترند"""
    words = [w for w in normalize(trend["name"]).split()
             if len(w) >= 3 and w not in STOP_WORDS]
    if not words:
        return []
    cores = [w[2:] if w.startswith("ال") and len(w) > 4 else w for w in words]
    needed = len(cores) if len(cores) <= 2 else -(-len(cores) * 2 // 3)
    scored = []
    for n in news:
        hits = sum(1 for c in cores if c in n["norm"])
        if hits >= needed and (len(cores) > 1 or len(cores[0]) >= 4):
            scored.append((hits, n["created_at"], n))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [s[2] for s in scored[:NEWS_PER_TREND]]


# ----------------------------- العقل: الحكم على الترند -----------------------------
def prefilter(trend, cfg):
    """قبل أي حكم: الكلمات الممنوعة والرياضة والترفيه والإعلانات بتطلع فوراً"""
    text = trend["context_norm"]
    if first_match(cfg["blocked"], text):
        return "كلمة ممنوعة"
    word = first_match(cfg["ignore"], normalize(trend["name"]))
    if word:
        return f"ترند مستبعد («{word}»)"
    return None


def keyword_judge(candidates, cfg):
    """العقل المؤقت: بيدوّر على كلمات العملاء والكلمات الوطنية
    باسم الترند وبعناوين الأخبار اللي بتحكي عنه"""
    decisions = {}
    for t in candidates:
        name_norm = normalize(t["name"])
        whom, why = [], []
        for c in cfg["clients"]:
            word = first_match(c["words"], name_norm)
            where = "باسم الترند"
            if not word:
                word = first_match(c["words"], t["context_norm"])
                where = "بخبر عنه"
            if word:
                whom.append(c["name"])
                why.append(f"«{word}» {where} ← {c['name']}")
        word = first_match(cfg["national"], name_norm)
        where = "باسم الترند"
        if not word:
            word = first_match(cfg["national"], t["context_norm"])
            where = "بخبر عنه"
        if word:
            whom.append(NATIONAL)
            why.append(f"«{word}» {where} ← ترند وطني")
        if whom:
            decisions[t["key"]] = {"for": whom, "why": "لقيت " + "، و".join(why)}
    return decisions


AI_SYSTEM = """انت محرر محتوى بوكالة «عبسان»، وكالة بتبني الحضور المهني لقيادات سعودية على لينكدإن وX.
مهمتك: تقرر لكل ترند من ترندات X بالسعودية إذا هو فرصة حقيقية إنه واحد من عملاء الوكالة يكتب عنه بوست مهني الآن، أو إذا هو ترند وطني سعودي مهم.

عملاء الوكالة:
{clients}

«ترند وطني» يعني: حدث وطني كبير بيهم السعودية كلها، متل قرار حكومي مهم، إنجاز للمملكة، رؤية 2030 ومشاريعها، القيادة وولي العهد، أمن الوطن والدفاع عنه، مناسبة وطنية. عملاء الوكالة بيحبوا يشاركوا الفخر الوطني وقوة الوطن.

القواعد:
- اختار بس الترندات المهمة فعلاً. الأغلبية بتكون مش مهمة، وهاد طبيعي.
- مش مهم: الرياضة والأندية، الترفيه والمسلسلات والحفلات، الإعلانات والعروض والمسابقات التجارية، الهاشتاقات الاجتماعية العامة (متل «انشر تغريدة عن…»)، الأخبار الشخصية والفضائح.
- مش مهم أبداً: أي شي غير لائق أو فيه إيحاء، أو جدل ديني أو أخلاقي.
- إذا ما في خبر بيوضّح سبب الترند واسمه مش واضح، احكم بحذر، ولا تختاره إلا إذا اسمه بيدل بوضوح على حدث وطني أو على مجال عميل.
- "for": أسماء العملاء بالضبط زي ما هي مكتوبة فوق، و/أو "{national}". فاضية إذا مش مهم.
- "why": إذا مهم، جملة وحدة قصيرة بالعربي: ليش بيهم، ومن أي زاوية ممكن العميل يكتب. إذا مش مهم، اكتب السبب بكلمتين.

رد بـ JSON بس، بدون أي كلام قبله أو بعده، بهالشكل:
[{{"id": 1, "relevant": true, "for": ["..."], "why": "..."}}]
لازم يكون في عنصر لكل ترند."""


def ai_judge(candidates, cfg):
    """العقل الذكي: نموذج Claude بيقرأ الترندات والأخبار عنها وبيحكم.
    بيرمي خطأ إذا ما زبط، والبوت بيرجع للكلمات"""
    clients_text = "\n".join(f"- {c['name']}: {c['about'] or '، '.join(c['raw_words'])}"
                             for c in cfg["clients"])
    system = AI_SYSTEM.format(clients=clients_text, national=NATIONAL)
    lines = []
    for i, t in enumerate(candidates, 1):
        info = [f"المركز {t['rank']}"]
        if hours_text(t.get("hours")):
            info.append(hours_text(t["hours"]))
        if t["is_new"]:
            info.append("جديد")
        lines.append(f"{i}. {t['name']} ({'، '.join(info)})")
        for n in t["news"]:
            lines.append(f"   خبر من {n['source']}: {n['title']}")
        if not t["news"]:
            lines.append("   ما لقيت خبر عنه")
    body = json.dumps({
        "model": AI_MODEL,
        "max_tokens": AI_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": "ترندات السعودية الجديدة:\n" + "\n".join(lines)}],
    }).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": AI_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    text = "".join(b.get("text", "") for b in reply.get("content") or [] if b.get("type") == "text")
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise RuntimeError(f"رد Claude مش JSON: {text[:200]!r}")
    items = json.loads(text[start:end + 1])
    allowed = {c["name"] for c in cfg["clients"]} | {NATIONAL}
    decisions = {}
    for item in items:
        try:
            t = candidates[int(item.get("id")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        whom = [w for w in item.get("for") or [] if w in allowed]
        if item.get("relevant") and whom:
            decisions[t["key"]] = {"for": whom, "why": str(item.get("why") or "").strip()}
        else:
            print(f"  ⛔ {t['name']}: {shorten(str(item.get('why') or 'مش مهم'), 80)}")
    usage = reply.get("usage") or {}
    print(f"Claude: {usage.get('input_tokens', '?')} داخل، {usage.get('output_tokens', '?')} طالع.")
    return decisions


def judge(candidates, cfg):
    """يرجع (القرارات، اسم العقل اللي حكم)"""
    if not candidates:
        return {}, "ai" if AI_KEY else "kw"
    if AI_KEY:
        try:
            return ai_judge(candidates, cfg), "ai"
        except Exception as e:
            print(f"الذكاء الاصطناعي ما زبط ({e}): رجعت للكلمات.")
            return keyword_judge(candidates, cfg), "kw-fallback"
    return keyword_judge(candidates, cfg), "kw"


BRAIN_NAMES = {"ai": "ذكاء اصطناعي", "kw": "كلمات",
               "kw-fallback": "كلمات (الذكاء الاصطناعي ما رد)"}


# ----------------------------- الرسائل -----------------------------
def whom_text(whom, cfg):
    emojis = {c["name"]: c["emoji"] for c in cfg["clients"]}
    parts = []
    for w in whom:
        parts.append("🇸🇦 ترند وطني" if w == NATIONAL else f"{emojis.get(w, '📰')} {w}")
    return " · ".join(html.escape(p) for p in parts)


def format_alert(trend, decision, tweets, cfg):
    stats = [f"المركز {trend['rank']} بالسعودية"]
    if hours_text(trend.get("hours")):
        stats.append(hours_text(trend["hours"]))
    if trend["is_new"]:
        stats.append("🆕 جديد")
    lines = [
        "🔥 <b>ترند مهم لعملاء عبسان</b>",
        whom_text(decision["for"], cfg),
        "",
        f"<b>{html.escape(trend['name'])}</b>",
        "📊 " + " · ".join(stats),
    ]
    if decision.get("why"):
        lines += ["", f"💡 <b>ليش:</b> {html.escape(decision['why'])}"]
    if trend["news"]:
        lines += ["", "📰 <b>شو صاير:</b>"]
        for n in trend["news"]:
            lines.append(f'• <a href="{html.escape(n["link"], quote=True)}">'
                         f'{html.escape(n["title"])}</a> ({html.escape(n["source"])})')
    if tweets:
        lines += ["", "🗣 <b>أبرز التغريدات:</b>"]
        for i, tw in enumerate(tweets, 1):
            likes = compact_number(tw["likes"])
            lines.append(f'{i}. <a href="{html.escape(tw["url"], quote=True)}">'
                         f'@{html.escape(tw["user"])}</a>: {html.escape(shorten(tw["text"]))}'
                         + (f" (❤️ {likes})" if likes and likes != "0" else ""))
    lines += ["", f'🔍 <a href="{html.escape(trend["url"], quote=True)}">'
                  f'شوف كل التغريدات عن الترند</a>']
    return "\n".join(lines)


def format_check_note(trends_count, brain):
    return (f"✅ فحص {saudi_time()} · شفت {trends_count} ترند · "
            f"ما في ترند جديد بيخص عملاء عبسان\n🧠 الحكم: {BRAIN_NAMES.get(brain, brain)}")


def format_latest(state, cfg):
    items = state.get("recent_trends", [])[:LATEST_COUNT]
    lines = ["🗂 <b>آخر الترندات المهمة</b>"]
    if state.get("last_check"):
        lines[0] += f"\n⏰ آخر فحص: {saudi_time(parse_time(state['last_check']))}"
    if not items:
        lines.append("\nلسا ما في ترندات مهمة.")
    for i, t in enumerate(items, 1):
        lines.append(f'\n{i}. <a href="{html.escape(t["url"], quote=True)}">'
                     f'{html.escape(t["name"])}</a>: {whom_text(t["for"], cfg)}'
                     f"\n⏰ {saudi_time(parse_time(t['sent_at']), with_date=True)}")
        if t.get("why"):
            lines.append("💡 " + html.escape(shorten(t["why"], 150)))
    return "\n".join(lines)


def format_sources(cfg):
    lines = ["📡 <b>شو البوت بيراقب</b>",
             f"كل ساعة بيشوف أعلى {cfg['per_check']} ترند على X بالسعودية، "
             "وبيبعت بس الترند اللي بيهم عملاء عبسان أو الترند الوطني المهم.",
             f"🧠 الحكم: {'ذكاء اصطناعي' if AI_KEY else 'كلمات'}"]
    if cfg["feeds"]:
        lines.append("📰 سياق الترند من: " + "، ".join(html.escape(f["name"]) for f in cfg["feeds"]))
    for c in cfg["clients"]:
        lines.append(f"\n{c['emoji']} <b>{html.escape(c['name'])}</b>")
        lines.append(html.escape("، ".join(c["raw_words"])) or "ما في كلمات")
    lines.append("\n🇸🇦 <b>ترند وطني</b>")
    lines.append(html.escape("، ".join(cfg["raw_national"])) or "ما في كلمات")
    return "\n".join(lines)


def handle_commands(state, cfg, reply_enabled):
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
            send_message(format_latest(state, cfg), reply_to=msg.get("message_id"))
        elif command == "/sources":
            send_message(format_sources(cfg), reply_to=msg.get("message_id"))


# ----------------------------- الفحص -----------------------------
def context_hash(trend):
    links = sorted(n["link"] for n in trend["news"])
    return hashlib.sha1("|".join(links).encode("utf-8")).hexdigest()[:12]


def check_trends(state, cfg, now):
    """فحص واحد: يجيب الترندات، يحكم على الجديد، ويبعت المهم"""
    trends = fetch_trends(cfg)
    news = fetch_news(cfg["feeds"])
    judged = state.setdefault("judged", {})
    brain_now = "ai" if AI_KEY else "kw"

    candidates = []
    for t in trends:
        t["news"] = related_news(t, news)
        t["context_norm"] = normalize(t["name"] + " " + " ".join(n["norm"] for n in t["news"]))
        old = judged.get(t["key"]) or {}
        if old.get("sent"):
            continue  # انبعت قبل: ما منعيده
        ctx = context_hash(t)
        # ما منعيد الحكم إلا إذا: انضاف خبر جديد عنه، أو تعدّلت القوائم،
        # أو تغيّر العقل (يوم ما يشتغل الذكاء الاصطناعي)
        if (old and old.get("ctx") == ctx and old.get("rules") == cfg["rules"]
                and old.get("brain") == brain_now):
            continue
        reason = prefilter(t, cfg)
        judged[t["key"]] = {"at": now.isoformat(), "sent": False, "ctx": ctx,
                            "rules": cfg["rules"], "brain": brain_now, "name": t["name"]}
        if reason:
            print(f"  ⛔ {t['name']}: {reason}")
            continue
        candidates.append(t)

    decisions, brain = judge(candidates, cfg)
    if brain == "kw-fallback":
        for t in candidates:  # الذكاء الاصطناعي ما رد: منخليه يحكم عليهم بالفحص الجاي
            judged[t["key"]]["brain"] = "kw"
    print(f"شفت {len(trends)} ترند، حكمت على {len(candidates)} جديد ({BRAIN_NAMES.get(brain, brain)})، "
          f"المهم: {len(decisions)}.")

    sent, lookups = 0, 0
    for t in candidates:
        d = decisions.get(t["key"])
        if not d:
            if brain != "ai":
                print(f"  ⛔ {t['name']}: ما إله علاقة بالعملاء")
            continue
        if sent >= MAX_ALERTS_PER_CHECK:
            print(f"  ⏸ {t['name']}: وصلنا سقف {MAX_ALERTS_PER_CHECK} ترندات بهالفحص، بيضل للفحص الجاي.")
            judged.pop(t["key"], None)
            continue
        tweets = []
        if lookups < MAX_TWEET_LOOKUPS_PER_CHECK:
            lookups += 1
            try:
                tweets = fetch_top_tweets(t, cfg["blocked"])
            except Exception as e:
                print(f"  ما قدرت أجيب تغريدات {t['name']}: {e}")
        if send_message(format_alert(t, d, tweets, cfg)):
            sent += 1
            judged[t["key"]]["sent"] = True
            print(f"  ✅ {t['name']}: {' + '.join(d['for'])} · {shorten(d['why'], 80)}")
            recent = state.setdefault("recent_trends", [])
            recent.insert(0, {"name": t["name"], "url": t["url"], "for": d["for"],
                              "why": d["why"], "sent_at": now.isoformat()})
            del recent[LATEST_COUNT:]
        else:
            judged.pop(t["key"], None)  # ما انبعت: منرجع نجرّبه بالفحص الجاي
        time.sleep(1)

    if sent == 0 and SILENT_CHECK_MESSAGE:
        send_message(format_check_note(len(trends), brain), silent=True)
    print(f"Sent {sent} trend alerts.")

    # ننسى الترندات القديمة حتى ما يكبر الملف
    oldest = now - timedelta(days=JUDGED_MEMORY_DAYS)
    for k in [k for k, v in judged.items() if (parse_time(v.get("at")) or now) < oldest]:
        judged.pop(k)


# ----------------------------- التشغيل -----------------------------
ANNOUNCEMENT = (
    "✅ <b>تحديث بوت عبسان: صار رادار ترندات</b>\n"
    "ما عاد يبعت كل منشور من الجهات. كل ساعة بيشوف ترندات X بالسعودية، "
    "وبيبعت بس الترند اللي بيهم عملاء عبسان أو الترند الوطني المهم 🔔.\n"
    "ولما ما يلاقي شي، بيبعت رسالة صامتة 🔕 حتى تعرفوا إنه شغّال.\n\n"
    "• /latest آخر الترندات المهمة\n"
    "• /sources شو البوت بيراقب"
)


def migrate(state):
    """يرجع (state, رسالة التحديث أو None)"""
    if state.get("version") == STATE_VERSION:
        return state, None
    # النسخ القديمة (مصادر العملاء): منبلّش ذاكرة جديدة ومنحتفظ بس بمكان قراءة الأوامر
    return ({"version": STATE_VERSION, "update_offset": state.get("update_offset", 0),
             "judged": {}, "recent_trends": [], "last_ok": None},
            ANNOUNCEMENT)


def main():
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
                              ("TELEGRAM_CHAT_ID", CHAT_ID),
                              ("APIFY_TOKEN", APIFY_TOKEN)] if not v]
    if missing:
        sys.exit("Missing secrets: " + ", ".join(missing))

    cfg = load_sources()
    old_state = load_json(STATE_FILE, {})
    fresh_start = not old_state  # ما في ذاكرة أبداً: منتجاهل الأوامر القديمة
    state, announcement = migrate(old_state)
    now = now_utc()
    print(f"🧠 العقل: {'ذكاء اصطناعي (' + AI_MODEL + ')' if AI_KEY else 'كلمات'}")

    if announcement:
        send_message(announcement)
        telegram("setMyCommands", commands=[
            {"command": "latest", "description": "آخر الترندات المهمة"},
            {"command": "sources", "description": "شو البوت بيراقب"},
        ])

    last_ok = parse_time(state.get("last_ok"))
    retry_at = parse_time(state.get("retry_at"))
    due = last_ok is None or now - last_ok >= timedelta(minutes=TRENDS_EVERY_MINUTES - 5)
    if due and (retry_at is None or now >= retry_at):
        try:
            check_trends(state, cfg, now)
            state["last_ok"] = state["last_check"] = now.isoformat()
            state.pop("retry_at", None)
            state["fail_streak"] = 0
        except Exception as e:
            print(f"فحص الترندات failed: {e}")
            state["retry_at"] = (now + timedelta(minutes=RETRY_AFTER_FAIL_MINUTES)).isoformat()
            state["fail_streak"] = state.get("fail_streak", 0) + 1
            if state["fail_streak"] == 3:
                send_message("⚠️ <b>البوت ما قدر يجيب ترندات السعودية من 3 محاولات متتالية.</b>\n"
                             "افحصوا تبويب Actions على GitHub، ورصيد Apify كمان.")
    else:
        print("لسا ما إجا وقت الفحص.")

    handle_commands(state, cfg, reply_enabled=not fresh_start)
    save_state(state)


if __name__ == "__main__":
    main()
