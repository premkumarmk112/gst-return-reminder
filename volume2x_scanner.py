import os, re, io, sys, time, html, csv, statistics
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo
import requests
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup

IST = ZoneInfo("Asia/Kolkata")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
if not TOKEN or not CHAT_IDS:
    sys.exit("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secret is empty")

VOL_MULT = float(os.getenv("VOL_MULT", "2"))          # today's volume vs 1-week avg
TOP_N = int(os.getenv("TOP_N", "10"))                 # stocks shown in the report
MIN_AVG_VOL = float(os.getenv("MIN_AVG_VOL", "50000"))  # ignore illiquid stocks
SLEEP = float(os.getenv("SLEEP", "1.2"))              # delay between Screener requests
MAX_PAGES = int(os.getenv("MAX_PAGES", "8"))          # industry pages to read on Screener
SMA_LIST = [10, 15, 20, 25, 30, 35]

HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
       "Accept": "text/csv,text/html,*/*"}

NIFTY500_URLS = [
    "https://archive.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
]


# ---------------------------------------------------------------- helpers
def num(t):
    """Convert text like '₹ 1,234.5' or '12.3%' to float."""
    if t is None:
        return None
    t = re.sub(r"[₹,%\s]|Cr\.?", "", t)
    try:
        return float(t)
    except ValueError:
        return None


def f(v, nd=1, pre="", suf=""):
    return "NA" if v is None or v != v else f"{pre}{v:,.{nd}f}{suf}"


def sign(v):
    if v is None:
        return "NA"
    return ("🟢 " if v >= 0 else "🔴 ") + f"{v:+.1f}%"


def send(text):
    for cid in CHAT_IDS:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": cid, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=30)
        print("telegram", cid, r.status_code, r.text[:150])
        if not r.ok:
            sys.exit(f"ERROR: Telegram rejected the message: {r.text}")


def send_chunks(blocks):
    cur = ""
    for b in blocks:
        if len(cur) + len(b) > 3800:      # Telegram limit is 4096 chars
            send(cur)
            cur = ""
        cur += b + "\n\n"
    if cur.strip():
        send(cur)


# ---------------------------------------------------------------- universe
def load_symbols():
    for url in NIFTY500_URLS:
        try:
            r = requests.get(url, headers=HDR, timeout=30)
            r.raise_for_status()
            rd = csv.DictReader(io.StringIO(r.text))
            syms = [row["Symbol"].strip() for row in rd if row.get("Symbol")]
            if syms:
                print("loaded", len(syms), "symbols from", url)
                return syms
        except Exception as ex:
            print("failed", url, ex)
    return []


# ---------------------------------------------------------------- price data
def download(symbols):
    """Full daily history (needed for all-time high) in chunks of 100."""
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        tick = [s + ".NS" for s in chunk]
        try:
            df = yf.download(tick, period="max", interval="1d", group_by="ticker",
                             auto_adjust=False, progress=False, threads=True)
        except Exception as ex:
            print("download failed", i, ex)
            continue
        for s in chunk:
            try:
                d = df[s + ".NS"].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(d) >= 40:
                out[s] = d
        print("downloaded", min(i + 100, len(symbols)), "/", len(symbols))
    return out


def scan(hist, today):
    """Return (matching stocks, number of stocks that have a bar for today)."""
    res, live = [], 0
    for s, d in hist.items():
        if d.index[-1].date() != today:
            continue
        live += 1
        vols = d["Volume"]
        vol = float(vols.iloc[-1])
        prev = vols.iloc[-6:-1]                      # last 5 full trading days
        if len(prev) < 5:
            continue
        avg = float(prev.mean())
        if avg <= 0 or avg < MIN_AVG_VOL:
            continue
        ratio = vol / avg
        if ratio < VOL_MULT:
            continue
        close = d["Close"]
        cmp_ = float(close.iloc[-1])
        last = d.iloc[-252:]                         # ~52 weeks
        smas = {n: (cmp_ / float(close.iloc[-n:].mean()) - 1) * 100
                for n in SMA_LIST if len(close) >= n}
        res.append(dict(sym=s, cmp=cmp_, vol=vol, avg=avg, ratio=ratio,
                        hi52=float(last["High"].max()), lo52=float(last["Low"].min()),
                        ath=float(d["High"].max()), smas=smas))
    return res, live


# ---------------------------------------------------------------- delivery %
def delivery_map():
    """Delivery % from NSE bhavcopy (latest published day; today's comes after market close)."""
    now = datetime.now(IST).date()
    for back in range(0, 8):
        day = now - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        ds = day.strftime("%d%m%Y")
        for base in ("https://nsearchives.nseindia.com/products/content/",
                     "https://archives.nseindia.com/products/content/"):
            try:
                r = requests.get(f"{base}sec_bhavdata_full_{ds}.csv", headers=HDR, timeout=30)
                if r.status_code == 200 and len(r.text) > 1000:
                    df = pd.read_csv(io.StringIO(r.text))
                    df.columns = df.columns.str.strip()
                    df["SERIES"] = df["SERIES"].astype(str).str.strip()
                    df = df[df["SERIES"] == "EQ"]
                    sym = df["SYMBOL"].astype(str).str.strip()
                    dp = pd.to_numeric(df["DELIV_PER"].astype(str).str.strip(), errors="coerce")
                    print("delivery data for", day)
                    return dict(zip(sym, dp)), day
            except Exception as ex:
                print("delivery failed", ds, ex)
    return {}, None


# ---------------------------------------------------------------- Screener
def fetch(url):
    for i in range(4):
        try:
            r = requests.get(url, headers=HDR, timeout=30)
            if r.status_code == 200:
                return BeautifulSoup(r.text, "lxml")
            print("screener status", r.status_code, url)
            if r.status_code == 429:
                time.sleep(10 * (i + 1))
            else:
                return None
        except requests.RequestException:
            time.sleep(3)
    return None


_ind_cache = {}


def industry_rows(href):
    """All companies of a Screener industry page: list of (symbol, pe, market cap)."""
    if href in _ind_cache:
        return _ind_cache[href]
    rows, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        soup = fetch(f"https://www.screener.in{href}?page={page}")
        time.sleep(SLEEP)
        if soup is None:
            break
        table = soup.select_one("table")
        if table is None:
            break
        heads = [th.get_text(" ", strip=True) for th in table.select("thead th")]
        pe_i = next((i for i, h in enumerate(heads) if h.startswith("P/E")), None)
        mc_i = next((i for i, h in enumerate(heads) if h.startswith("Mar Cap")), None)
        if pe_i is None or mc_i is None:
            break
        new = 0
        for tr in table.select("tr"):
            tds = tr.find_all("td")
            a = tr.select_one('a[href*="/company/"]')
            if not a or len(tds) <= max(pe_i, mc_i):
                continue
            sym = a["href"].split("/company/")[1].split("/")[0]
            if sym in seen:
                continue
            seen.add(sym)
            new += 1
            rows.append((sym, num(tds[pe_i].get_text(strip=True)),
                         num(tds[mc_i].get_text(strip=True))))
        if new == 0:
            break
    _ind_cache[href] = rows
    return rows


def profit_growth(soup):
    """Compounded profit growth (%) from Screener: 3-year, else 5-year."""
    for t in soup.select("table.ranges-table"):
        th = t.select_one("th")
        if th and "compounded profit" in th.get_text(" ", strip=True).lower():
            vals = {}
            for tr in t.select("tr"):
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    key = tds[0].get_text(strip=True).rstrip(":").lower()
                    vals[key] = num(tds[1].get_text(strip=True))
            for k in ("3 years", "5 years"):
                if vals.get(k) is not None:
                    return vals[k], k
    return None, None


def blank(it):
    for k in ("pe", "eps", "ind", "ind_pe", "rank", "total", "peg", "growth", "growth_yrs"):
        it.setdefault(k, None)
    it.setdefault("name", it["sym"])


def enrich(it):
    blank(it)
    soup = fetch(f"https://www.screener.in/company/{quote(it['sym'])}/consolidated/")
    if soup is None:
        soup = fetch(f"https://www.screener.in/company/{quote(it['sym'])}/")
    time.sleep(SLEEP)
    if soup is None:
        return
    h1 = soup.select_one("h1")
    if h1:
        it["name"] = h1.get_text(strip=True)
    ratios = {}
    for li in soup.select("#top-ratios li"):
        n, v = li.select_one(".name"), li.select_one(".value")
        if n and v:
            ratios[n.get_text(strip=True)] = v.get_text(" ", strip=True)
    pe = num(ratios.get("Stock P/E"))
    it["pe"] = pe
    if pe and pe > 0:
        it["eps"] = it["cmp"] / pe                    # TTM EPS implied by P/E

    # PEG ratio: Screener's own value if shown, else P/E / profit growth
    growth, yrs = profit_growth(soup)
    it["growth"], it["growth_yrs"] = growth, yrs
    peg = num(ratios.get("PEG Ratio"))
    if peg is None and pe and pe > 0 and growth and growth > 0:
        peg = pe / growth
    it["peg"] = peg

    links = [a for a in soup.select("#peers p.sub a")
             if (a.get("href") or "").startswith("/market/")]
    if not links:
        return
    a = links[-1]                                     # most specific = industry
    it["ind"] = a.get_text(strip=True)
    rows = industry_rows(a["href"])
    pes = [r[1] for r in rows if r[1] and r[1] > 0]
    if pes:
        it["ind_pe"] = statistics.median(pes)
    caps = sorted([r for r in rows if r[2] is not None], key=lambda r: -r[2])
    for i, r in enumerate(caps, 1):
        if r[0] == it["sym"]:
            it["rank"], it["total"] = i, len(caps)
            break


# ---------------------------------------------------------------- report
def build(rank, d):
    e = html.escape
    tv = f"https://www.tradingview.com/chart/?symbol=NSE:{quote(d['sym'])}"
    sc = f"https://www.screener.in/company/{quote(d['sym'])}/consolidated/"
    below_ath = (d["cmp"] / d["ath"] - 1) * 100
    below_hi = (d["cmp"] / d["hi52"] - 1) * 100
    s = d["smas"]
    sma1 = " | ".join(f"{n}: {sign(s.get(n))}" for n in SMA_LIST[:3])
    sma2 = " | ".join(f"{n}: {sign(s.get(n))}" for n in SMA_LIST[3:])
    rk = f"#{d['rank']} of {d['total']}" if d["rank"] else "NA"
    if d.get("growth") is not None and d.get("growth_yrs"):
        gtxt = f" (profit CAGR {d['growth']:.0f}%, {d['growth_yrs']})"
    else:
        gtxt = ""
    return (
        f"<b>{rank}. {e(d['name'])}</b> ({e(d['sym'])})\n"
        f"🏭 {e(d['ind'] or 'NA')}\n"
        f"CMP: {f(d['cmp'], 2, '₹')}\n"
        f"📊 Volume: <b>{d['ratio']:.1f}x</b> 1-week avg "
        f"({d['vol'] / 1e5:.2f}L vs {d['avg'] / 1e5:.2f}L)\n"
        f"📦 Delivery: <b>{f(d.get('deliv'), 1, suf='%')}</b> (last published day)\n"
        f"52W High: {f(d['hi52'], 2, '₹')} ({below_hi:+.1f}%) | "
        f"52W Low: {f(d['lo52'], 2, '₹')}\n"
        f"All-time High: {f(d['ath'], 2, '₹')} ({below_ath:+.1f}%)\n"
        f"P/E: {f(d['pe'])} | Industry P/E: {f(d['ind_pe'])} | EPS: {f(d['eps'], 2, '₹')}\n"
        f"PEG: <b>{f(d['peg'], 2)}</b>{gtxt}\n"
        f"Market-cap rank in industry: {rk}\n"
        f"Price vs SMA → {sma1}\n"
        f"{sma2}\n"
        f"📈 <a href=\"{tv}\">TradingView Chart</a> | <a href=\"{sc}\">Screener</a>"
    )


def main():
    symbols = load_symbols()
    if not symbols:
        send("Volume 2x scanner: could not download the Nifty 500 list.")
        sys.exit("ERROR: no symbols")
    now = datetime.now(IST)
    hist = download(symbols)
    res, live = scan(hist, now.date())
    print("stocks with today's bar:", live, "| matches:", len(res))

    if live < 50:
        send(f"Volume 2x scanner ({now:%d-%b %H:%M} IST): no live price data for today "
             f"(market holiday or data delay).")
        return

    dmap, dday = delivery_map()
    for r in res:
        r["deliv"] = dmap.get(r["sym"])
    res.sort(key=lambda r: (r["deliv"] if r["deliv"] == r["deliv"] and r["deliv"] is not None
                            else -1), reverse=True)

    if not res:
        send(f"Volume 2x scanner ({now:%d-%b %H:%M} IST): 0 stocks with volume ≥ "
             f"{VOL_MULT:g}x of 1-week average (checked {live} stocks).")
        return

    top = res[:TOP_N]
    for it in top:
        try:
            enrich(it)
        except Exception as ex:
            print("enrich failed", it["sym"], ex)
            blank(it)

    note = f"Delivery % from {dday:%d-%b}" if dday else "Delivery % not available"
    head = (f"<b>🔥 Volume ≥ {VOL_MULT:g}x of 1-week avg | Top {len(top)} by Delivery %</b>\n"
            f"{now:%d-%b-%Y %H:%M} IST | {len(res)} stocks matched out of {live}\n"
            f"{note}")
    send_chunks([head] + [build(i, d) for i, d in enumerate(top, 1)])


if __name__ == "__main__":
    main()
