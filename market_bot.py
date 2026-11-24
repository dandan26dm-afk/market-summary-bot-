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
import os
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
CRON_SUMMER = "20 13 * * 1-5"   # שעון קיץ בישראל (UTC+3) → מתעורר ב-16:20
CRON_WINTER = "20 14 * * 1-5"   # שעון חורף בישראל (UTC+2) → מתעורר ב-16:20

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

# תעודות הסל של הסקטורים (SPDR): טיקר → שם בעברית
# מוצגות בחלק העליון, ממוינות מהחזקה לחלשה
SECTORS = {
    "XLK": "טכנולוגיה",
    "XLC": "תקשורת",
    "XLY": "צריכה מחזורית",
    "XLF": "פיננסים",
    "XLV": "בריאות",
    "XLI": "תעשייה",
    "XLE": "אנרגיה",
    "XLB": "חומרים",
    "XLP": "צריכה בסיסית",
    "XLU": "תשתיות",
    "XLRE": 'נדל"ן',
}

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
VIX_FEAR = 20           # רמה מעל 20 → "סטטיסטיקה לטובתנו"
VIX_PANIC = 30          # רמה מעל 30 → "קנייה גם כשמגעיל"
VIX_SPIKE_PCT = 30      # קפיצה יומית של 30% ומעלה → "סימן קנייה" (גובר על השאר)

# מתחת לשינוי הזה (באחוזים, לכל כיוון) ה-S&P נחשב "ללא שינוי" → רקע אפור-כחלחל
FLAT_BAND = 0.20

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


def env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def in_send_window() -> bool:
    """ה-workflow מתעורר כמה פעמים. שולחים רק מ-17:00 שעון ישראל ועד 3 שעות אחרי."""
    now = dt.datetime.now(IL)
    target = now.replace(hour=SEND_HOUR_IL, minute=SEND_MINUTE_IL, second=0, microsecond=0)
    if now < target - dt.timedelta(minutes=3):
        print(f"עכשיו {now:%H:%M} שעון ישראל – מוקדם מדי, מדלגים (ההרצה של 17:00 תשלח).")
        return False
    late_h = (now - target).total_seconds() / 3600
    if late_h > MAX_LATE_HOURS:
        print(f"GitHub איחר ב-{late_h:.1f} שעות – לא שולחים היום.")
        return False
    if late_h > 0.02:
        print(f"GitHub איחר ב-{late_h * 60:.0f} דקות – שולחים עכשיו.")
    return True


def us_market_is_trading() -> bool:
    """בודק לפי שעון ניו יורק שהמסחר כבר נפתח (09:30) ולא נסגר.
    לפני הפתיחה Yahoo מחזיר נתונים חלקיים ושגויים, ולכן לא שולחים."""
    now = dt.datetime.now(NY)
    if now.weekday() >= 5:
        print("סוף שבוע בארה\"ב – לא שולחים.")
        return False
    open_t = now.replace(hour=9, minute=35, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=15, second=0, microsecond=0)
    if not (open_t <= now <= close_t):
        print(f"עכשיו {now:%H:%M} בניו יורק – המסחר לא פתוח (09:30–16:00). לא שולחים.")
        return False
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


def sector_tiles(quotes: dict[str, Quote]) -> tuple[str, str]:
    """אריחי הסקטורים, ממוינים מהחזק לחלש. רקע האריח מתחזק ככל שהתנועה גדולה יותר."""
    items = [(s, quotes[s].pct) for s in SECTORS if s in quotes]
    items.sort(key=lambda x: (x[1] is None, -(x[1] or 0)))
    tiles = []
    for sym, p in items:
        if p is None:
            bg = "rgba(255,255,255,.03)"
        else:
            alpha = 0.06 + 0.32 * min(abs(p) / 3.0, 1.0)     # 3% ומעלה = הכי כהה
            rgb = "74,222,128" if p > 0 else "242,100,100"
            bg = f"rgba({rgb},{alpha:.2f})"
        tiles.append(
            f'<div class="sec" style="background:{bg}">'
            f'<div class="sec-sym">{sym}</div>'
            f'<div class="sec-name">{html.escape(SECTORS[sym])}</div>'
            f'<div class="sec-pct {cls(p)}">{fmt_pct(p)}</div></div>'
        )
    known = [p for _, p in items if p is not None]
    ups = sum(1 for p in known if p > 0)
    summary = f"עולים: {num(f'{ups}/{len(known)}')}" if known else ""
    return "\n".join(tiles), summary


def vix_banner(vix: Quote | None) -> str:
    if not vix or vix.price is None:
        return ""
    v, chg = vix.price, vix.pct

    # קפיצה חדה ביום אחד – סימן קנייה, גובר על כל השאר
    if chg is not None and chg >= VIX_SPIKE_PCT:
        if v > VIX_PANIC:
            extra = "Vix &gt; 30 · קנייה גם כשמגעיל"
        elif v > VIX_FEAR:
            extra = "Vix &gt; 20 · סטטיסטיקה לטובתנו"
        else:
            extra = f"רמת VIX ב-{num(f'{v:.2f}')}"
        return ('<div class="vix spike">'
                '<div class="l1">✅ סימן קנייה</div>'
                f'<div class="l2">ה-VIX זינק היום ב-{num(f"+{chg:.1f}%")} 🛒</div>'
                f'<div class="l3">{extra}</div></div>')

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


def font_css() -> str:
    local = BASE_DIR / "fonts" / "Heebo.ttf"
    if local.exists():
        import base64
        b64 = base64.b64encode(local.read_bytes()).decode()
        return ("<style>@font-face{font-family:'Heebo';font-weight:100 900;"
                f"src:url(data:font/ttf;base64,{b64}) format('truetype');}}</style>")
    return ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
            'family=Heebo:wght@400;500;700;800;900&display=swap">')


def build_html(quotes: dict[str, Quote]) -> str:
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

    all_pos = top and all(p >= 0 for _, p in top)
    all_neg = bottom and all(p < 0 for _, p in bottom)
    sectors_html, sectors_summary = sector_tiles(quotes)

    tpl_path = next((c for c in (BASE_DIR / "templates" / "digest",
                                 BASE_DIR / "templates" / "digest.html",
                                 BASE_DIR / "template.html") if c.is_file()), None)
    if tpl_path is None:
        sys.exit("❌ לא נמצא קובץ העיצוב (templates/digest).")
    tpl = Template(tpl_path.read_text(encoding="utf-8"))
    return tpl.safe_substitute(
        font_css=font_css(),
        mood=mood,
        up_count=f"{ups}/{len(ranked)}",
        title_html=build_title(ranked, spx_pct),
        sectors_html=sectors_html,
        sectors_summary=sectors_summary,
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

def send_to_discord(png: Path) -> None:
    import requests
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        sys.exit("❌ חסר DISCORD_WEBHOOK_URL (צריך להגדיר אותו ב-GitHub Secrets).")
    for attempt in range(4):
        with open(png, "rb") as f:
            r = requests.post(
                url,
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
        "sectors": {"XLK": 1.42, "XLC": 0.95, "XLY": 1.18, "XLF": 0.61, "XLV": -0.22,
                    "XLI": 0.48, "XLE": -0.87, "XLB": 0.31, "XLP": -0.35, "XLU": -0.51,
                    "XLRE": 0.12},
        "macro": {"^GSPC": (7723, 0.73), "QQQ": (749.58, 1.02), "^VIX": (15.31, -6.59),
                  "^RUT": (2833, 0.94), "RSP": (209.73, 0.35), "BTC-USD": (84497, -0.42),
                  "CL=F": (91.11, -1.90), "^TNX": (5.28, None)},
        "tnx_prev": 5.24,
    },
    "fear": {
        "watch": {"GEV": 1.12, "WGMI": 0.42, "AAPL": -0.31, "MSFT": -0.58, "GOOGL": -0.74,
                  "META": -0.96, "AMZN": -1.10, "MAGS": -1.24, "IGV": -1.40, "NOW": -1.66,
                  "AVGO": -1.83, "SOXX": -2.05, "NVDA": -2.31, "TSLA": -2.64, "AMD": -2.92,
                  "PLTR": -3.48, "SPCX": -4.15},
        "sectors": {"XLK": -2.10, "XLC": -1.55, "XLY": -1.92, "XLF": -1.08, "XLV": 0.24,
                    "XLI": -1.21, "XLE": 0.86, "XLB": -0.95, "XLP": 0.41, "XLU": 0.63,
                    "XLRE": -1.44},
        "macro": {"^GSPC": (7512, -1.38), "QQQ": (728.40, -1.71), "^VIX": (23.42, 18.6),
                  "^RUT": (2761, -1.52), "RSP": (205.10, -1.05), "BTC-USD": (81230, -2.80),
                  "CL=F": (93.40, 1.20), "^TNX": (5.36, None)},
        "tnx_prev": 5.29,
    },
    "panic": {
        "watch": {"GEV": -1.20, "AAPL": -2.10, "MSFT": -2.35, "WGMI": -2.60, "GOOGL": -2.88,
                  "META": -3.12, "AMZN": -3.30, "MAGS": -3.55, "IGV": -3.70, "NOW": -4.02,
                  "AVGO": -4.40, "SOXX": -4.85, "NVDA": -5.12, "TSLA": -5.60, "AMD": -5.95,
                  "PLTR": -6.70, "SPCX": -8.10},
        "sectors": {"XLK": -4.35, "XLC": -3.60, "XLY": -3.95, "XLF": -3.12, "XLV": -1.45,
                    "XLI": -2.88, "XLE": -2.20, "XLB": -2.65, "XLP": -0.92, "XLU": -0.74,
                    "XLRE": -2.31},
        "macro": {"^GSPC": (7290, -3.10), "QQQ": (701.25, -3.65), "^VIX": (34.80, 41.2),
                  "^RUT": (2650, -3.40), "RSP": (199.80, -2.75), "BTC-USD": (76410, -5.10),
                  "CL=F": (87.60, -3.90), "^TNX": (5.12, None)},
        "tnx_prev": 5.21,
    },
}


def demo_data(name: str) -> dict[str, Quote]:
    d = DEMO[name]
    quotes: dict[str, Quote] = {}
    for sym, p in {**d["watch"], **d["sectors"]}.items():
        price = 100.0
        quotes[sym] = Quote(sym, price, price / (1 + p / 100))
    for sym, (price, p) in d["macro"].items():
        prev = d["tnx_prev"] if sym == "^TNX" else price / (1 + p / 100)
        quotes[sym] = Quote(sym, price, prev)
    return quotes


# ---------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true", help="רק לבדוק אם ההרצה המתוזמנת רלוונטית")
    ap.add_argument("--no-send", action="store_true", help="לשמור תמונה בלי לשלוח לדיסקורד")
    ap.add_argument("--demo", choices=list(DEMO), help="נתוני דוגמה לתצוגה מקדימה")
    ap.add_argument("--out", default="out/market_digest.png")
    args = ap.parse_args()

    if args.gate:
        ok = gate()
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"run={'true' if ok else 'false'}\n")
        return

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    no_send = args.no_send or env_true("NO_SEND")
    if args.demo:
        quotes = demo_data(args.demo)
    else:
        # הרצה רגילה (מתוזמנת או טריגר חיצוני): בודקים שעה וחג.
        # הרצה ידנית עם "להריץ גם מחוץ לשעות" (FORCE) מדלגת על הבדיקות.
        scheduled = not env_true("FORCE")
        if scheduled and not in_send_window():
            return
        if scheduled and not us_market_is_trading():
            return

        symbols = list(WATCHLIST) + list(SECTORS) + [m[1] for m in MACRO]
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
            fresh = [s for s in WATCHLIST if s in quotes and quotes[s].pct is not None]
            if len(fresh) < len(WATCHLIST) // 2:
                print(f"רק {len(fresh)}/{len(WATCHLIST)} מניות עם נתונים של היום – "
                      "לא שולחים תמונה חלקית. ההרצה הבאה תנסה שוב.")
                return

    render_png(build_html(quotes), out)
    if not no_send:
        send_to_discord(out)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("sent=true\n")


if __name__ == "__main__":
    main()
