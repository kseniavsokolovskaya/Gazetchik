import os, json, html, datetime, urllib.request, xml.etree.ElementTree as ET

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]
KEY = os.environ["DASHBOARD_KEY"]
REPO = os.environ.get("GITHUB_REPOSITORY", "user/gazeta")
SERVER = "https://home-dashboard.dzyn.workers.dev/api"

FEEDS = [
    ("MC.today", "https://mc.today/feed/"),
    ("КОШТ", "https://kosht.media/feed/"),
]
CHANNELS = [
    ("Про Бізнес", "https://t.me/business_ua"),
    ("MPG: Бізнес", "https://t.me/mpg_finance"),
    ("Wallet Pro", "https://t.me/walletproua"),
]

def get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()

def hm(m):
    return "24:00" if m >= 1440 else f"{m//60:02d}:{m%60:02d}"

def load_dash():
    try:
        return json.loads(get(SERVER, {"X-Key": KEY}))
    except Exception as e:
        return {"error": str(e)}

def load_feed(url, n=6):
    try:
        root = ET.fromstring(get(url))
        items = []
        for it in root.iter("item"):
            items.append({
                "title": (it.findtext("title") or "").strip(),
                "link": (it.findtext("link") or "").strip(),
            })
            if len(items) >= n:
                break
        return items
    except Exception:
        return []

def outages_text(day):
    if not day:
        return "график ещё не опубликован"
    if not day.get("hasSchedule"):
        return day.get("label", "нет данных")
    o = day.get("outages", [])
    if not o:
        return "Отключений нет 🎉"
    return "Без света: " + ", ".join(
        f"{hm(x['start'])}–{hm(x['end'])}{'?' if x.get('type') == 'possible' else ''}" for x in o)

def bar(day):
    if not day or not day.get("hasSchedule"):
        return ""
    cells = []
    for h in range(24):
        s, e = h * 60, h * 60 + 60
        cls = "on"
        for x in day.get("outages", []):
            if x["start"] < e and x["end"] > s:
                cls = "maybe" if x.get("type") == "possible" else "off"
        cells.append(f'')
    return '' + "".join(cells) + ' 06121824'

def build(d, news):
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    date = now.strftime("%d.%m.%Y")
    P = d.get("schedule") or {}
    W = d.get("weather")
    X = d.get("fx")
    Q = d.get("air")
    esc = html.escape

    light = f""" 💡 Свет · очередь {esc(str(P.get('group', '12.1')))}
Сегодня {bar(P.get('today'))} {esc(outages_text(P.get('today')))}
Завтра {bar(P.get('tomorrow'))} {esc(outages_text(P.get('tomorrow')))} """ if P else " 💡 Свет График ДТЭК недоступен "

    if W:
        sign = lambda v: f"+{v}" if v > 0 else str(v)
        umb = " — возьми зонт ☔" if W.get("umbrella") else ""
        air = f" Воздух: {esc(Q['label'])} (AQI {Q['aqi']}) " if Q else ""
        weather = f""" 🌤 Погода · Киев {sign(W['temp'])}° {esc(W['text'])}
Ощущается {sign(W['feels'])}° · {W['tmin']}…{W['tmax']}° · ветер {W['wind']} м/с {esc(W.get('precipText',''))}{umb} {air} """
    else:
        weather = " 🌤 Погода Нет данных "

    if X:
        f = lambda v: "—" if v is None else f"{v:.2f}"
        rows = "".join(f" {s} покупка {f(X[k]['buy'])} · продажа {f(X[k]['sell'])} " for k, s in (("usd", "$"), ("eur", "€")) if X.get(k))
        fx = f" 💵 Курс {rows} "
    else:
        fx = ""

    blocks = ""
    for name, items in news:
        if items:
            li = "".join(f' {esc(i["title"])}' for i in items)
        else:
            li = " Не удалось загрузить "
        blocks += f" 📰 {esc(name)} {li} "

    ch = "".join(f' {esc(n)}' for n, u in CHANNELS)
    blocks += f" 📈 Финансы · каналы {ch} "

    return f"""<!doctype html>"""

def send(text, url):
    data = json.dumps({
        "chat_id": CHAT, "text": text, "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[{"text": "📰 Открыть газету", "url": url}]]},
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=25)

if __name__ == "__main__":
    d = load_dash()
    news = [(n, load_feed(u)) for n, u in FEEDS]
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(build(d, news))
    user, repo = REPO.split("/")
    url = f"https://{user.lower()}.github.io/{repo}/?v={datetime.date.today()}"
    P = d.get("schedule") or {}
    W = d.get("weather") or {}
    short = f"☀️ Доброе утро!\n💡 {html.escape(outages_text(P.get('today')))}"
    if W:
        short += f"\n🌤 {W.get('temp')}°, {html.escape(W.get('text', ''))}"
    send(short, url)
