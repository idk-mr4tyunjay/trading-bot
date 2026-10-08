"""Network inputs: Hyperliquid market data, Fear & Greed index, news headlines."""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import xml.etree.ElementTree as ET

from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.error import ClientError

log = logging.getLogger("data")
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
_cache = {}


def cached(key, ttl, fn):
    """Return fn() at most once per ttl seconds; on failure fall back to the last good value."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        log.warning("%s failed: %s", key, e)
        return hit[1] if hit else None
    _cache[key] = (time.time(), val)
    return val


def retry_429(fn, *args):
    """Call fn, backing off (1, 2, 4 ... 32 s) while Hyperliquid answers 429 rate-limited."""
    wait = 1.0
    while True:
        try:
            return fn(*args)
        except ClientError as e:
            if e.status_code != 429 or wait > 60:
                raise
            time.sleep(wait)
            wait *= 2


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (trading-bot)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def binance_candles(coin, interval, start_ms, end_ms):
    """Closed USDT-M perp candles from Binance's public API (no key), oldest first. Used only for long backtests:
    Hyperliquid serves just the latest 5000 bars, and Binance's prices track Hyperliquid's closely for majors."""
    out, t = [], start_ms
    while t < end_ms:
        rows = json.loads(http_get("https://fapi.binance.com/fapi/v1/klines?symbol=%sUSDT&interval=%s&startTime=%d&limit=1500"
                                   % (coin, interval, t), timeout=30))
        out += [{"t": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
                for r in rows if r[6] < end_ms]  # r[6] = close time: keep closed bars only
        if len(rows) < 1500:
            break
        t = rows[-1][0] + 1
        time.sleep(0.3)
    return out


def binance_funding(coin, start_ms, end_ms):
    """Binance funding events as [(time_ms, rate for the period)], oldest first."""
    out, t = [], start_ms
    while t < end_ms:
        rows = json.loads(http_get("https://fapi.binance.com/fapi/v1/fundingRate?symbol=%sUSDT&startTime=%d&endTime=%d&limit=1000"
                                   % (coin, t, end_ms), timeout=30))
        out += [(r["fundingTime"], float(r["fundingRate"])) for r in rows]
        if len(rows) < 1000:
            break
        t = rows[-1]["fundingTime"] + 1
        time.sleep(0.3)
    return out


def hourly_funding(events):
    """Binance pays every 8h (sometimes 4h); Hyperliquid every hour. Spread each event over the hours after it is
    paid as [(time_ms, hourly rate)], so both venues look alike and a backtest never sees a rate before it is known."""
    out = []
    for i, (t, r) in enumerate(events):
        gap = min(max(round((t - events[i - 1][0]) / 3_600_000), 1), 8) if i else 8
        out += [(t + k * 3_600_000, r / gap) for k in range(gap)]
    return out


def fear_greed():
    """alternative.me Crypto Fear & Greed, 0..100, updates daily."""
    return cached("fear_greed", 3600, lambda: int(json.loads(http_get("https://api.alternative.me/fng/?limit=1"))["data"][0]["value"]))


def fear_greed_history():
    """Full daily Fear & Greed history as [(time_ms, value)], oldest first."""
    rows = json.loads(http_get("https://api.alternative.me/fng/?limit=0"))["data"]
    return sorted((int(r["timestamp"]) * 1000, int(r["value"])) for r in rows)


def headlines(feeds, ttl, n=25):
    def fetch():
        titles = []
        for url in feeds:
            try:
                titles += [i.findtext("title").strip() for i in ET.fromstring(http_get(url)).iter("item") if i.findtext("title")][:n // len(feeds)]  # newest few from each feed
            except Exception as e:
                log.warning("feed %s: %s", url, e)
        return titles
    return cached("headlines", ttl, fetch)


class Market:
    def __init__(self, testnet=False):
        self.info = Info(constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL, skip_ws=True)
        self.meta, self.ctx = {}, {}

    def refresh(self):
        meta, ctxs = self.info.meta_and_asset_ctxs()
        for a, c in zip(meta["universe"], ctxs):
            self.meta[a["name"]], self.ctx[a["name"]] = a, c

    def mids(self):
        return {k: float(v) for k, v in self.info.all_mids().items()}

    def candles(self, coin, interval, bars):
        now = int(time.time() * 1000)
        raw = retry_429(self.info.candles_snapshot, coin, interval, now - bars * INTERVAL_MS[interval], now)
        return [{"t": x["t"], "o": float(x["o"]), "h": float(x["h"]), "l": float(x["l"]),
                 "c": float(x["c"]), "v": float(x["v"])} for x in raw]

    def funding_history(self, coin, start_ms, end_ms):
        """Hourly funding rates as [(time_ms, rate)]. The API pages 500 at a time and each page costs ~45 of the
        1200/min rate-limit weight, so pages are spaced out."""
        out = []
        while start_ms < end_ms:
            page = retry_429(self.info.funding_history, coin, start_ms, end_ms)
            time.sleep(2.5)
            if not page:
                break
            out += [(x["time"], float(x["fundingRate"])) for x in page]
            start_ms = page[-1]["time"] + 1
        return out

    def book_imbalance(self, coin, levels=10):
        """(bid size - ask size) / total over the top levels: -1 all asks .. 1 all bids."""
        bids, asks = self.info.l2_snapshot(coin)["levels"]
        b = sum(float(x["sz"]) for x in bids[:levels])
        a = sum(float(x["sz"]) for x in asks[:levels])
        return (b - a) / (b + a) if b + a else 0.0
