"""Backtest the strategy on real Hyperliquid candles, fees included.
    python src/backtest.py --coin BTC [--interval 1h] [--config config.json]
Prints two runs: technical factors only, and "like_live", which replays historical funding and
Fear & Greed and treats the order book as neutral. News/AI have no history and are left out.
simulate() also runs several coins on one account under max_open_positions, like the live bot."""
from __future__ import annotations

import argparse
import json
import os
from bisect import bisect_right

import strategy

CACHE = ".cache"


def asof(series, t):
    """Latest value at or before t from [(t, value)] sorted by t, else None."""
    i = bisect_right(series, (t, float("inf")))
    return series[i - 1][1] if i else None


def prepare(candles, cfg, live=None):
    """Factor scores for every closed bar, computed once and reused across parameter sets.
    live: {"funding": [(t_ms, hourly_rate)], "fear_greed": [(t_ms, 0..100)]} to score bars like the live bot."""
    ind = cfg["indicators"]
    rows = []
    for i in range(ind["ema_slow"] + ind["mom_len"] + 2, len(candles) - 1):
        f, info = strategy.technical(candles[max(0, i + 1 - cfg["lookback_bars"]):i + 1], ind)
        decided = candles[i + 1]["t"]  # bar i has closed; the bot decides now
        if live is not None:
            rate, fg = asof(live["funding"], decided), asof(live["fear_greed"], decided)
            f["funding"] = strategy.factor_funding(rate) if rate is not None else None
            f["fear_greed"] = strategy.factor_fear_greed(fg) if fg is not None else None
            f["orderbook"] = 0.0  # no history; live it is noise around 0
        rows.append({"bar": candles[i], "t": decided, "nxt": candles[i + 1]["o"], "f": f, "atr": info["atr"]})
    return rows


def simulate(prepared, cfg, equity=100.0, sz_decimals=None, start=None, end=None, close_at_end=False, pnls=None):
    """prepared: {coin: prepare(...)} in priority order (the live bot scans cfg["coins"] in order).
    One account: realized equity sizes each trade and max_open_positions caps them.
    start/end bound the decision times (ms). pnls, if given, collects every trade's P&L."""
    rk, fees = cfg["risk"], cfg["fees"]
    cost = (fees["taker_pct"] + fees["slippage_pct"]) / 100
    hold_ms = rk["max_hold_hours"] * 3_600_000
    steps = {}
    for coin, rows in prepared.items():
        for r in rows:
            if (start is None or r["t"] >= start) and (end is None or r["t"] < end):
                steps.setdefault(r["t"], []).append((coin, r))
    begin, peak, max_dd, fees_paid = equity, equity, 0.0, 0.0
    pos, last, trades = {}, {}, []

    def close(coin, px):
        nonlocal equity, fees_paid
        p = pos.pop(coin)
        pnl = p["side"] * (px - p["entry"]) * p["size"] - p["size"] * px * cost
        equity += pnl
        fees_paid += p["size"] * px * cost
        trades.append((coin, pnl))

    for t in sorted(steps):
        for coin, r in steps[t]:  # stop checked before target: conservative when both are inside one bar
            p, bar = pos.get(coin), r["bar"]
            if p:
                hit = (p["sl"] if (bar["l"] <= p["sl"] if p["side"] > 0 else bar["h"] >= p["sl"]) else
                       p["tp"] if (bar["h"] >= p["tp"] if p["side"] > 0 else bar["l"] <= p["tp"]) else None)
                if hit is not None:
                    close(coin, hit)
        for coin, r in steps[t]:
            score, nxt, p = strategy.combine(r["f"], cfg["weights"]), r["nxt"], pos.get(coin)
            if p:
                if p["side"] * score <= -cfg["exit_threshold"] or t - p["opened"] > hold_ms:
                    close(coin, nxt)
            elif (abs(score) >= cfg["entry_threshold"] and (score > 0 or cfg["allow_short"])
                  and len(pos) < rk["max_open_positions"]):
                size, sl_d, tp_d, why = strategy.size_position(equity, nxt, r["atr"], rk, fees, sz_decimals[coin])
                if not why:
                    side = 1 if score > 0 else -1
                    equity -= size * nxt * cost
                    fees_paid += size * nxt * cost
                    pos[coin] = {"side": side, "size": size, "entry": nxt, "opened": t,
                                 "sl": nxt - side * sl_d, "tp": nxt + side * tp_d}
            last[coin] = r["bar"]["c"]
        mark = equity + sum(p["side"] * (last[c] - p["entry"]) * p["size"] for c, p in pos.items())
        peak = max(peak, mark)
        max_dd = max(max_dd, (peak - mark) / peak)
    if close_at_end:
        for coin in list(pos):
            close(coin, last[coin])
    vals = [x for _, x in trades]
    if pnls is not None:
        pnls.extend(vals)
    wins, losses = [x for x in vals if x > 0], [x for x in vals if x <= 0]
    out = {
        "trades": len(vals),
        "win_rate_pct": round(100 * len(wins) / len(vals), 1) if vals else 0,
        "return_pct": round(100 * (equity / begin - 1), 2),
        "max_drawdown_pct": round(100 * max_dd, 2),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) else None,
        "fees_paid_usd": round(fees_paid, 2),
    }
    if len(prepared) > 1:
        by = {}
        for c, x in trades:
            b = by.setdefault(c, {"trades": 0, "pnl_usd": 0.0})
            b["trades"] += 1
            b["pnl_usd"] = round(b["pnl_usd"] + x, 2)
        out["per_coin"] = by
    return out


def backtest(candles, cfg, equity=100.0, sz_decimals=5, live=None):
    """One coin, whole history."""
    r = simulate({"": prepare(candles, cfg, live)}, cfg, equity, {"": sz_decimals})
    warm = cfg["indicators"]["ema_slow"] + cfg["indicators"]["mom_len"] + 2
    r["buy_and_hold_pct"] = round(100 * (candles[-1]["c"] / candles[warm]["o"] - 1), 2)
    return r


def history(market, coin, interval, bars=5000):
    """(candles, funding) for one coin. Funding history is slow to page through, so it's cached in .cache/
    and only the newer part is fetched on later runs."""
    from data import INTERVAL_MS
    candles = market.candles(coin, interval, bars)
    end = candles[-1]["t"] + INTERVAL_MS[interval]
    path = os.path.join(CACHE, "funding_%s.json" % coin)
    try:
        with open(path) as f:
            funding = [tuple(x) for x in json.load(f)]
    except FileNotFoundError:
        funding = []
    if not funding or funding[0][0] > candles[0]["t"] + 3_600_000:
        funding = market.funding_history(coin, candles[0]["t"], end)
    else:
        funding += market.funding_history(coin, funding[-1][0] + 1, end)
    os.makedirs(CACHE, exist_ok=True)
    with open(path, "w") as f:
        json.dump(funding, f)
    return candles, funding


if __name__ == "__main__":
    from data import Market, fear_greed_history
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--interval")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--equity", type=float, default=100)
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    interval = a.interval or cfg["interval"]
    m = Market()
    m.refresh()
    candles, funding = history(m, a.coin, interval)  # Hyperliquid serves the most recent 5000 bars
    days = (candles[-1]["t"] - candles[0]["t"]) / 86_400_000
    print("%s %s: %d bars (%.0f days)" % (a.coin, interval, len(candles), days))
    szd = m.meta[a.coin]["szDecimals"]
    live = {"funding": funding, "fear_greed": fear_greed_history()}
    print(json.dumps({"technical_only": backtest(candles, cfg, a.equity, szd),
                      "like_live": backtest(candles, cfg, a.equity, szd, live)}, indent=1))
