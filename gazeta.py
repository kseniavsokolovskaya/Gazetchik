"""Утренняя газета Ксени: свет, погода, курс, новости. Запускается GitHub Actions в 10:00."""
import os, re, json, html, sys, datetime, traceback, urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

# strip/очистка: на случай, если в секрет случайно попали пробелы, кавычки или ```
TOKEN = re.sub(r"[^\w:-]", "", os.environ.get("TELEGRAM_TOKEN", ""))
CHAT = re.sub(r"[^\d-]", "", os.environ.get("TELEGRAM_CHAT_ID", "")) or "908434474"
KEY = os.environ.get("DASHBOARD_KEY", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "user/gazeta")
SERVER = "https://home-dashboard.dzyn.workers.dev/api"
KYIV = ZoneInfo("Europe/Kyiv")

# Новости с сайтов (RSS)
FEEDS = [
    ("MC.today", "https://mc.today", ["https://mc.today/feed/", "https://mc.today/uk/feed/"]),
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
                t = (it.findtext("title") or "").strip()
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


# ---------------- страница ----------------
def sign(v):
    try:
        return f"+{v}" if float(v) > 0 else str(v)
    except Exception:
        return str(v)


def news_list(items, empty="Сегодня не загрузилось"):
    if not items:
        return f'<p class="sub">{empty}</p>'
    return "<ul>" + "".join(
        f'<li><a href="{esc(i["link"])}">{esc(i["title"])}</a></li>' for i in items) + "</ul>"


def build(d, feeds, channels, now):
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

    # Курс
    X = d.get("fx") or {}
    rows = ""
    for k, s in (("usd", "$"), ("eur", "€")):
        if X.get(k):
            f = lambda v: "—" if v is None else f"{v:.2f}"
            rows += f'<div class="fx"><span class="cur">{s}</span><span>{f(X[k].get("buy"))}</span><span class="sub">{f(X[k].get("sell"))}</span></div>'
    fx = f'<section class="card half"><h2>💵 Курс</h2>{rows or "<p class=sub>нет данных</p>"}<p class="sub small">покупка · продажа</p></section>'

    news = "".join(
        f'<section class="card"><h2>📰 <a href="{esc(site)}">{esc(name)}</a></h2>{news_list(items)}</section>'
        for name, site, items in feeds)
    tg = "".join(
        f'<h3><a href="https://t.me/{h}">{esc(name)}</a></h3>{news_list(items, "Постов не видно")}'
        for name, h, items in channels)
    tg = f'<section class="card"><h2>📈 Финансы · Telegram</h2>{tg}</section>'

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
ul{{margin:0;padding-left:18px}} li{{margin:6px 0}}
a{{color:var(--text)}} li a{{text-decoration:none;border-bottom:1px solid var(--maybe)}}
footer{{text-align:center;color:var(--sub);font-size:12px;margin-top:16px}}
@media (max-width:420px){{.grid{{flex-direction:column}} .grid .half:last-child{{flex:auto}}}}
</style></head><body><main>
<header><h1>Ранок</h1><p>{date_h}</p></header>
{light}
<div class="grid">{weather}{fx}</div>
{news}
{tg}
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

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(build(d, feeds, channels, now))
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
