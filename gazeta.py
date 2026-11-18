"""Утренняя газета Ксени «Ранок»: главное, свет, LUNO, погода, курс, Украина, бизнес, видео.
Запускается каждое утро (будильник cron-job.org + запасное расписание GitHub)."""
import os, re, json, html, sys, datetime, traceback, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

# ---------------- настройки и секреты ----------------
# очистка: на случай, если в секрет случайно попали пробелы, кавычки или ```
TOKEN = re.sub(r"[^\w:-]", "", os.environ.get("TELEGRAM_TOKEN", ""))
CHAT = re.sub(r"[^\d-]", "", os.environ.get("TELEGRAM_CHAT_ID", "")) or "370560008"
KEY = os.environ.get("DASHBOARD_KEY", "").strip()
LUNO_URL = os.environ.get("LUNO_URL", "").strip().rstrip("/")
_m = re.search(r"project/([a-z0-9]{15,})", LUNO_URL) or (re.fullmatch(r"[a-z0-9]{15,}", LUNO_URL) and re.match(r"(.*)", LUNO_URL))
if _m:  # вставили ссылку на дашборд или только ID проекта — соберём адрес сами
    LUNO_URL = f"https://{_m.group(1)}.supabase.co"
LUNO_KEY = os.environ.get("LUNO_ANON_KEY", "").strip()
LUNO_SECRET = os.environ.get("LUNO_SECRET", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "user/gazeta")
EVENT = os.environ.get("GITHUB_EVENT_NAME", "manual")
SERVER = "https://home-dashboard.dzyn.workers.dev/api"
KYIV = ZoneInfo("Europe/Kyiv")

# Бизнес и финансы: сайты (RSS)
FEEDS = [
    # сайт MC.today не пускает серверы GitHub (Forbidden), поэтому берём его статьи через Google Новости
    ("MC.today", "https://mc.today", [
        "https://news.google.com/rss/search?q=site:mc.today+when:2d&hl=uk&gl=UA&ceid=UA:uk",
        "https://mc.today/feed/"]),
    ("КОШТ", "https://kosht.media", ["https://kosht.media/feed/"]),
]
# Бизнес и финансы: Telegram. (название, [возможные адреса], слово, которое должно быть в названии канала)
BUSINESS_TG = [
    ("Про Бізнес", ["business_ua", "probusiness_ua", "pro_business_ua"], "бізнес"),
    ("MPG: Бізнес", ["mpg_finance"], ""),
    ("Wallet Pro", ["walletproua"], ""),
]
# Украина: Telegram
UKRAINE_TG = [
    ("Insider UA", ["insiderukr"], "insider"),
    ("Ukraine Now", ["UkraineNow"], "ukraine"),
    ("КМВА", ["VA_Kyiv", "kyiv_mva", "KyivCityOfficial"], "київ|киев|кмва|кмда"),
    ("Бабель", ["babel"], "бабель"),
    ("Мінцифри", ["mintsyfra", "mintsyfra_news", "mintsyfra_ua", "mincyfry"], "цифр"),
]
UKRAINE_TOP = 10          # сколько новостей Украины показывать
FRESH_HOURS = 26          # «свежее» = за последние сутки с запасом
# YouTube: (название, @адрес или поиск, если адрес не известен)
YOUTUBE = [
    ("Безпалов Медіа", "search:Безпалов медіа"),
    ("Як не стати овочем", "search:Як не стати овочем Оксана Мороз"),
    ("Розмова про гроші", "@rozmova_pro_groshi"),
    ("Fancy Something", "search:Fancy Something"),
    ("Аліна Дейнека", "@alina_deyneka"),
]

# Украина: убираем войну, удары, потери и рекламу — оставляем то, что влияет на жизнь
WAR = re.compile(
    r"удар|обстріл|обстрел|вибух|взрыв|атак|ракет|дрон|шахед|шахід|бпла|балісти|баллисти|"
    r"уламк|обломк|влучан|попадан|загин|загиб|погиб|вбит|убит|поранен|ранен|жертв|постражд|пострада|"
    r"руйнув|руйнац|разруш|пожеж|пожар|тривог|тревог|укритт|укрыт|фронт|штурм|окупант|оккупант|"
    r"полон|пленн|в плен|ліквід|ликвид|генштаб|бойов|боев|втрати ворога|потери врага|сирен|"
    r"#реклама|реклама|партнерськ|промокод|розіграш|розыгрыш",
    re.I)

esc = html.escape
problems = []  # что не загрузилось — печатаем в лог


def log(msg):
    print(msg, flush=True)


def get(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gazeta-bot",
        "Accept-Language": "uk,ru;q=0.8,en;q=0.5", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def hm(m):
    return "24:00" if m >= 1440 else f"{m // 60:02d}:{m % 60:02d}"


def clean(s):
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"[ \t ]+", " ", s).strip()


# ---------------- данные: сервер дашборда ----------------
def load_dash():
    try:
        d = json.loads(get(SERVER, {"X-Key": KEY}))
        log("Сервер дашборда: ок")
        return d
    except Exception as e:
        problems.append(f"сервер дашборда: {e}")
        return {}


# ---------------- данные: RSS ----------------
# Google Новости иногда отдают не статьи, а страницы автора/рубрики — их пропускаем
JUNK = re.compile(r"останні статті|последние статьи|всі статті|все статьи|досьє|досье|\|\s*MC:|автор[иа]? MC", re.I)


def load_feed(urls, n=5):
    for url in urls:
        try:
            root = ET.fromstring(get(url))
            items = []
            for it in root.iter("item"):
                t = (it.findtext("title") or "").strip()
                if "news.google" in url:
                    t = re.sub(r"\s+[-–—]\s+[^-–—]{2,40}$", "", t)
                l = (it.findtext("link") or "").strip()
                if t and l and not JUNK.search(t):
                    items.append({"title": t, "link": l})
                if len(items) >= n:
                    break
            if items:
                log(f"RSS {url}: {len(items)}")
                return items
        except Exception as e:
            problems.append(f"RSS {url}: {e}")
    return []


# ---------------- данные: Telegram ----------------
def parse_views(s):
    s = s.strip().upper().replace(",", ".")
    try:
        if s.endswith("K"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("M"):
            return int(float(s[:-1]) * 1_000_000)
        return int(float(s))
    except Exception:
        return 0


def tg_page(handle, before=None):
    url = f"https://t.me/s/{handle}" + (f"?before={before}" if before else "")
    page = get(url).decode("utf-8", "ignore")
    m = re.search(r'tgme_channel_info_header_title[^>]*>\s*<span[^>]*>(.*?)</span>', page, re.S)
    title = clean(m.group(1)) if m else ""
    posts = []
    for block in re.split(r'<div class="tgme_widget_message_wrap', page)[1:]:
        m_link = re.search(r'data-post="([^"]+)"', block)
        m_text = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not (m_link and m_text):
            continue
        text = clean(m_text.group(1))
        if not text:
            continue
        m_time = re.search(r'<time[^>]+datetime="([^"]+)"', block)
        m_views = re.search(r'tgme_widget_message_views">([^<]+)<', block)
        when = None
        if m_time:
            try:
                when = datetime.datetime.fromisoformat(m_time.group(1))
            except Exception:
                pass
        # заголовок = первая строка поста
        first = next((ln.strip() for ln in text.split("\n") if len(ln.strip()) > 15), text.split("\n")[0])
        posts.append({"title": first, "full": text, "link": f"https://t.me/{m_link.group(1)}",
                      "id": int(m_link.group(1).rsplit("/", 1)[-1]), "time": when,
                      "views": parse_views(m_views.group(1)) if m_views else 0})
    return title, posts


def resolve_tg(name, handles, must):
    """Пробует адреса по очереди и берёт тот, у которого правильное название канала."""
    for h in handles:
        try:
            title, posts = tg_page(h)
        except Exception as e:
            problems.append(f"Telegram {h}: {e}")
            continue
        if posts and (not must or re.search(must, title, re.I)):
            if h != handles[0]:
                log(f"Telegram {name}: нашёлся по адресу @{h} («{title}»)")
            return h, posts
    problems.append(f"Telegram {name}: не нашёлся ни по одному адресу ({', '.join(handles)})")
    return None, []


def load_tg(name, handles, must, since, pages=1):
    """Посты канала за последние сутки (для больших каналов листает несколько страниц)."""
    h, posts = resolve_tg(name, handles, must)
    if not h:
        return []
    allp = list(posts)
    for _ in range(pages - 1):
        if not allp or not allp[0]["time"] or allp[0]["time"] < since:
            break
        try:
            _, older = tg_page(h, before=allp[0]["id"])
        except Exception:
            break
        if not older:
            break
        allp = older + allp
    fresh = [p for p in allp if p["time"] is None or p["time"] >= since]
    log(f"Telegram {name}: {len(fresh)} постов за сутки")
    return fresh[::-1]  # свежие сверху


# ---------------- данные: YouTube ----------------
YT_HEADERS = {"Cookie": "CONSENT=YES+1; SOCS=CAI"}


def yt_channel_id(spec):
    if spec.startswith("@"):
        page = get(f"https://www.youtube.com/{urllib.parse.quote(spec)}", YT_HEADERS).decode("utf-8", "ignore")
        m = re.search(r'"(?:externalId|channelId)":"(UC[\w-]{22})"', page) or re.search(r'channel/(UC[\w-]{22})', page)
        return (m.group(1), spec) if m else (None, None)
    q = spec.split(":", 1)[1]
    page = get("https://www.youtube.com/results?sp=EgIQAg%253D%253D&search_query=" + urllib.parse.quote(q),
               YT_HEADERS).decode("utf-8", "ignore")
    m = re.search(r'"channelRenderer":\{"channelId":"(UC[\w-]{22})","title":\{"simpleText":"([^"]+)"', page)
    return (m.group(1), m.group(2)) if m else (None, None)


def load_youtube(since):
    out = []
    for name, spec in YOUTUBE:
        try:
            cid, found = yt_channel_id(spec)
            if not cid:
                problems.append(f"YouTube {name}: канал не найден")
                continue
            if spec.startswith("search:"):
                log(f"YouTube {name}: по поиску нашёлся канал «{found}»")
            root = ET.fromstring(get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"))
            ns = {"a": "http://www.w3.org/2005/Atom"}
            n = 0
            for e in root.findall("a:entry", ns):
                pub = datetime.datetime.fromisoformat(e.findtext("a:published", "", ns).replace("Z", "+00:00"))
                if pub < since:
                    continue
                link = e.find("a:link", ns).get("href")
                if "/shorts/" in link:
                    continue
                out.append({"title": e.findtext("a:title", "", ns), "link": link, "src": name, "time": pub})
                n += 1
            log(f"YouTube {name}: новых видео {n}")
        except Exception as e:
            problems.append(f"YouTube {name}: {e}")
    return sorted(out, key=lambda v: v["time"], reverse=True)


# ---------------- данные: LUNO ----------------
def load_luno():
    """Задачи LUNO на сегодня (функция gazeta_today в Supabase)."""
    if not (LUNO_URL and LUNO_KEY and LUNO_SECRET):
        log("LUNO: секреты не заданы, блок пропущен")
        return None
    try:
        req = urllib.request.Request(
            f"{LUNO_URL}/rest/v1/rpc/gazeta_today",
            data=json.dumps({"secret": LUNO_SECRET}).encode(),
            headers={"apikey": LUNO_KEY, "Content-Type": "application/json",
                     # старый ключ anon (JWT, начинается на eyJ) нужен и в Authorization;
                     # новый publishable (sb_publishable_…) — только в apikey
                     **({"Authorization": f"Bearer {LUNO_KEY}"} if LUNO_KEY.startswith("eyJ") else {})})
        with urllib.request.urlopen(req, timeout=25) as r:
            tasks = json.loads(r.read())
        if isinstance(tasks, str):
            tasks = json.loads(tasks)
        log(f"LUNO: задач на сегодня {len(tasks)}")
        return tasks
    except urllib.error.HTTPError as e:
        problems.append(f"LUNO: {e.code} {e.read().decode('utf-8', 'ignore')[:200]}")
    except Exception as e:
        problems.append(f"LUNO: {e}")
    return None


def plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def luno_block(tasks):
    if tasks is None:
        return ""
    if not tasks:
        return '<section class="card"><h2>🌙 LUNO · на сегодня ничего не запланировано</h2></section>'
    left = [t for t in tasks if not t.get("done")]
    done = [t for t in tasks if t.get("done")]
    li = ""
    for t in left + done:
        icon = "✓" if t.get("done") else "○"
        fam = f' <span class="sub">· {esc(str(t.get("familiar_icon") or ""))} {esc(t["familiar"])}</span>' if t.get("familiar") else ""
        xp = f'<span class="xp">+{t["xp"]} XP</span>' if t.get("xp") else ""
        tag = {"overdue": '<span class="tag red">просрочено</span>',
               "deadline": '<span class="tag org">дедлайн сегодня</span>',
               "due": '<span class="tag">🔁</span>'}.get(t.get("status"), "")
        li += (f'<li class="task{" done" if t.get("done") else ""}"><span class="chk">{icon}</span>'
               f'<span class="tn">{esc(str(t.get("name", "")))}{tag}{fam}</span>{xp}</li>')
    n = len(left)
    head = f"{n} {plural(n, 'задача', 'задачи', 'задач')}" + (f" · сделано {len(done)}" if done else "")
    return (f'<section class="card"><details><summary><h2>🌙 LUNO · {head}</h2>'
            f'<span class="more">показать</span></summary><ul class="tasks">{li}</ul></details></section>')


# ---------------- свет ----------------
def pick_days(P, today_str):
    today, tomorrow = P.get("today"), P.get("tomorrow")
    if today and today.get("date") != today_str and tomorrow and tomorrow.get("date") == today_str:
        today, tomorrow = tomorrow, None
    return today, tomorrow


def outages_text(day):
    if not day:
        return "график ещё не опубликован"
    if not day.get("hasSchedule"):
        return day.get("label") or "нет данных"
    o = day.get("outages") or []
    if not o:
        return "Отключений нет"
    return "Без света: " + ", ".join(
        f"{hm(x['start'])}–{hm(x['end'])}{'?' if x.get('type') == 'possible' else ''}" for x in o)


def light_next(day, now_min):
    """Только ближайшее изменение: «свет выключат в 13:00» или «свет будет с 16:00»."""
    if not day:
        return "график света ещё не опубликован"
    if not day.get("hasSchedule"):
        return day.get("label") or "графика света нет"
    outs = sorted(day.get("outages") or [], key=lambda x: x["start"])
    if not outs:
        return "сегодня без отключений"
    for o in outs:
        maybe = " (возможно)" if o.get("type") == "possible" else ""
        if o["start"] <= now_min < o["end"]:
            return f"света нет, будет с {hm(o['end'])}{maybe}" if o["end"] < 1440 else f"света нет до конца дня{maybe}"
        if o["start"] > now_min:
            return f"свет выключат в {hm(o['start'])}{maybe}"
    return "отключений больше не будет"


def bar(day):
    if not day or not day.get("hasSchedule"):
        return ""
    segs = []
    for x in sorted(day.get("outages") or [], key=lambda x: x["start"]):
        cls = "maybe" if x.get("type") == "possible" else "off"
        segs.append(f'<i class="{cls}" style="left:{x["start"] / 14.4:.2f}%;width:{max(0.4, (x["end"] - x["start"]) / 14.4):.2f}%"></i>')
    ticks = "".join(f"<span>{h}</span>" for h in (0, 6, 12, 18, 24))
    return f'<div class="bar">{"".join(segs)}</div><div class="ticks">{ticks}</div>'


# ---------------- главное за 5 секунд ----------------
def summary_lines(d, luno, now):
    P = d.get("schedule") or {}
    t, _ = pick_days(P, now.strftime("%Y-%m-%d")) if P.get("today") else (None, None)
    lines = [("💡", light_next(t, now.hour * 60 + now.minute) if P else "график света недоступен")]
    W = d.get("weather")
    if W:
        w = f"{sign(W.get('temp'))}°, {str(W.get('text', '')).lower()}"
        lines.append(("☔" if W.get("umbrella") else "🌤", w + (" — возьми зонт" if W.get("umbrella") else "")))
    advice = None
    if luno is not None:
        left = [x for x in luno if not x.get("done")]
        busy = len(left) > 10
        lines.append(("🌙", "день насыщенный, но всё по силам" if busy else "сегодня спокойный день"))
        if any(x.get("status") == "overdue" for x in left):
            advice = "Начни с одного старого хвоста — дальше пойдёт легче."
        elif busy:
            advice = "Выбери три главных дела, остальное подождёт."
        else:
            advice = "Хороший день, чтобы закрыть что-то давнее."
    return lines, advice


# ---------------- фильтр повторов ----------------
STOP = set("це що як для про від та або але вже його її їх при під над без через після щодо цей ця які який яка "
           "это как для что про при уже его она они".split())


def words(title):
    ws = re.findall(r"[a-zа-яіїєґё0-9]+", title.lower())
    return {w[:5] for w in ws if (len(w) >= 3 or w.isdigit()) and w not in STOP}


def same(a, b):
    if not a or not b:
        return False
    common = len(a & b)
    return common >= 3 and common / min(len(a), len(b)) >= 0.5


def dedupe(groups):
    """groups: [(источник, [новости])] по приоритету — по всей газете сразу.
    Повтор убирается, а у оригинала отмечается, где ещё об этом писали."""
    seen, out, removed = [], [], 0
    for name, items in groups:
        kept = []
        for it in items:
            w = words(it["title"] + " " + it.get("full", "")[:200])
            dup = next((orig for ow, orig in seen if same(w, ow)), None)
            if dup is not None:
                if name != dup["src"] and name not in dup["also"]:
                    dup["also"].append(name)
                dup["views"] = max(dup.get("views", 0), it.get("views", 0))
                removed += 1
                continue
            it["also"] = []
            seen.append((w, it))
            kept.append(it)
        out.append(kept)
    if removed:
        log(f"Фильтр повторов убрал: {removed}")
    return out


SEEN_FILE = "docs/seen.json"
SEEN_DAYS = 7  # сколько дней помнить показанные новости


def load_seen():
    try:
        return json.load(open(SEEN_FILE, encoding="utf-8"))
    except Exception:
        return {}


def drop_seen(items, seen, today_str):
    """Убирает то, что уже было в прошлых выпусках (по ссылке или похожему заголовку).
    Сегодняшние повторные запуски не считаются — иначе пробный запуск съел бы утренний выпуск."""
    old = [x for day, xs in seen.items() if day < today_str for x in xs]
    old_links = {x["link"] for x in old}
    old_words = [set(x["words"]) for x in old]
    fresh = [it for it in items
             if it["link"] not in old_links and not any(same(words(it["title"]), w) for w in old_words)]
    return fresh, len(items) - len(fresh)


def save_seen(seen, shown, today_str):
    seen[today_str] = [{"link": i["link"], "words": sorted(words(i["title"]))} for i in shown]
    keep = sorted(seen)[-SEEN_DAYS:]
    os.makedirs("docs", exist_ok=True)
    json.dump({d: seen[d] for d in keep}, open(SEEN_FILE, "w", encoding="utf-8"), ensure_ascii=False)


# ---------------- курс ----------------
FX_FILE = "docs/fx.json"


def fx_compare(X, today_str):
    """Достаёт вчерашний курс из docs/fx.json и записывает сегодняшний."""
    try:
        hist = json.load(open(FX_FILE, encoding="utf-8"))
    except Exception:
        hist = {}
    past = sorted(d for d in hist if d < today_str)
    prev = hist[past[-1]] if past else {}
    if X:
        hist[today_str] = {k: X[k].get("buy") for k in ("usd", "eur") if X.get(k)}
        hist = {d: hist[d] for d in sorted(hist)[-7:]}  # храним неделю
        os.makedirs("docs", exist_ok=True)
        json.dump(hist, open(FX_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return prev


# ---------------- страница ----------------
def sign(v):
    try:
        return f"+{v}" if float(v) > 0 else str(v)
    except Exception:
        return str(v)


TITLE_MAX = 90  # максимальная длина заголовка в газете


def short(t, n=TITLE_MAX):
    t = re.sub(r"^[^\wА-Яа-яІіЇїЄєҐґ«\"(]+", "", t).strip()  # эмодзи и значки в начале
    if len(t) <= n:
        return t
    return t[:n].rsplit(" ", 1)[0].rstrip(",.:;—–- ") + "…"


def news_list(items, empty="Свежих новостей с прошлого выпуска нет", meta=True):
    if not items:
        return f'<p class="sub">{empty}</p>'
    out = ""
    for i in items:
        m = ""
        if meta:
            m = esc(i.get("src", ""))
            if i.get("also"):
                m += " · также: " + esc(", ".join(i["also"]))
            m = f'<span class="also">{m}</span>'
        out += f'<li><a href="{esc(i["link"])}">{esc(short(i["title"]))}</a>{m}</li>'
    return "<ul>" + out + "</ul>"


def mix(groups, limit=15):
    """Перемешивает источники по очереди, чтобы сверху не было пять новостей подряд из одного места."""
    out, i = [], 0
    while len(out) < limit and any(i < len(g) for g in groups):
        for g in groups:
            if i < len(g) and len(out) < limit:
                out.append(g[i])
        i += 1
    return out


def build(d, now, luno=None, prev_fx=None, ukraine=(), business=(), videos=()):
    prev_fx = prev_fx or {}
    today_str = now.strftime("%Y-%m-%d")
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    date_h = f"{now.day} {months[now.month - 1]}, {days[now.weekday()]}"

    # Главное
    lines, advice = summary_lines(d, luno, now)
    top = "".join(f'<p class="line"><span>{i}</span>{esc(t[0].upper() + t[1:])}</p>' for i, t in lines)
    if advice:
        top += f'<p class="advice">{esc(advice)}</p>'
    top = f'<section class="card top">{top}</section>'

    # Свет
    P = d.get("schedule") or {}
    if P.get("today"):
        t, tm = pick_days(P, today_str)
        light = f"""<section class="card">
          <h2>💡 Свет · очередь {esc(str(P.get('group', '12.1')))}</h2>
          <h3>Сегодня</h3>{bar(t)}<p>{esc(outages_text(t))}</p>
          <h3>Завтра</h3>{bar(tm)}<p>{esc(outages_text(tm))}</p></section>"""
    else:
        light = '<section class="card"><h2>💡 Свет</h2><p class="sub">График ДТЭК сейчас недоступен</p></section>'

    # Погода + воздух
    W, Q = d.get("weather"), d.get("air")
    if W:
        umb = '<p class="warn">☔ Возьми зонт</p>' if W.get("umbrella") else ""
        air = f'<p class="sub">Воздух: {esc(str(Q.get("label")))} (AQI {esc(str(Q.get("aqi")))})</p>' if Q else ""
        weather = f"""<section class="card half"><h2>🌤 Киев</h2>
          <div class="big">{sign(W.get('temp'))}°</div><p>{esc(str(W.get('text', '')))}</p>
          <p class="sub">ощущается {sign(W.get('feels'))}° · {esc(str(W.get('tmin')))}…{esc(str(W.get('tmax')))}° · ветер {esc(str(W.get('wind')))} м/с</p>
          <p class="sub">{esc(str(W.get('precipText', '')))}</p>{umb}{air}</section>"""
    else:
        weather = '<section class="card half"><h2>🌤 Киев</h2><p class="sub">Погода недоступна</p></section>'

    # Курс + изменение со вчера
    X = d.get("fx") or {}
    f = lambda v: "—" if v is None else f"{v:.2f}"
    rows = ""
    for k, sym in (("usd", "$"), ("eur", "€")):
        if X.get(k):
            buy, old = X[k].get("buy"), prev_fx.get(k)
            delta = ""
            if buy is not None and old is not None:
                diff = buy - old
                if abs(diff) < 0.005:
                    delta = '<span class="d same">= без изм.</span>'
                elif diff > 0:
                    delta = f'<span class="d up">▲ {diff:.2f}</span>'
                else:
                    delta = f'<span class="d down">▼ {abs(diff):.2f}</span>'
            rows += (f'<div class="fx"><span class="cur">{sym}</span><span>{f(buy)}</span>'
                     f'<span class="sub">{f(X[k].get("sell"))}</span></div>{delta}')
    note = "покупка · продажа · к вчера" if prev_fx else "покупка · продажа<br>сравнение со вчера появится завтра"
    fx = f'<section class="card half"><h2>💵 Курс</h2>{rows or "<p class=sub>нет данных</p>"}<p class="sub small">{note}</p></section>'

    ukr = f'<section class="card"><h2>🇺🇦 Украина · главное</h2>{news_list(ukraine, "Ничего важного с прошлого выпуска")}</section>'
    biz = f'<section class="card"><h2>📈 Бизнес и финансы</h2>{news_list(business)}</section>'
    vid = (f'<section class="card"><h2>▶️ Новые видео</h2>{news_list(videos)}</section>' if videos else "")

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ранок · {now.strftime('%d.%m')}</title>
<style>
:root{{--bg:#EEF0E2;--card:#E2E6CF;--top:#D6DCBA;--text:#2A2F17;--sub:#6D7355;--olive:#5E6E22;--on:#B8C67C;--off:#59603F;--maybe:#959B78;--warn:#B0502A}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1C1F14;--card:#272B1C;--top:#323823;--text:#E8EBD7;--sub:#9DA385;--olive:#AEBF66;--on:#55632A;--off:#0E100A;--maybe:#3C412C;--warn:#E58257}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,system-ui,"Segoe UI",Roboto,sans-serif}}
main{{max-width:680px;margin:0 auto;padding:22px 16px 40px}}
header{{text-align:center;margin-bottom:18px}}
header h1{{font:700 34px/1.1 Georgia,"Times New Roman",serif;margin:0;letter-spacing:.5px}}
header p{{margin:6px 0 0;color:var(--sub)}}
.card{{background:var(--card);border-radius:16px;padding:14px 16px;margin-bottom:12px}}
.top{{background:var(--top)}} .line{{display:flex;gap:10px;margin:2px 0;font-size:17px}} .line span{{width:22px;text-align:center}}
.advice{{margin:8px 0 0;color:var(--olive);font-style:italic}}
.grid{{display:flex;gap:12px;margin-bottom:12px}} .grid .card{{margin-bottom:0}} .grid .half{{flex:1;min-width:0}}
.grid .half:last-child{{flex:0 0 38%}}
h2{{font-size:15px;margin:0 0 8px;color:var(--olive)}} h2 a{{color:inherit;text-decoration:none}}
h3{{font-size:13px;margin:10px 0 4px;color:var(--sub);font-weight:600}}
p{{margin:4px 0}} .sub{{color:var(--sub);font-size:14px}} .small{{font-size:12px}} .warn{{color:var(--warn);font-weight:600}}
.big{{font-size:38px;font-weight:700;line-height:1}}
.bar{{position:relative;height:14px;border-radius:6px;background:var(--on);overflow:hidden}}
.bar i{{position:absolute;top:0;bottom:0;background:var(--off)}} .bar i.maybe{{background:var(--maybe)}}
.ticks{{display:flex;justify-content:space-between;font-size:11px;color:var(--sub);margin-top:2px}}
.fx{{display:flex;gap:8px;align-items:baseline;font-size:18px;font-weight:600}} .fx .cur{{color:var(--olive);width:14px}}
.fx .sub{{font-size:13px;font-weight:400}}
details summary{{list-style:none;cursor:pointer;display:flex;justify-content:space-between;align-items:baseline}}
details summary::-webkit-details-marker{{display:none}} details summary h2{{margin:0}}
.more{{font-size:12px;color:var(--sub)}} details[open] .more{{visibility:hidden}} details[open] summary{{margin-bottom:8px}}
ul{{margin:0;padding-left:18px}} li{{margin:8px 0}}
ul.tasks{{list-style:none;padding:0}} .task{{display:flex;gap:8px;align-items:baseline}} .chk{{color:var(--olive);font-weight:700;width:14px}}
.tn{{flex:1}} .xp{{font-size:12px;color:var(--olive)}} .tag{{font-size:11px;margin-left:6px;color:var(--sub)}}
.tag.red{{color:var(--warn);font-weight:600}} .tag.org{{color:#C47A1C;font-weight:600}} .task.done .tn{{text-decoration:line-through;color:var(--sub)}}
.d{{display:block;font-size:12px;margin:-2px 0 4px 22px}} .up{{color:var(--warn)}} .down{{color:var(--olive)}} .same{{color:var(--sub)}}
.also{{display:block;font-size:12px;color:var(--sub)}}
a{{color:var(--text)}} li a{{text-decoration:none;border-bottom:1px solid var(--maybe)}}
footer{{text-align:center;color:var(--sub);font-size:12px;margin-top:16px}}
@media (max-width:420px){{.grid{{flex-direction:column}} .grid .half:last-child{{flex:auto}}}}
</style></head><body><main>
<header><h1>Ранок</h1><p>{date_h}</p></header>
{top}
{light}
{luno_block(luno)}
<div class="grid">{weather}{fx}</div>
{ukr}
{biz}
{vid}
<footer>Выпуск собран в {now.strftime('%H:%M')} · новости ведут на оригинал</footer>
</main></body></html>"""


# ---------------- Telegram-бот ----------------
def tg(method, payload=None):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def diagnose():
    """Пишет в лог, какой бот в токене и кто ему писал — чтобы сразу видеть причину."""
    try:
        me = tg("getMe")["result"]
        log(f"Бот в токене: @{me.get('username')}")
    except Exception as e:
        log(f"Токен не работает: {e}")
        return None
    try:
        ups = tg("getUpdates").get("result", [])
        chats = {str(u[k]["chat"]["id"]) for u in ups for k in ("message", "edited_message") if k in u}
        log(f"Боту писали из чатов: {', '.join(chats) or 'никто (напиши боту /start)'}")
        return next(iter(chats)) if len(chats) == 1 else None
    except Exception as e:
        log(f"getUpdates: {e}")
        return None


def send(text, url, chat=None):
    data = json.dumps({
        "chat_id": chat or CHAT, "text": text, "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[{"text": "📰 Открыть газету", "url": url}]]},
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=25)
        log("Telegram: сообщение отправлено")
    except urllib.error.HTTPError as e:
        log(f"Telegram ответил ошибкой {e.code}: {e.read().decode('utf-8', 'ignore')}")
        raise


SENT_FILE = "docs/sent.json"


def already_sent(today_str):
    try:
        return json.load(open(SENT_FILE, encoding="utf-8")).get("date") == today_str
    except Exception:
        return False


def mark_sent(today_str, now):
    os.makedirs("docs", exist_ok=True)
    json.dump({"date": today_str, "time": now.strftime("%H:%M"), "by": EVENT}, open(SENT_FILE, "w", encoding="utf-8"))


# ---------------- сборка ----------------
def main():
    missing = [n for n, v in (("TELEGRAM_TOKEN", TOKEN), ("DASHBOARD_KEY", KEY)) if not v]
    if missing:
        log("Не найдены секреты: " + ", ".join(missing) + ". Проверь Settings → Secrets → Actions.")
        sys.exit(1)

    now = datetime.datetime.now(KYIV)
    today_str = now.strftime("%Y-%m-%d")
    # Запасное расписание GitHub не дублирует газету, если будильник уже сработал
    if EVENT == "schedule" and already_sent(today_str):
        log("Сегодняшний выпуск уже отправлен — запасной запуск ничего не делает.")
        return

    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=FRESH_HOURS)
    d = load_dash()
    luno = load_luno()

    # Новости: Украина и бизнес
    ukr_groups = []
    for name, handles, must in UKRAINE_TG:
        posts = load_tg(name, handles, must, since, pages=5)
        clean_posts = [p for p in posts if not WAR.search(p["full"])]
        if len(clean_posts) < len(posts):
            log(f"  {name}: убрано про войну/удары/рекламу: {len(posts) - len(clean_posts)}")
        ukr_groups.append((name, clean_posts))
    biz_groups = [(n, load_feed(urls)) for n, _, urls in FEEDS]
    for name, handles, must in BUSINESS_TG:
        biz_groups.append((name, load_tg(name, handles, must, since)[:3]))
    videos = load_youtube(since)

    for n, items in ukr_groups + biz_groups:
        for it in items:
            it["src"] = n

    # Уже было в прошлых выпусках — убираем
    seen = load_seen()
    dropped = 0
    groups = []
    for n, items in ukr_groups + biz_groups:
        fresh, k = drop_seen(items, seen, today_str)
        dropped += k
        groups.append((n, fresh))
    videos, k = drop_seen(videos, seen, today_str)
    dropped += k
    if dropped:
        log(f"Уже были в прошлых выпусках: {dropped}")

    # Один общий фильтр повторов на всю газету: сначала Украина, потом бизнес
    cleaned = dedupe(groups)
    nu = len(ukr_groups)
    ukr_all = [it for g in cleaned[:nu] for it in g]
    # Топ Украины: сколько каналов написали об этом, потом просмотры
    ukraine = sorted(ukr_all, key=lambda it: (len(it["also"]), it.get("views", 0)), reverse=True)[:UKRAINE_TOP]
    business = mix(cleaned[nu:], limit=40)

    save_seen(seen, ukraine + business + videos, today_str)
    prev_fx = fx_compare(d.get("fx"), today_str)

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(build(d, now, luno, prev_fx, ukraine, business, videos))
    log("Страница собрана: docs/index.html")

    user, repo = REPO.split("/")
    url = f"https://{user.lower()}.github.io/{repo}/?v={now.strftime('%Y%m%d%H%M')}"

    lines, _ = summary_lines(d, luno, now)
    msg = "☀️ <b>Доброе утро, Ксеня!</b>\n" + "\n".join(f"{i} {esc(t[0].upper() + t[1:])}" for i, t in lines if i != "🌙")
    if luno is not None:
        n = len([x for x in luno if not x.get("done")])
        msg += f"\n🌙 LUNO: {n} {plural(n, 'задача', 'задачи', 'задач')} на сегодня"
    try:
        send(msg, url)
    except urllib.error.HTTPError:
        fallback = diagnose()
        if fallback and fallback != CHAT:
            log("Пробую отправить в чат, из которого писали боту…")
            send(msg, url, fallback)
        else:
            raise
    mark_sent(today_str, now)

    if problems:
        log("\nЧто не загрузилось (газета всё равно ушла):")
        for p in problems:
            log(" - " + p)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
