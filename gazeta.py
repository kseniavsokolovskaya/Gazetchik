"""Утренняя газета Ксени: свет, погода, курс, новости. Запускается GitHub Actions в 10:00."""
import os, re, json, html, sys, datetime, traceback, urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

# strip/очистка: на случай, если в секрет случайно попали пробелы, кавычки или ```
TOKEN = re.sub(r"[^\w:-]", "", os.environ.get("TELEGRAM_TOKEN", ""))
CHAT = re.sub(r"[^\d-]", "", os.environ.get("TELEGRAM_CHAT_ID", "")) or "908434474"
KEY = os.environ.get("DASHBOARD_KEY", "").strip()
LUNO_URL = os.environ.get("LUNO_URL", "").strip().rstrip("/")
_m = re.search(r"project/([a-z0-9]{15,})", LUNO_URL) or (re.fullmatch(r"[a-z0-9]{15,}", LUNO_URL) and re.match(r"(.*)", LUNO_URL))
if _m:  # вставили ссылку на дашборд или только ID проекта — соберём адрес сами
    LUNO_URL = f"https://{_m.group(1)}.supabase.co"
LUNO_KEY = os.environ.get("LUNO_ANON_KEY", "").strip()
LUNO_SECRET = os.environ.get("LUNO_SECRET", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "user/gazeta")
SERVER = "https://home-dashboard.dzyn.workers.dev/api"
KYIV = ZoneInfo("Europe/Kyiv")

# Новости с сайтов (RSS)
FEEDS = [
    # сайт MC.today не пускает серверы GitHub (Forbidden), поэтому берём его статьи через Google Новости
    ("MC.today", "https://mc.today", [
        "https://news.google.com/rss/search?q=site:mc.today+when:2d&hl=uk&gl=UA&ceid=UA:uk",
        "https://mc.today/feed/"]),
    ("КОШТ", "https://kosht.media", ["https://kosht.media/feed/"]),
]
# Telegram-каналы про финансы (последние посты)
CHANNELS = [
    ("Про Бізнес", "business_ua"),
    ("MPG: Бізнес", "mpg_finance"),
    ("Wallet Pro", "walletproua"),
]

esc = html.escape
problems = []  # что не загрузилось — печатаем в лог


def log(msg):
    print(msg, flush=True)


def get(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (gazeta-bot)", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def hm(m):
    return "24:00" if m >= 1440 else f"{m // 60:02d}:{m % 60:02d}"


# ---------------- данные ----------------
def load_dash():
    try:
        d = json.loads(get(SERVER, {"X-Key": KEY}))
        log("Сервер дашборда: ок")
        return d
    except Exception as e:
        problems.append(f"сервер дашборда: {e}")
        return {}


def load_feed(urls, n=5):
    for url in urls:
        try:
            root = ET.fromstring(get(url))
            items = []
            for it in root.iter("item"):
                t = re.sub(r"\s+[-–—]\s+[^-–—]{2,40}$", "", (it.findtext("title") or "").strip()) if "news.google" in url else (it.findtext("title") or "").strip()
                l = (it.findtext("link") or "").strip()
                if t and l:
                    items.append({"title": t, "link": l})
                if len(items) >= n:
                    break
            if items:
                log(f"RSS {url}: {len(items)}")
                return items
        except Exception as e:
            problems.append(f"RSS {url}: {e}")
    return []


def clean(s):
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def load_channel(handle, n=3):
    """Последние посты публичного канала со страницы t.me/s/<канал>."""
    try:
        page = get(f"https://t.me/s/{handle}").decode("utf-8", "ignore")
        posts = []
        for block in re.split(r'<div class="tgme_widget_message_wrap', page)[1:]:
            m_link = re.search(r'data-post="([^"]+)"', block)
            m_text = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
            if not (m_link and m_text):
                continue
            text = clean(m_text.group(1))
            if not text:
                continue
            if len(text) > 170:
                text = text[:170].rsplit(" ", 1)[0] + "…"
            posts.append({"title": text, "link": f"https://t.me/{m_link.group(1)}"})
        posts = posts[-n:][::-1]  # самые свежие сверху
        log(f"Telegram {handle}: {len(posts)}")
        if not posts:
            problems.append(f"Telegram {handle}: постов не нашлось")
        return posts
    except Exception as e:
        problems.append(f"Telegram {handle}: {e}")
        return []


def load_luno():
    """Задачи LUNO, запланированные на сегодня (функция gazeta_today в Supabase)."""
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


def luno_block(tasks):
    if tasks is None:
        return ""
    if not tasks:
        return '<section class="card"><h2>🌙 LUNO · сегодня</h2><p class="sub">На сегодня ничего не запланировано</p></section>'
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
        fam = tag + fam
        li += f'<li class="task{" done" if t.get("done") else ""}"><span class="chk">{icon}</span><span class="tn">{esc(str(t.get("name", "")))}{fam}</span>{xp}</li>'
    head = f"осталось {len(left)} из {len(tasks)}" if done else f"{len(tasks)} {'квест' if len(tasks) == 1 else 'квеста' if len(tasks) < 5 else 'квестов'}"
    return f'<section class="card"><h2>🌙 LUNO · сегодня <span class="sub small">· {head}</span></h2><ul class="tasks">{li}</ul></section>'


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


def bar(day, now_min=None):
    if not day or not day.get("hasSchedule"):
        return ""
    segs = []
    for x in sorted(day.get("outages") or [], key=lambda x: x["start"]):
        cls = "maybe" if x.get("type") == "possible" else "off"
        left = x["start"] / 14.4
        width = max(0.4, (x["end"] - x["start"]) / 14.4)
        segs.append(f'<i class="{cls}" style="left:{left:.2f}%;width:{width:.2f}%"></i>')
    if now_min is not None:
        segs.append(f'<b class="now" style="left:{now_min / 14.4:.2f}%"></b>')
    ticks = "".join(f"<span>{h}</span>" for h in (0, 6, 12, 18, 24))
    return f'<div class="bar">{"".join(segs)}</div><div class="ticks">{ticks}</div>'


# ---------------- фильтр повторов ----------------
STOP = set("це що як для про від та або але вже його її їх при під над без через після щодо цей ця які який яка що это как для что про при уже его она они".split())


def words(title):
    ws = re.findall(r"[a-zа-яіїєґё0-9]+", title.lower())
    return {w[:5] for w in ws if (len(w) >= 3 or w.isdigit()) and w not in STOP}


def same(a, b):
    if not a or not b:
        return False
    common = len(a & b)
    return common >= 3 and common / min(len(a), len(b)) >= 0.5


def dedupe(groups):
    """groups: [(название источника, [новости])] по приоритету. Повторы убираем, помечаем «также в»."""
    seen = []  # (слова, новость)
    out = []
    removed = 0
    for name, items in groups:
        kept = []
        for it in items:
            w = words(it["title"])
            dup = next((orig for ow, orig in seen if same(w, ow)), None)
            if dup is not None:
                if name not in dup["also"]:
                    dup["also"].append(name)
                removed += 1
                continue
            it["also"] = []
            seen.append((w, it))
            kept.append(it)
        out.append(kept)
    if removed:
        log(f"Фильтр повторов убрал: {removed}")
    return out


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
    cut = t[:n].rsplit(" ", 1)[0].rstrip(",.:;—–- ")
    return cut + "…"


def news_list(items, empty="Свежих новостей с прошлого выпуска нет"):
    if not items:
        return f'<p class="sub">{empty}</p>'
    out = ""
    for i in items:
        meta = esc(i.get("src", ""))
        if i.get("also"):
            meta += " · также: " + esc(", ".join(i["also"]))
        out += f'<li><a href="{esc(i["link"])}">{esc(short(i["title"]))}</a><span class="also">{meta}</span></li>'
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


SEEN_FILE = "docs/seen.json"
SEEN_DAYS = 7  # сколько дней помнить показанные новости


def drop_seen(items, today_str):
    """Убирает новости, которые уже были в прошлых выпусках (по ссылке или похожему заголовку).
    Сегодняшние повторные запуски не считаются — иначе пробный запуск съел бы утренний выпуск."""
    try:
        seen = json.load(open(SEEN_FILE, encoding="utf-8"))
    except Exception:
        seen = {}
    old = [x for day, xs in seen.items() if day < today_str for x in xs]
    old_links = {x["link"] for x in old}
    old_words = [set(x["words"]) for x in old]
    fresh = [it for it in items
             if it["link"] not in old_links and not any(same(words(it["title"]), w) for w in old_words)]
    if len(fresh) < len(items):
        log(f"Уже были во вчерашних выпусках: {len(items) - len(fresh)}")
    return fresh, seen


def save_seen(seen, shown, today_str):
    seen[today_str] = [{"link": i["link"], "words": sorted(words(i["title"]))} for i in shown]
    keep = sorted(seen)[-SEEN_DAYS:]
    os.makedirs("docs", exist_ok=True)
    json.dump({d: seen[d] for d in keep}, open(SEEN_FILE, "w", encoding="utf-8"), ensure_ascii=False)


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


def build(d, news, now, prev_fx=None, luno=None):
    prev_fx = prev_fx or {}
    today_str = now.strftime("%Y-%m-%d")
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    date_h = f"{now.day} {months[now.month - 1]}, {days[now.weekday()]}"

    # Свет
    P = d.get("schedule") or {}
    if P.get("today"):
        t, tm = pick_days(P, today_str)
        light = f"""
        <section class="card">
          <h2>💡 Свет · очередь {esc(str(P.get('group', '12.1')))}</h2>
          <h3>Сегодня</h3>{bar(t)}<p>{esc(outages_text(t))}</p>
          <h3>Завтра</h3>{bar(tm)}<p>{esc(outages_text(tm))}</p>
        </section>"""
    else:
        light = '<section class="card"><h2>💡 Свет</h2><p class="sub">График ДТЭК сейчас недоступен</p></section>'

    # Погода + воздух
    W, Q = d.get("weather"), d.get("air")
    if W:
        umb = '<p class="warn">☔ Возьми зонт</p>' if W.get("umbrella") else ""
        air = f'<p class="sub">Воздух: {esc(str(Q.get("label")))} (AQI {esc(str(Q.get("aqi")))})</p>' if Q else ""
        weather = f"""
        <section class="card half">
          <h2>🌤 Киев</h2>
          <div class="big">{sign(W.get('temp'))}°</div>
          <p>{esc(str(W.get('text', '')))}</p>
          <p class="sub">ощущается {sign(W.get('feels'))}° · {esc(str(W.get('tmin')))}…{esc(str(W.get('tmax')))}° · ветер {esc(str(W.get('wind')))} м/с</p>
          <p class="sub">{esc(str(W.get('precipText', '')))}</p>{umb}{air}
        </section>"""
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

    news_block = f'<section class="card"><h2>📈 Бизнес и финансы</h2>{news_list(news)}</section>'

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Газета · {now.strftime('%d.%m')}</title>
<style>
:root{{--bg:#EEF0E2;--card:#E2E6CF;--text:#2A2F17;--sub:#6D7355;--olive:#5E6E22;--on:#B8C67C;--off:#59603F;--maybe:#959B78;--now:#1E2210;--warn:#B0502A}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1C1F14;--card:#272B1C;--text:#E8EBD7;--sub:#9DA385;--olive:#AEBF66;--on:#55632A;--off:#0E100A;--maybe:#3C412C;--now:#F2F5E2;--warn:#E58257}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,system-ui,"Segoe UI",Roboto,sans-serif}}
main{{max-width:680px;margin:0 auto;padding:22px 16px 40px}}
header{{text-align:center;margin-bottom:18px}}
header h1{{font:700 34px/1.1 Georgia,"Times New Roman",serif;margin:0;letter-spacing:.5px}}
header p{{margin:6px 0 0;color:var(--sub)}}
.card{{background:var(--card);border-radius:16px;padding:14px 16px;margin-bottom:12px}}
.grid{{display:flex;gap:12px;margin-bottom:12px}} .grid .card{{margin-bottom:0}} .grid .half{{flex:1;min-width:0}}
.grid .half:last-child{{flex:0 0 38%}}
h2{{font-size:15px;margin:0 0 8px;color:var(--olive)}} h2 a{{color:inherit;text-decoration:none}}
h3{{font-size:13px;margin:10px 0 4px;color:var(--sub);font-weight:600}} h3 a{{color:inherit}}
p{{margin:4px 0}} .sub{{color:var(--sub);font-size:14px}} .small{{font-size:12px}} .warn{{color:var(--warn);font-weight:600}}
.big{{font-size:38px;font-weight:700;line-height:1}}
.bar{{position:relative;height:14px;border-radius:6px;background:var(--on);overflow:hidden}}
.bar i{{position:absolute;top:0;bottom:0;background:var(--off)}} .bar i.maybe{{background:var(--maybe)}}
.bar b.now{{position:absolute;top:0;bottom:0;width:2px;background:var(--now)}}
.ticks{{display:flex;justify-content:space-between;font-size:11px;color:var(--sub);margin-top:2px}}
.fx{{display:flex;gap:8px;align-items:baseline;font-size:18px;font-weight:600}} .fx .cur{{color:var(--olive);width:14px}}
.fx .sub{{font-size:13px;font-weight:400}}
ul{{margin:0;padding-left:18px}} li{{margin:8px 0}} ul.tasks{{list-style:none;padding:0}} .task{{display:flex;gap:8px;align-items:baseline}} .chk{{color:var(--olive);font-weight:700;width:14px}} .tn{{flex:1}} .xp{{font-size:12px;color:var(--olive)}} .tag{{font-size:11px;margin-left:6px;color:var(--sub)}} .tag.red{{color:var(--warn);font-weight:600}} .tag.org{{color:#C47A1C;font-weight:600}} .task.done .tn{{text-decoration:line-through;color:var(--sub)}} .d{{display:block;font-size:12px;margin:-2px 0 4px 22px}} .up{{color:var(--warn)}} .down{{color:var(--olive)}} .same{{color:var(--sub)}} .also{{display:block;font-size:12px;color:var(--sub)}}
a{{color:var(--text)}} li a{{text-decoration:none;border-bottom:1px solid var(--maybe)}}
footer{{text-align:center;color:var(--sub);font-size:12px;margin-top:16px}}
@media (max-width:420px){{.grid{{flex-direction:column}} .grid .half:last-child{{flex:auto}}}}
</style></head><body><main>
<header><h1>Ранок</h1><p>{date_h}</p></header>
{light}
{luno_block(luno)}
<div class="grid">{weather}{fx}</div>
{news_block}
<footer>Выпуск собран в {now.strftime('%H:%M')} · новости ведут на оригинал</footer>
</main></body></html>"""


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
        log(f"Номер чата из секрета: {len(CHAT)} цифр, начинается на {CHAT[:3]}…")
        log(f"Боту писали из чатов: {', '.join(c[:3] + '…' for c in chats) or 'никто (напиши боту /start)'}")
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


def main():
    missing = [n for n, v in (("TELEGRAM_TOKEN", TOKEN), ("TELEGRAM_CHAT_ID", CHAT), ("DASHBOARD_KEY", KEY)) if not v]
    if missing:
        log("Не найдены секреты: " + ", ".join(missing) + ". Проверь Settings → Secrets → Actions.")
        sys.exit(1)

    now = datetime.datetime.now(KYIV)
    d = load_dash()
    feeds = [(name, site, load_feed(urls)) for name, site, urls in FEEDS]
    channels = [(name, h, load_channel(h)) for name, h in CHANNELS]
    groups = [(n, items) for n, _, items in feeds] + [(n, items) for n, _, items in channels]
    for n, items in groups:
        for it in items:
            it["src"] = n
    today_str = now.strftime("%Y-%m-%d")
    seen = {}
    filtered = []
    for n, items in groups:
        fresh, seen = drop_seen(items, today_str)
        filtered.append((n, fresh))
    news = mix(dedupe(filtered))
    save_seen(seen, news, today_str)
    luno = load_luno()
    prev_fx = fx_compare(d.get("fx"), now.strftime("%Y-%m-%d"))

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(build(d, news, now, prev_fx, luno))
    log("Страница собрана: docs/index.html")

    user, repo = REPO.split("/")
    url = f"https://{user.lower()}.github.io/{repo}/?v={now.strftime('%Y%m%d%H%M')}"

    P = d.get("schedule") or {}
    t, _ = pick_days(P, now.strftime("%Y-%m-%d")) if P.get("today") else (None, None)
    W = d.get("weather") or {}
    msg = "☀️ <b>Доброе утро, Ксеня!</b>\n"
    msg += f"💡 {esc(outages_text(t)) if P else 'график света недоступен'}\n"
    if W:
        msg += f"🌤 {sign(W.get('temp'))}°, {esc(str(W.get('text', '')))}"
        if W.get("umbrella"):
            msg += " · ☔ возьми зонт"
    if luno:
        msg = msg.rstrip("\n") + f"\n🌙 LUNO: на сегодня {len(luno)} — " + ", ".join(esc(str(t.get('name', ''))) for t in luno[:3]) + ("…" if len(luno) > 3 else "")
    try:
        send(msg, url)
    except urllib.error.HTTPError:
        fallback = diagnose()
        if fallback and fallback != CHAT:
            log("Пробую отправить в чат, из которого писали боту…")
            send(msg, url, fallback)
        else:
            raise

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
