"""Indian stocks (NSE) for the bot: Yahoo prices and candles, the NIFTY 50 universe, market hours and the rules an
NSE config must follow. Paper only: Yahoo is unofficial and can lag a few minutes, fine for simulated fills on
1h/1d bars, not for real orders (those need a broker API, SEBI's static-IP registration and a daily login)."""
from __future__ import annotations

import collections
import csv
import datetime as dt
import io
import json
import time

from data import cached, http_get, yahoo_chart

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
OPEN, CLOSE = dt.time(9, 15), dt.time(15, 30)
# bot interval -> (Yahoo interval, a range holding 300+ bars of it; Yahoo keeps 15m/30m for 60 days, 1h for 730)
INTERVALS = {"15m": ("15m", "1mo"), "30m": ("30m", "2mo"), "1h": ("60m", "3mo"), "1d": ("1d", "2y")}
STEP = {"15m": 900, "30m": 1800, "1h": 3600, "1d": 86400}


def is_open(now=None):
    """NSE's regular session, Mon-Fri 09:15-15:30 IST.
    ponytail: exchange holidays aren't listed; on one the bot just sees no new bars, so it doesn't trade."""
    t = dt.datetime.fromtimestamp(time.time() if now is None else now, IST)
    return t.weekday() < 5 and OPEN <= t.time() < CLOSE


def next_decision(interval, now):
    """Unix time of the bot's next decision: the next bar start inside a session (a bar is closed once the next one
    opens). 1h bars start 09:15, 10:15 ... 15:15; a 1d bar is decided at the next session's open."""
    t = dt.datetime.fromtimestamp(now, IST)
    for d in range(8):
        day = t.date() + dt.timedelta(days=d)
        if day.weekday() >= 5:
            continue
        b = dt.datetime.combine(day, OPEN, IST)
        while b.time() < CLOSE:
            if b.timestamp() > now:
                return b.timestamp()
            if STEP[interval] >= 86400:
                break
            b += dt.timedelta(seconds=STEP[interval])
    return None


def rules(cfg):
    """What an NSE config may not do -> error string or None. Checked at startup and on every dashboard save."""
    if cfg["mode"] != "paper":
        return "Indian stocks run in paper mode only: no broker is connected"
    if cfg["allow_short"]:
        return "stocks can't be shorted overnight: turn allow_short off"
    if cfg["risk"]["max_leverage"] != 1:
        return "delivery trades have no leverage: max_leverage must be 1"
    if cfg["ai"]["enabled"]:
        return "the AI prompts are written for crypto: keep the AI signal off"
    if cfg["weights"].get("fear_greed"):
        return "Fear & Greed is a crypto index: set its weight to 0"
    if cfg["interval"] not in INTERVALS:
        return "interval must be one of " + ", ".join(INTERVALS) + " (Yahoo has no 4h bars)"
    return None


def nifty50():
    rows = csv.DictReader(io.StringIO(http_get("https://archives.nseindia.com/content/indices/ind_nifty50list.csv").decode()))
    return [r["Symbol"].strip() for r in rows]


def spark(symbols):
    """{symbol: quote} for up to 20 NSE symbols in one request. Unknown symbols are left out."""
    raw = json.loads(http_get("https://query1.finance.yahoo.com/v7/finance/spark?range=1d&interval=1d&symbols="
                              + ",".join(s + ".NS" for s in symbols), timeout=20))
    out = {}
    for r in raw["spark"]["result"] or []:
        m = (r.get("response") or [{}])[0].get("meta") or {}
        if m.get("regularMarketPrice"):
            px = float(m["regularMarketPrice"])
            out[r["symbol"].removesuffix(".NS")] = {
                "px": px, "prev": m.get("chartPreviousClose"), "high": m.get("regularMarketDayHigh"),
                "low": m.get("regularMarketDayLow"), "vol": (m.get("regularMarketVolume") or 0) * px,
                "name": m.get("longName") or m.get("shortName"), "maxLev": 1}
    return out


class Market:
    """The methods bot.py uses on data.Market. Set `watch` (the symbols to price) before each refresh()."""

    def __init__(self):
        self.meta, self.ctx = {}, collections.defaultdict(dict)  # ctx: no funding for stocks
        self.quotes, self.watch = {}, set()

    def refresh(self):
        if is_open() or self.watch - set(self.quotes):  # prices don't move while NSE is closed
            syms = sorted(self.watch)
            for i in range(0, len(syms), 20):
                self.quotes.update(spark(syms[i:i + 20]))
        universe = cached("nifty50", 86400, nifty50) or []  # a failed fetch leaves just the priced symbols
        self.meta = {s: {"szDecimals": 0, "maxLeverage": 1} for s in set(universe) | set(self.quotes)}

    def is_open(self):
        return is_open()

    def mids(self):
        return {s: q["px"] for s, q in self.quotes.items()}

    def candles(self, coin, interval, bars):
        """Oldest first; the last bar may still be forming, like Hyperliquid's. Cached briefly, longer while closed."""
        iv, rng = INTERVALS[interval]
        got = cached("nse_%s_%s" % (coin, interval), 120 if is_open() else 3600,
                     lambda: yahoo_chart(coin, "interval=%s&range=%s" % (iv, rng)))
        return (got or [])[-bars:]

    def book_imbalance(self, coin, levels=10):
        return None  # no order book from Yahoo: the factor counts as unavailable
