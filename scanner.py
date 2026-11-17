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
SCAN_MODE = os.getenv("SCAN_MODE", "cross").lower()      # cross | above
MAX_STOCKS = int(os.getenv("MAX_STOCKS", "40"))           # cap per run (screener rate limit)
FORCE = os.getenv("FORCE_RUN", "0") == "1"                # ignore market-hours check

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
            if len(df) < SMA_DAYS + 2:
                continue
            # last row must be today's session (skip holidays / stale data)
            if df.index[-1].date() != datetime.now(IST).date() and not FORCE:
                continue
            close = df["Close"]
            sma = close.rolling(SMA_DAYS).mean()
            price, sma_now = float(close.iloc[-1]), float(sma.iloc[-1])
            prev_close, sma_prev = float(close.iloc[-2]), float(sma.iloc[-2])
            above = price > sma_now
            crossed = above and prev_close <= sma_prev
            if (SCAN_MODE == "cross" and crossed) or (SCAN_MODE == "above" and above):
                matches.append({
                    "symbol": tk.replace(".NS", ""),
                    "price": price,
                    "sma": sma_now,
                    "above_pct": (price / sma_now - 1) * 100,
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
    m.update(pe=None, ind_pe=None, fii=None, promoter=None, qoq=None, yoy=None)
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


# ---------------------------------------------------------------- telegram
def card(m: dict) -> str:
    sym = html.escape(m["symbol"])
    scr = f"https://www.screener.in/company/{m['symbol']}/consolidated/"
    tv = f"https://in.tradingview.com/chart/?symbol=NSE:{m['symbol']}"
    return (
        f"<b>{sym}</b>  ₹{m['price']:,.2f}  (19 SMA {m['sma']:,.2f}, +{m['above_pct']:.1f}% above)\n"
        f"P/E {fmt(m['pe'])} | Industry P/E {fmt(m['ind_pe'])}\n"
        f"FII {fmt(m['fii'], '%')} | Promoter {fmt(m['promoter'], '%')}\n"
        f"Sales QoQ {fmt(m['qoq'], '%')} | Sales YoY {fmt(m['yoy'], '%')}\n"
        f"Return 1M {fmt(m['ret1m'], '%')} | 3M {fmt(m['ret3m'], '%')}\n"
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
    print(f"{len(matches)} matches")
    if not matches:
        tg_send(f"📊 {now} IST\nNo stocks {label} 19-day SMA right now.")
        return
    shown = matches[:MAX_STOCKS]
    sess = requests.Session()
    sess.headers.update(UA)
    for m in shown:
        price_stats(m)
        screener_data(m, sess)
        time.sleep(1.5)
    header = (f"📈 <b>{now} IST</b> - {len(matches)} stocks {label} 19-day SMA"
              + (f" (top {len(shown)} shown)" if len(matches) > len(shown) else ""))
    send_all(header, [card(m) for m in shown])


if __name__ == "__main__":
    main()
