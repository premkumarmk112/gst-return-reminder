"""
19-day SMA scanner -> Telegram
Runs every hour (GitHub Actions) during NSE market hours.

Logic:
  - Universe: Nifty 500 (or symbols.txt if present)
  - SCAN_MODE=cross (default): stocks that closed <= 19 SMA yesterday and are trading ABOVE 19 SMA today
  - SCAN_MODE=above          : all stocks currently trading above 19 SMA
  - For each match: P/E, Industry P/E, FII %, Promoter %, QoQ sales, YoY sales,
    1M / 3M return, 52W high/low, All-time high, + Screener & TradingView links
"""
import html
import io
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

IST = timezone(timedelta(hours=5, minutes=30))
SMA_DAYS = 19
SMA2_DAYS = int(os.getenv("SMA2_DAYS", "0"))              # >0: price must ALSO be above this SMA (e.g. 21)
SMA_LABEL = f"{SMA_DAYS}-day" + (f" and {SMA2_DAYS}-day" if SMA2_DAYS else "")
SCAN_MODE = os.getenv("SCAN_MODE", "cross").lower()      # cross | above
MAX_STOCKS = int(os.getenv("MAX_STOCKS", "40"))           # max candidates checked on screener per run
SCANNER_NAME = os.getenv("SCANNER_NAME", "")              # label shown in the Telegram header
FORCE = os.getenv("FORCE_RUN", "0") == "1"                # ignore market-hours check

# ---- fundamental filters: all OFF by default. Set env vars to turn them on.
# (when a filter is on, a stock with missing data is rejected)
MAX_PE = float(os.getenv("MAX_PE", "0"))                  # >0: P/E must be below this
MIN_PROMOTER = float(os.getenv("MIN_PROMOTER", "0"))      # >0: promoter holding % must be above this
NEED_SALES_GROWTH = os.getenv("NEED_SALES_GROWTH", "0") == "1"  # 1: QoQ and YoY sales both > 0
NEED_EPS_GROWTH = os.getenv("NEED_EPS_GROWTH", "0") == "1"      # 1: EPS up vs last quarter AND vs year-ago quarter
FILTERS_ON = MAX_PE > 0 or MIN_PROMOTER > 0 or NEED_SALES_GROWTH or NEED_EPS_GROWTH

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
NIFTY500_URLS = [
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
]


# ---------------------------------------------------------------- helpers
def market_open_now() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 10 <= t <= 15 * 60 + 40


def load_universe() -> list[str]:
    if os.path.exists("symbols.txt"):
        syms = [s.strip().upper() for s in open("symbols.txt") if s.strip() and not s.startswith("#")]
        if syms:
            print(f"Universe: symbols.txt ({len(syms)})")
            return syms
    for url in NIFTY500_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            syms = df["Symbol"].astype(str).str.strip().tolist()
            print(f"Universe: Nifty 500 ({len(syms)})")
            return syms
        except Exception as e:  # noqa: BLE001
            print("Universe fetch failed:", url, e)
    sys.exit("Could not load stock universe. Add a symbols.txt file (one NSE symbol per line).")


def to_float(txt):
    if txt is None:
        return None
    txt = re.sub(r"[^\d.\-]", "", str(txt).replace(",", ""))
    try:
        return float(txt)
    except ValueError:
        return None


def fmt(v, suffix="", nd=1):
    return "N/A" if v is None else f"{v:,.{nd}f}{suffix}"


def mark(v):
    """Green up / red down indicator."""
    if v is None:
        return "⚪"
    return "🟢▲" if v > 0 else ("🔴▼" if v < 0 else "⚪")


def sgn(v, nd=1):
    return "N/A" if v is None else f"{v:+.{nd}f}%"


def pct_change(new, old):
    if new is None or old in (None, 0):
        return None
    return (new / old - 1) * 100


# ---------------------------------------------------------------- scan
def find_matches(symbols: list[str]) -> list[dict]:
    tickers = [s + ".NS" for s in symbols]
    matches = []
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        data = yf.download(batch, period="3mo", interval="1d", group_by="ticker",
                           auto_adjust=True, threads=True, progress=False)
        for tk in batch:
            try:
                df = data[tk].dropna(subset=["Close"])
            except Exception:  # noqa: BLE001
                continue
            if len(df) < max(SMA_DAYS, SMA2_DAYS) + 2:
                continue
            # last row must be today's session (skip holidays / stale data)
            if df.index[-1].date() != datetime.now(IST).date() and not FORCE:
                continue
            close = df["Close"]
            sma = close.rolling(SMA_DAYS).mean()
            price, sma_now = float(close.iloc[-1]), float(sma.iloc[-1])
            prev_close, sma_prev = float(close.iloc[-2]), float(sma.iloc[-2])
            above = price > sma_now
            was_above = prev_close > sma_prev
            sma2_now = None
            if SMA2_DAYS:
                sma2 = close.rolling(SMA2_DAYS).mean()
                if sma2.iloc[-2:].isna().any():
                    continue
                sma2_now, sma2_prev = float(sma2.iloc[-1]), float(sma2.iloc[-2])
                above = above and price > sma2_now
                was_above = was_above and prev_close > sma2_prev
            crossed = above and not was_above
            if (SCAN_MODE == "cross" and crossed) or (SCAN_MODE == "above" and above):
                matches.append({
                    "symbol": tk.replace(".NS", ""),
                    "price": price,
                    "sma": sma_now,
                    "sma2": sma2_now,
                    "above_pct": (price / sma_now - 1) * 100,
                    "day_pct": (price / prev_close - 1) * 100,
                })
    matches.sort(key=lambda m: m["above_pct"], reverse=True)
    return matches


def price_stats(m: dict):
    """1M / 3M return, 52W high/low, all-time high (from full history)."""
    try:
        h = yf.Ticker(m["symbol"] + ".NS").history(period="max", auto_adjust=True)
        h = h.dropna(subset=["Close"])
        price = m["price"]
        last_date = h.index[-1]

        def ret(days):
            past = h[h.index <= last_date - pd.Timedelta(days=days)]
            return pct_change(price, float(past["Close"].iloc[-1])) if len(past) else None

        yr = h[h.index > last_date - pd.Timedelta(days=365)]
        m["ret1m"], m["ret3m"] = ret(30), ret(91)
        m["hi52"], m["lo52"] = float(yr["High"].max()), float(yr["Low"].min())
        m["ath"] = float(h["High"].max())
    except Exception as e:  # noqa: BLE001
        print("price_stats failed", m["symbol"], e)
        m.update(ret1m=None, ret3m=None, hi52=None, lo52=None, ath=None)


# ---------------------------------------------------------------- screener
def table_rows(section):
    out = {}
    if not section:
        return out
    for tr in section.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        name = tds[0].get_text(strip=True).replace("+", "").strip()
        out[name] = [to_float(td.get_text()) for td in tds[1:]]
    return out


def screener_data(m: dict, sess: requests.Session):
    sym = m["symbol"]
    m.update(pe=None, ind_pe=None, fii=None, promoter=None, qoq=None, yoy=None,
             eps_cur=None, eps_prev=None, eps_yago=None)
    soup = None
    for path in ("consolidated/", ""):
        try:
            r = sess.get(f"https://www.screener.in/company/{sym}/{path}", timeout=30)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                if soup.select_one("#top-ratios"):
                    break
        except Exception as e:  # noqa: BLE001
            print("screener fetch failed", sym, e)
        time.sleep(1)
    if soup is None or not soup.select_one("#top-ratios"):
        return

    # P/E
    for li in soup.select("#top-ratios li"):
        nm = li.select_one(".name")
        if nm and "Stock P/E" in nm.get_text():
            m["pe"] = to_float(li.select_one(".number").get_text())

    # shareholding (latest quarter = last column)
    shp = table_rows(soup.select_one("#quarterly-shp"))
    for k, v in shp.items():
        if k.startswith("Promoters") and v:
            m["promoter"] = v[-1]
        if k.startswith("FIIs") and v:
            m["fii"] = v[-1]

    # sales growth from quarterly results
    q = table_rows(soup.select_one("#quarters"))
    sales = q.get("Sales") or q.get("Revenue") or next(
        (v for k, v in q.items() if k.startswith(("Sales", "Revenue"))), None)
    if sales:
        sales = [x for x in sales if x is not None]
        if len(sales) >= 2:
            m["qoq"] = pct_change(sales[-1], sales[-2])
        if len(sales) >= 5:
            m["yoy"] = pct_change(sales[-1], sales[-5])

    # EPS: latest quarter, previous quarter, same quarter last year
    eps = next((v for k, v in q.items() if k.startswith("EPS")), None)
    if eps:
        if len(eps) >= 1:
            m["eps_cur"] = eps[-1]
        if len(eps) >= 2:
            m["eps_prev"] = eps[-2]
        if len(eps) >= 5:
            m["eps_yago"] = eps[-5]

    # industry P/E = median P/E of peers
    try:
        ph = soup.select_one("[data-url*='/peers/']")
        if ph:
            time.sleep(1)
            pr = sess.get("https://www.screener.in" + ph["data-url"], timeout=30)
            psoup = BeautifulSoup(pr.text, "html.parser")
            table = psoup.find("table")
            heads = [th.get_text(strip=True) for th in table.find_all("th")]
            idx = next(i for i, h in enumerate(heads) if h.startswith("P/E"))
            rows = table.select("tbody tr")
            pes = []
            for tr in rows:
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) <= idx:
                    continue
                if tds[1].lower().startswith("median"):
                    m["ind_pe"] = to_float(tds[idx])
                elif tds[1] and not tds[1].lower().startswith("median"):
                    v = to_float(tds[idx])
                    if v and v > 0:
                        pes.append(v)
            if m["ind_pe"] is None and pes:
                m["ind_pe"] = statistics.median(pes)
    except Exception as e:  # noqa: BLE001
        print("industry PE failed", sym, e)


def passes_filters(m: dict) -> bool:
    if MAX_PE > 0 and (m.get("pe") is None or not (0 < m["pe"] < MAX_PE)):
        return False
    if MIN_PROMOTER > 0 and (m.get("promoter") is None or m["promoter"] <= MIN_PROMOTER):
        return False
    if NEED_SALES_GROWTH:
        if m.get("qoq") is None or m["qoq"] <= 0:
            return False
        if m.get("yoy") is None or m["yoy"] <= 0:
            return False
    if NEED_EPS_GROWTH:
        c, p, y = m.get("eps_cur"), m.get("eps_prev"), m.get("eps_yago")
        if c is None or p is None or y is None or c <= p or c <= y:
            return False
    return True


# ---------------------------------------------------------------- telegram
def eps_line(m: dict) -> str:
    c, p, y = m.get("eps_cur"), m.get("eps_prev"), m.get("eps_yago")
    up = lambda a, b: mark(a - b) if a is not None and b is not None else "⚪"  # noqa: E731
    return (f"EPS: this Q {fmt(c, nd=2)} | last Q {fmt(p, nd=2)} {up(c, p)}"
            f" | year-ago Q {fmt(y, nd=2)} {up(c, y)}")


def card(m: dict) -> str:
    sym = html.escape(m["symbol"])
    scr = f"https://www.screener.in/company/{m['symbol']}/consolidated/"
    tv = f"https://in.tradingview.com/chart/?symbol=NSE:{m['symbol']}"
    return (
        f"<b>{sym}</b>  ₹{m['price']:,.2f}  {mark(m['day_pct'])} {sgn(m['day_pct'], 2)} today\n"
        f"{SMA_DAYS} SMA {m['sma']:,.2f} (+{m['above_pct']:.1f}% above)"
        + (f" | {SMA2_DAYS} SMA {m['sma2']:,.2f}" if m.get("sma2") else "") + "\n"
        f"P/E {fmt(m['pe'])} | Industry P/E {fmt(m['ind_pe'])}\n"
        f"FII {fmt(m['fii'], '%')} | Promoter {fmt(m['promoter'], '%')}\n"
        f"Sales QoQ {mark(m['qoq'])} {sgn(m['qoq'])} | YoY {mark(m['yoy'])} {sgn(m['yoy'])}\n"
        f"{eps_line(m)}\n"
        f"Return 1M {mark(m['ret1m'])} {sgn(m['ret1m'])} | 3M {mark(m['ret3m'])} {sgn(m['ret3m'])}\n"
        f"52W High {fmt(m['hi52'], nd=2)} | 52W Low {fmt(m['lo52'], nd=2)}\n"
        f"All-time High {fmt(m['ath'], nd=2)}\n"
        f'<a href="{scr}">Screener</a> | <a href="{tv}">TradingView</a>'
    )


def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print(text)
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        print("Telegram error:", r.text)
        # fallback: resend as plain text so a formatting problem never hides the alert
        plain = re.sub(r"<[^>]+>", "", text)
        r2 = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": html.unescape(plain),
                  "disable_web_page_preview": True},
            timeout=30,
        )
        print("Telegram fallback:", r2.status_code)


def send_all(header: str, cards: list[str]):
    msg = header
    for c in cards:
        if len(msg) + len(c) + 2 > 3900:
            tg_send(msg)
            msg = ""
        msg += ("\n\n" if msg else "") + c
    if msg:
        tg_send(msg)


# ---------------------------------------------------------------- main
def main():
    if not FORCE and not market_open_now():
        print("Market closed - skipping.")
        return
    symbols = load_universe()
    matches = find_matches(symbols)
    now = datetime.now(IST).strftime("%d-%b %H:%M")
    label = "crossed above" if SCAN_MODE == "cross" else "trading above"
    print(f"{len(matches)} matches (before fundamental filters)")
    parts = []
    if MAX_PE > 0:
        parts.append(f"P/E below {MAX_PE:g}")
    if MIN_PROMOTER > 0:
        parts.append(f"Promoter above {MIN_PROMOTER:g}%")
    if NEED_SALES_GROWTH:
        parts.append("Sales QoQ and YoY up")
    if NEED_EPS_GROWTH:
        parts.append("EPS up vs last Q and year-ago Q")
    rules = ", ".join(parts) if parts else "no filters"
    rules = html.escape(rules)
    tag = html.escape(f"[{SCANNER_NAME}] " if SCANNER_NAME else "")
    if not matches:
        tg_send(f"📊 {tag}{now} IST\nNo stocks {label} {SMA_LABEL} SMA right now.")
        return
    candidates = matches[:MAX_STOCKS]
    sess = requests.Session()
    sess.headers.update(UA)
    passed = []
    for m in candidates:
        screener_data(m, sess)
        if passes_filters(m):
            price_stats(m)
            passed.append(m)
        time.sleep(1.5)
    if not passed:
        tg_send(f"📊 {tag}{now} IST\n{len(matches)} stocks {label} {SMA_LABEL} SMA, "
                f"but none match your filters ({rules}).")
        return
    header = (f"📈 <b>{tag}{now} IST</b> - {len(passed)} stocks {label} {SMA_LABEL} SMA "
              f"({rules})"
              + (f" - top {len(candidates)} checked" if len(matches) > len(candidates) else ""))
    send_all(header, [card(m) for m in passed])


if __name__ == "__main__":
    main()
