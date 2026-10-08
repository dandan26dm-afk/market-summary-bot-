#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
בוט "הסיפור של היום" – סיכום שוק יומי לדיסקורד.

מושך נתונים אמיתיים מ-Yahoo Finance, בונה תמונה בעברית ושולח אותה ל-Discord.

בדיקה מקומית:
    python market_bot.py --no-send                 # נתונים אמיתיים, שומר PNG בלי לשלוח
    python market_bot.py --demo calm --no-send     # נתוני דוגמה: calm / fear / panic
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

# =====================================================================
#                      הגדרות – כאן משנים דברים
# =====================================================================

SEND_HOUR_IL = 17           # שעת השליחה בשעון ישראל
SEND_MINUTE_IL = 0
MAX_LATE_HOURS = 3          # אם GitHub איחר יותר מזה – לא שולחים באותו יום

# חייב להתאים בדיוק לשורות ה-cron בקובץ .github/workflows/market-summary.yml
CRON_SUMMER = "0 13 * * 1-5"   # שעון קיץ בישראל (UTC+3) → מתעורר ב-16:00
CRON_WINTER = "0 14 * * 1-5"   # שעון חורף בישראל (UTC+2) → מתעורר ב-16:00 ב-16:30

# הרשימה שלך: טיקר → תגית קטנה שמופיעה ליד השם ("" = בלי תגית)
WATCHLIST = {
    "NVDA": "שבבים",
    "PLTR": "תוכנה",
    "IGV": "תוכנה",
    "GEV": "אנרגיה",
    "MAGS": "מגה-קאפ",
    "SOXX": "שבבים",
    "AAPL": "מגה-קאפ",
    "GOOGL": "מגה-קאפ",
    "NOW": "תוכנה",
    "AMZN": "מגה-קאפ",
    "TSLA": "מגה-קאפ",
    "META": "מגה-קאפ",
    "MSFT": "מגה-קאפ",
    "AVGO": "שבבים",
    "AMD": "שבבים",
    "SPCX": "",
    "WGMI": "כורי ביטקוין",
}
TOP_N = 5   # כמה מניות בכל צד

# החלק התחתון: (שם שמוצג, סימול ב-Yahoo, סוג תצוגה, תגית)
#   index = מספר שלם מעל 1000 / usd = עם $ / yield = תשואה ב-% ושינוי ב-bps
MACRO = [
    ("S&P 500", "^GSPC", "index", ""),
    ("QQQ", "QQQ", "price", ""),
    ("VIX", "^VIX", "index", ""),
    ("RUSSELL", "^RUT", "index", ""),
    ("RSP", "RSP", "price", "שוויוני"),
    ("BTCUSD", "BTC-USD", "usd", ""),
    ("USOIL", "CL=F", "price", ""),      # נפט WTI (חוזה עתידי)
    ("US10Y", "^TNX", "yield", ""),      # תשואת אג"ח 10 שנים
]

# סימולים שנסחרים 24/7 או בחוזים – לא בודקים להם "האם יש נתון של היום"
NON_US_SESSION = {"BTC-USD", "CL=F"}

# ספי ה-VIX
VIX_FEAR = 20
VIX_PANIC = 30

# מתחת לשינוי הזה (באחוזים, לכל כיוון) ה-S&P נחשב "ללא שינוי" → רקע אפור-כחלחל
FLAT_BAND = 0.20

# חדשות
NEWS_SOURCES = ["^GSPC", "SPY", "QQQ"]
NEWS_MAX_AGE_HOURS = 20
SEND_NEWS_LINK = True        # קישור לכתבה המלאה מתחת לתמונה בדיסקורד

# שמות חברות – כדי להעדיף כותרת שמדברת על מניה מהרשימה
COMPANY_NAMES = {
    "NVDA": ["nvidia"], "PLTR": ["palantir"], "GEV": ["ge vernova"],
    "AAPL": ["apple"], "GOOGL": ["alphabet", "google"], "NOW": ["servicenow"],
    "AMZN": ["amazon"], "TSLA": ["tesla"], "META": ["meta platforms", "meta"],
    "MSFT": ["microsoft"], "AVGO": ["broadcom"], "AMD": ["amd"], "SPCX": ["spacex"],
}
MARKET_WORDS = ["stock market", "stocks", "wall street", "s&p", "nasdaq", "dow",
                "fed", "rally", "sell-off", "selloff", "treasury", "yields", "futures"]

# =====================================================================

BASE_DIR = Path(__file__).resolve().parent
IL = ZoneInfo("Asia/Jerusalem")
NY = ZoneInfo("America/New_York")


@dataclass
class Quote:
    symbol: str
    price: float | None
    prev: float | None
    last_date: dt.date | None = None

    @property
    def pct(self) -> float | None:
        if self.price is None or not self.prev:
            return None
        return (self.price / self.prev - 1) * 100


@dataclass
class Headline:
    title: str
    provider: str
    url: str | None
    published: dt.datetime | None


# ---------------------------------------------------------------------
#  תזמון
# ---------------------------------------------------------------------

def expected_cron(now_utc: dt.datetime) -> str:
    offset_h = now_utc.astimezone(IL).utcoffset().total_seconds() / 3600
    return CRON_SUMMER if offset_h >= 3 else CRON_WINTER


def gate() -> bool:
    """GitHub מריץ שתי שורות cron (קיץ/חורף). רק אחת מהן מתאימה להיום."""
    sched = os.environ.get("TRIGGER_SCHEDULE", "").strip()
    if not sched:
        print("הרצה ידנית – ממשיכים מיד.")
        return True
    exp = expected_cron(dt.datetime.now(dt.timezone.utc))
    if sched == exp:
        print(f"ה-cron '{sched}' מתאים לשעון הנוכחי בישראל – ממשיכים.")
        return True
    print(f"ה-cron '{sched}' לא מתאים לעונה הנוכחית (מצופה '{exp}') – מדלגים.")
    return False


def wait_until_send_time() -> bool:
    now = dt.datetime.now(IL)
    target = now.replace(hour=SEND_HOUR_IL, minute=SEND_MINUTE_IL, second=0, microsecond=0)
    if now < target:
        secs = (target - now).total_seconds()
        print(f"ממתינים {secs / 60:.1f} דקות עד {target:%H:%M} שעון ישראל...")
        time.sleep(secs)
        return True
    late_h = (now - target).total_seconds() / 3600
    if late_h > MAX_LATE_HOURS:
        print(f"GitHub איחר ב-{late_h:.1f} שעות – לא שולחים היום.")
        return False
    if late_h > 0.02:
        print(f"GitHub איחר ב-{late_h * 60:.0f} דקות – שולחים עכשיו.")
    return True


# ---------------------------------------------------------------------
#  נתונים מ-Yahoo Finance
# ---------------------------------------------------------------------

def _quote_from_frame(sym: str, frame) -> Quote | None:
    if frame is None or getattr(frame, "empty", True) or "Close" not in frame:
        return None
    closes = frame["Close"].dropna()
    if len(closes) < 2:
        return None
    return Quote(sym, float(closes.iloc[-1]), float(closes.iloc[-2]), closes.index[-1].date())


def _single_quote(sym: str) -> Quote | None:
    import yfinance as yf
    for attempt in range(3):
        try:
            hist = yf.Ticker(sym).history(period="10d", interval="1d", auto_adjust=False)
            q = _quote_from_frame(sym, hist)
            if q:
                return q
        except Exception as e:
            print(f"  {sym}: ניסיון {attempt + 1} נכשל ({e})")
        time.sleep(3 * (attempt + 1))
    return None


def fetch_quotes(symbols: list[str]) -> dict[str, Quote]:
    """מחיר אחרון (כולל המסחר של היום) + סגירה קודמת, לכל סימול."""
    import yfinance as yf
    frame = None
    for attempt in range(3):
        try:
            frame = yf.download(symbols, period="10d", interval="1d", group_by="ticker",
                                auto_adjust=False, progress=False, threads=True)
            if frame is not None and not frame.empty:
                break
        except Exception as e:
            print(f"הורדה מרוכזת נכשלה (ניסיון {attempt + 1}): {e}")
        time.sleep(5 * (attempt + 1))

    quotes: dict[str, Quote] = {}
    for sym in symbols:
        q = None
        try:
            if frame is not None and sym in frame.columns.get_level_values(0):
                q = _quote_from_frame(sym, frame[sym])
        except Exception:
            q = None
        if q is None:
            print(f"  {sym}: מנסים שוב בנפרד...")
            q = _single_quote(sym)
        if q is None:
            print(f"  ⚠️ {sym}: אין נתונים – לא יוצג.")
            continue
        quotes[sym] = q
    return quotes


def drop_stale(quotes: dict[str, Quote], today_ny: dt.date) -> None:
    """מניה שאין לה עדיין נתון של היום – לא מציגים לה שינוי (כדי לא להציג את אתמול)."""
    for sym, q in quotes.items():
        if sym in NON_US_SESSION:
            continue
        if q.last_date != today_ny:
            print(f"  ⚠️ {sym}: הנתון האחרון מ-{q.last_date} ולא מהיום – השינוי לא יוצג.")
            q.prev = None


# ---------------------------------------------------------------------
#  כותרת חדשות מ-Yahoo Finance
# ---------------------------------------------------------------------

def _parse_news_item(item: dict) -> Headline | None:
    c = item.get("content") or item
    title = (c.get("title") or "").strip()
    if not title or c.get("contentType", "STORY") not in ("STORY",):
        return None
    published = None
    if c.get("pubDate"):
        try:
            published = dt.datetime.fromisoformat(c["pubDate"].replace("Z", "+00:00"))
        except ValueError:
            pass
    elif item.get("providerPublishTime"):
        published = dt.datetime.fromtimestamp(item["providerPublishTime"], dt.timezone.utc)
    provider = (c.get("provider") or {}).get("displayName") or item.get("publisher") or "Yahoo Finance"
    url = ((c.get("canonicalUrl") or {}).get("url")
           or (c.get("clickThroughUrl") or {}).get("url")
           or item.get("link"))
    return Headline(title, provider, url, published)


def _score(h: Headline, now: dt.datetime, movers: list[str], from_market_feed: bool) -> float:
    t = h.title.lower()
    age_h = (now - h.published).total_seconds() / 3600
    score = 2.0 * max(0.0, 1 - age_h / NEWS_MAX_AGE_HOURS)          # טרי יותר = טוב יותר
    if any(w in t for w in MARKET_WORDS):
        score += 1.5                                                  # כותרת על השוק כולו
    if "stock market today" in t:
        score += 1.0
    for sym in movers:                                                # מניה שזזה חזק היום
        for name in COMPANY_NAMES.get(sym, []) + [sym.lower()]:
            if re.search(rf"\b{re.escape(name)}\b", t):
                score += 1.0
                break
    if from_market_feed:
        score += 0.5
    if re.search(r"\b(to buy|should you|\d+ stocks?)\b", t) or t.endswith("?"):
        score -= 1.5                                                  # כותרות "קליקבייט"
    return score


def fetch_headline(movers: list[str]) -> Headline | None:
    import yfinance as yf
    now = dt.datetime.now(dt.timezone.utc)
    best: tuple[float, Headline] | None = None
    seen = set()
    for sym in NEWS_SOURCES + movers[:2]:
        try:
            items = yf.Ticker(sym).get_news(count=20, tab="news") or []
        except Exception as e:
            print(f"  חדשות {sym}: נכשל ({e})")
            continue
        for item in items:
            h = _parse_news_item(item)
            if not h or not h.published or h.title.lower() in seen:
                continue
            seen.add(h.title.lower())
            if (now - h.published) > dt.timedelta(hours=NEWS_MAX_AGE_HOURS):
                continue
            s = _score(h, now, movers, sym in NEWS_SOURCES)
            if best is None or s > best[0]:
                best = (s, h)
    if best:
        print(f"כותרת נבחרה ({best[0]:.2f}): {best[1].title}")
        return best[1]
    print("לא נמצאה כותרת עדכנית – משתמשים במשפט אוטומטי.")
    return None


# ---------------------------------------------------------------------
#  בניית התמונה
# ---------------------------------------------------------------------

def cls(p: float | None, invert: bool = False) -> str:
    if p is None or abs(p) < 0.005:
        return "flat"
    up = p > 0
    return ("down" if up else "up") if invert else ("up" if up else "down")


def fmt_pct(p: float | None) -> str:
    return "—" if p is None else f"{p:+.2f}%"


def fmt_value(v: float, kind: str) -> str:
    if kind == "usd":
        return f"${v:,.0f}"
    if kind == "yield":
        return f"{v:.2f}%"
    if kind == "index" and v >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def num(text: str) -> str:
    return f'<span class="num">{html.escape(text)}</span>'


def mover_rows(items: list[tuple[str, float]]) -> str:
    rows = []
    for sym, p in items:
        tag = WATCHLIST.get(sym, "")
        tag_html = f'<span class="tag">{html.escape(tag)}</span>' if tag else ""
        rows.append(
            f'<div class="row"><div><span class="sym">{sym}</span>{tag_html}</div>'
            f'<div class="pct {cls(p)}">{fmt_pct(p)}</div></div>'
        )
    return "\n".join(rows)


def build_title(ranked: list[tuple[str, float]], spx_pct: float | None) -> str:
    gainers = [s for s, p in ranked if p > 0][:2]
    losers = [s for s, p in reversed(ranked) if p < 0][:2]
    market_up = spx_pct is None or spx_pct >= 0
    if market_up and gainers:
        names, word, c = gainers, "העליות", "up"
    elif losers:
        names, word, c = losers, "הירידות", "down"
    elif gainers:
        names, word, c = gainers, "העליות", "up"
    else:
        return "השוק נסחר ללא כיוון ברור"
    verb = "מובילה" if len(names) == 1 else "מובילות"
    return f'{" ו-".join(names)} {verb} את <span class="{c}">{word}</span>'


def fallback_story(spx: Quote | None, qqq: Quote | None) -> str:
    """משפט אוטומטי בעברית – רק אם אין כותרת חדשות עדכנית."""
    if not spx or spx.pct is None:
        return "וול סטריט פתחה את יום המסחר."
    p = spx.pct
    mood = "בעליות" if p > FLAT_BAND else "בירידות" if p < -FLAT_BAND else "ללא כיוון ברור"
    verb = lambda x: "עולה" if x >= 0 else "יורד"
    s = f"וול סטריט נסחרת {mood}: ה-S&amp;P 500 {verb(p)} ב-{num(f'{abs(p):.2f}%')}"
    if qqq and qqq.pct is not None:
        s += f' והנאסד"ק {verb(qqq.pct)} ב-{num(f"{abs(qqq.pct):.2f}%")}'
    return s + "."


def vix_banner(vix: Quote | None) -> str:
    if not vix or vix.price is None:
        return ""
    v = vix.price
    if v > VIX_PANIC:
        return ('<div class="vix panic"><div class="l1" dir="ltr">Vix &gt; 30</div>'
                '<div class="l2">קנייה🛒🛒🛒</div><div class="l3">קנייה גם כשמגעיל</div></div>')
    if v > VIX_FEAR:
        return ('<div class="vix fear"><div class="l1" dir="ltr">Vix &gt; 20 🛒</div>'
                '<div class="l2">סטטיסטיקה לטובתנו 🛒</div></div>')
    return f'<div class="vix calm">רמת VIX ב-{num(f"{v:.2f}")}: השוק רגוע יחסית</div>'


def macro_tiles(quotes: dict[str, Quote]) -> str:
    tiles = []
    for label, sym, kind, tag in MACRO:
        q = quotes.get(sym)
        if not q or q.price is None:
            val, chg, c = "—", "—", "flat"
        else:
            price, prev = q.price, q.prev
            if kind == "yield" and price > 20:          # ליתר ביטחון: אם Yahoo מחזיר פי 10
                price, prev = price / 10, (prev / 10 if prev else prev)
            val = fmt_value(price, kind)
            if kind == "yield":
                bps = None if not prev else (price - prev) * 100
                chg = "—" if bps is None else f"{bps:+.0f} bps"
                c = cls(bps)
            else:
                chg = fmt_pct(q.pct)
                c = cls(q.pct, invert=(sym == "^VIX"))   # VIX יורד = טוב → ירוק
        tag_html = f'<span class="t-tag">{html.escape(tag)}</span>' if tag else ""
        tiles.append(
            f'<div class="tile"><div class="t-label"><span>{html.escape(label)}</span>{tag_html}</div>'
            f'<div class="t-val">{html.escape(val)}</div>'
            f'<div class="t-chg {c}">{html.escape(chg)}</div></div>'
        )
    return "\n".join(tiles)


def time_ago(published: dt.datetime | None) -> str:
    if not published:
        return ""
    mins = int((dt.datetime.now(dt.timezone.utc) - published).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)} min ago"
    return f"{mins // 60}h ago"


def font_css() -> str:
    local = BASE_DIR / "fonts" / "Heebo.ttf"
    if local.exists():
        import base64
        b64 = base64.b64encode(local.read_bytes()).decode()
        return ("<style>@font-face{font-family:'Heebo';font-weight:100 900;"
                f"src:url(data:font/ttf;base64,{b64}) format('truetype');}}</style>")
    return ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
            'family=Heebo:wght@400;500;700;800;900&display=swap">')


def build_html(quotes: dict[str, Quote], headline: Headline | None) -> str:
    ranked = sorted(
        [(s, quotes[s].pct) for s in WATCHLIST if s in quotes and quotes[s].pct is not None],
        key=lambda x: x[1], reverse=True,
    )
    top = ranked[:TOP_N]
    bottom = sorted(ranked, key=lambda x: x[1])[:TOP_N]
    ups = sum(1 for _, p in ranked if p > 0)

    spx = quotes.get("^GSPC")
    spx_pct = spx.pct if spx else None
    mood = ("flat" if spx_pct is None or abs(spx_pct) < FLAT_BAND
            else "up" if spx_pct > 0 else "down")

    if headline:
        story_dir = "ltr" if not re.search(r"[\u0590-\u05FF]", headline.title) else "rtl"
        meta = " · ".join(x for x in [headline.provider, time_ago(headline.published)] if x)
        story_meta = f'<div class="story-meta">{html.escape(meta)}</div>'
        story = html.escape(headline.title)
    else:
        story_dir, story_meta = "rtl", ""
        story = fallback_story(spx, quotes.get("QQQ"))

    all_pos = top and all(p >= 0 for _, p in top)
    all_neg = bottom and all(p < 0 for _, p in bottom)

    tpl = Template((BASE_DIR / "template.html").read_text(encoding="utf-8"))
    return tpl.safe_substitute(
        font_css=font_css(),
        mood=mood,
        up_count=f"{ups}/{len(ranked)}",
        title_html=build_title(ranked, spx_pct),
        story_dir=story_dir,
        story_meta_html=story_meta,
        story_html=story,
        gainers_title="העולות המובילות" if all_pos else "החזקות ברשימה",
        losers_title="היורדות המובילות" if all_neg else "החלשות ברשימה",
        gainers_rows=mover_rows(top),
        losers_rows=mover_rows(bottom),
        vix_html=vix_banner(quotes.get("^VIX")),
        tiles_html=macro_tiles(quotes),
    )


def render_png(html_str: str, out_path: Path) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 980, "height": 1300}, device_scale_factor=2)
        try:
            page.set_content(html_str, wait_until="networkidle", timeout=30000)
        except Exception:
            page.set_content(html_str, wait_until="load")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        page.locator("#card").screenshot(path=str(out_path))
        browser.close()
    print(f"התמונה נשמרה: {out_path}")


# ---------------------------------------------------------------------
#  שליחה לדיסקורד
# ---------------------------------------------------------------------

def send_to_discord(png: Path, headline: Headline | None) -> None:
    import requests
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        sys.exit("❌ חסר DISCORD_WEBHOOK_URL (צריך להגדיר אותו ב-GitHub Secrets).")
    payload = {}
    if SEND_NEWS_LINK and headline and headline.url:
        payload = {"content": f"📰 [לכתבה המלאה ב-Yahoo Finance]({headline.url})",
                   "flags": 4}   # 4 = בלי תצוגה מקדימה של הקישור
    for attempt in range(4):
        with open(png, "rb") as f:
            r = requests.post(
                url,
                data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                files={"files[0]": ("market_summary.png", f, "image/png")},
                timeout=60,
            )
        if r.ok:
            print("✅ נשלח לדיסקורד.")
            return
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 2))
            print(f"דיסקורד ביקש להמתין {wait} שניות...")
            time.sleep(wait + 0.5)
            continue
        print(f"שגיאה מדיסקורד {r.status_code}: {r.text[:300]}")
        time.sleep(3)
    sys.exit("❌ השליחה לדיסקורד נכשלה.")


# ---------------------------------------------------------------------
#  נתוני דוגמה (לתצוגה מקדימה בלבד)
# ---------------------------------------------------------------------

DEMO = {
    "calm": {
        "watch": {"SPCX": 7.35, "TSLA": 4.65, "AVGO": 3.35, "AMD": 2.95, "SOXX": 2.18,
                  "NVDA": 1.92, "WGMI": 1.74, "MAGS": 1.21, "AMZN": 1.05, "GOOGL": 0.88,
                  "AAPL": 0.64, "MSFT": 0.47, "META": 0.30, "IGV": 0.18, "GEV": 0.13,
                  "PLTR": -0.68, "NOW": -2.45},
        "macro": {"^GSPC": (7723, 0.73), "QQQ": (749.58, 1.02), "^VIX": (15.31, -6.59),
                  "^RUT": (2833, 0.94), "RSP": (209.73, 0.35), "BTC-USD": (84497, -0.42),
                  "CL=F": (91.11, -1.90), "^TNX": (5.28, None)},
        "tnx_prev": 5.24,
        "headline": "Demo headline: stocks climb as chipmakers rally and Tesla jumps",
    },
    "fear": {
        "watch": {"GEV": 1.12, "WGMI": 0.42, "AAPL": -0.31, "MSFT": -0.58, "GOOGL": -0.74,
                  "META": -0.96, "AMZN": -1.10, "MAGS": -1.24, "IGV": -1.40, "NOW": -1.66,
                  "AVGO": -1.83, "SOXX": -2.05, "NVDA": -2.31, "TSLA": -2.64, "AMD": -2.92,
                  "PLTR": -3.48, "SPCX": -4.15},
        "macro": {"^GSPC": (7512, -1.38), "QQQ": (728.40, -1.71), "^VIX": (23.42, 18.6),
                  "^RUT": (2761, -1.52), "RSP": (205.10, -1.05), "BTC-USD": (81230, -2.80),
                  "CL=F": (93.40, 1.20), "^TNX": (5.36, None)},
        "tnx_prev": 5.29,
        "headline": "Demo headline: stocks slide as bond yields jump and tech sells off",
    },
    "panic": {
        "watch": {"GEV": -1.20, "AAPL": -2.10, "MSFT": -2.35, "WGMI": -2.60, "GOOGL": -2.88,
                  "META": -3.12, "AMZN": -3.30, "MAGS": -3.55, "IGV": -3.70, "NOW": -4.02,
                  "AVGO": -4.40, "SOXX": -4.85, "NVDA": -5.12, "TSLA": -5.60, "AMD": -5.95,
                  "PLTR": -6.70, "SPCX": -8.10},
        "macro": {"^GSPC": (7290, -3.10), "QQQ": (701.25, -3.65), "^VIX": (34.80, 41.2),
                  "^RUT": (2650, -3.40), "RSP": (199.80, -2.75), "BTC-USD": (76410, -5.10),
                  "CL=F": (87.60, -3.90), "^TNX": (5.12, None)},
        "tnx_prev": 5.21,
        "headline": "Demo headline: stocks plunge as volatility spikes across markets",
    },
}


def demo_data(name: str) -> tuple[dict[str, Quote], Headline]:
    d = DEMO[name]
    quotes: dict[str, Quote] = {}
    base_prices = {"NVDA": 190, "PLTR": 180, "IGV": 110, "GEV": 640, "MAGS": 62, "SOXX": 290,
                   "AAPL": 250, "GOOGL": 240, "NOW": 980, "AMZN": 230, "TSLA": 440,
                   "META": 760, "MSFT": 520, "AVGO": 350, "AMD": 210, "SPCX": 120, "WGMI": 40}
    for sym, p in d["watch"].items():
        price = base_prices[sym]
        quotes[sym] = Quote(sym, price, price / (1 + p / 100))
    for sym, (price, p) in d["macro"].items():
        prev = d["tnx_prev"] if sym == "^TNX" else price / (1 + p / 100)
        quotes[sym] = Quote(sym, price, prev)
    pub = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=35)
    return quotes, Headline(d["headline"], "Demo", None, pub)


# ---------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true", help="רק לבדוק אם ההרצה המתוזמנת רלוונטית")
    ap.add_argument("--no-send", action="store_true", help="לשמור תמונה בלי לשלוח לדיסקורד")
    ap.add_argument("--demo", choices=list(DEMO), help="נתוני דוגמה לתצוגה מקדימה")
    ap.add_argument("--out", default="market_summary.png")
    args = ap.parse_args()

    if args.gate:
        ok = gate()
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"run={'true' if ok else 'false'}\n")
        return

    out = Path(args.out)
    if args.demo:
        quotes, headline = demo_data(args.demo)
    else:
        scheduled = bool(os.environ.get("TRIGGER_SCHEDULE", "").strip())
        if scheduled and not wait_until_send_time():
            return

        symbols = list(WATCHLIST) + [m[1] for m in MACRO]
        print("מושכים נתונים מ-Yahoo Finance...")
        quotes = fetch_quotes(symbols)
        if not quotes:
            sys.exit("❌ לא התקבלו נתונים מ-Yahoo Finance.")

        today_ny = dt.datetime.now(NY).date()
        ref = quotes.get("^GSPC") or quotes.get("QQQ")
        market_open_today = bool(ref and ref.last_date == today_ny)
        if scheduled:
            if not market_open_today:
                print(f"הבורסה בארה\"ב סגורה היום ({today_ny}) – לא שולחים.")
                return
            drop_stale(quotes, today_ny)

        ranked = sorted([s for s in WATCHLIST if s in quotes and quotes[s].pct is not None],
                        key=lambda s: abs(quotes[s].pct), reverse=True)
        print("מחפשים את כותרת היום...")
        headline = fetch_headline(ranked[:3])

    render_png(build_html(quotes, headline), out)
    if not args.no_send:
        send_to_discord(out, headline)


if __name__ == "__main__":
    main()
