"""Backtest the technical part of the strategy on real Hyperliquid candles, fees included.
    python backtest.py --coin BTC [--interval 1h] [--config config.json]
Funding/orderbook/news/Jev have no history here, so only trend/momentum/mean_reversion weights count."""
from __future__ import annotations

import argparse
import json

import strategy


def backtest(candles, cfg, equity=100.0, sz_decimals=5):
    ind, rk, fees = cfg["indicators"], cfg["risk"], cfg["fees"]
    cost = (fees["taker_pct"] + fees["slippage_pct"]) / 100
    warm = ind["ema_slow"] + ind["mom_len"] + 2
    start, peak, max_dd, pos, trades, fees_paid = equity, equity, 0.0, None, [], 0.0
    for i in range(warm, len(candles) - 1):
        bar = candles[i]
        if pos:  # stop checked before target: conservative when both are inside one bar
            hit = (pos["sl"] if (bar["l"] <= pos["sl"] if pos["side"] > 0 else bar["h"] >= pos["sl"]) else
                   pos["tp"] if (bar["h"] >= pos["tp"] if pos["side"] > 0 else bar["l"] <= pos["tp"]) else None)
            if hit is not None:
                pnl = pos["side"] * (hit - pos["entry"]) * pos["size"] - pos["size"] * hit * cost
                equity += pnl
                fees_paid += pos["size"] * hit * cost
                trades.append(pnl)
                pos = None
        window = candles[max(0, i + 1 - cfg["lookback_bars"]):i + 1]
        f, info = strategy.technical(window, ind)
        score = strategy.combine(f, cfg["weights"])
        nxt = candles[i + 1]["o"]
        if pos and pos["side"] * score <= -cfg["exit_threshold"]:
            pnl = pos["side"] * (nxt - pos["entry"]) * pos["size"] - pos["size"] * nxt * cost
            equity += pnl
            fees_paid += pos["size"] * nxt * cost
            trades.append(pnl)
            pos = None
        elif not pos and abs(score) >= cfg["entry_threshold"] and (score > 0 or cfg["allow_short"]):
            size, sl_d, tp_d, why = strategy.size_position(equity, nxt, info["atr"], rk, fees, sz_decimals)
            if not why:
                side = 1 if score > 0 else -1
                equity -= size * nxt * cost
                fees_paid += size * nxt * cost
                pos = {"side": side, "size": size, "entry": nxt, "sl": nxt - side * sl_d, "tp": nxt + side * tp_d}
        mark = equity + (pos["side"] * (bar["c"] - pos["entry"]) * pos["size"] if pos else 0)
        peak = max(peak, mark)
        max_dd = max(max_dd, (peak - mark) / peak)
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    return {
        "trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0,
        "return_pct": round(100 * (equity / start - 1), 2),
        "max_drawdown_pct": round(100 * max_dd, 2),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) else None,
        "fees_paid_usd": round(fees_paid, 2),
        "buy_and_hold_pct": round(100 * (candles[-1]["c"] / candles[warm]["o"] - 1), 2),
    }


if __name__ == "__main__":
    import time
    from data import INTERVAL_MS, Market
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
    candles = m.candles(a.coin, interval, 5000)  # Hyperliquid serves the most recent 5000 bars
    days = (candles[-1]["t"] - candles[0]["t"]) / 86_400_000
    print("%s %s: %d bars (%.0f days)" % (a.coin, interval, len(candles), days))
    print(json.dumps(backtest(candles, cfg, a.equity, m.meta[a.coin]["szDecimals"]), indent=1))
