"""Funding carry backtest: hold the coin and short the same amount of its perp. Price moves cancel out, and the short
collects funding while longs are paying. It only holds while the trailing funding rate is high, otherwise sits in cash.
    python src/carry.py [--source binance|hyperliquid] [--coins BTC,ETH,SOL] [--entry-apr 15] [--exit-apr 5]
                        [--lookback-days 7] [--leverage 2]
Research only: the bot doesn't trade carry. Hyperliquid nets one position per coin per account, so live carry next to
the trend bot would need its own sub-account, and the spot leg (UBTC, UETH, USOL, HYPE) its own order code.
Not modelled: the gap between spot and perp prices, and spot fees above the base tier."""
from __future__ import annotations

import argparse
import datetime as dt
import json

from backtest import Funding, history

DAY = 86_400_000
DEFAULTS = {
    "entry_apr": 15.0,       # open when the trailing funding rate pays at least this (% a year on the short)
    "exit_apr": 5.0,         # close when it falls below this
    "lookback_days": 7,      # trailing window for that rate
    "leverage": 2.0,         # short leg leverage: capital splits spot : margin = lev : 1
    "rebalance_pct": 25.0,   # top up the short's margin from the spot leg after a move this big
    "liq_pct": 48.0,         # isolated short at 2x is liquidated about this far up (minus maintenance margin)
    "spot_taker_pct": 0.07,  # Hyperliquid base tier
    "perp_taker_pct": 0.045,
    "slippage_pct": 0.02,
    "max_positions": 1,      # with $100 a second pair would leave each leg near the $10 minimum
}


def trailing_apr(fund, t, days):
    """Average funding over the last `days` before t, as % a year."""
    return fund.paid(t - days * DAY, t) / (days * 24) * 8760 * 100


def simulate_carry(markets, cfg=None, equity=100.0, start=None, end=None):
    """markets: {coin: (candles, [(t_ms, hourly funding rate)])}. Decides at each bar open from closed data only.
    -> summary dict with yearly returns, time in market, costs and income."""
    c = dict(DEFAULTS, **(cfg or {}))
    lev, slip = c["leverage"], c["slippage_pct"] / 100
    leg_cost = (c["spot_taker_pct"] + c["perp_taker_pct"]) / 100 + 2 * slip  # one trade on each leg
    fund = {k: Funding(f) for k, (_, f) in markets.items()}
    steps = {}
    for coin, (candles, _) in markets.items():
        for prev, bar in zip(candles, candles[1:]):  # decide at bar open, knowing the previous bar
            if (start is None or bar["t"] >= start) and (end is None or bar["t"] < end):
                steps.setdefault(bar["t"], []).append((coin, prev, bar["o"]))
    begin, peak, max_dd = equity, equity, 0.0
    pos, years, last_px = {}, {}, {}
    totals = {"fees": 0.0, "funding": 0.0, "trades": 0, "rebalances": 0, "liquidations": 0}
    held_steps = 0

    def close(coin, px):
        nonlocal equity
        p = pos.pop(coin)
        fee = p["size"] * px * leg_cost
        equity -= fee
        totals["fees"] += fee
        totals["trades"] += 1

    for t in sorted(steps):
        yr = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).year
        years.setdefault(yr, [equity, equity])
        for coin, prev, px in steps[t]:
            p = pos.get(coin)
            if not p:
                continue
            got = p["size"] * prev["c"] * fund[coin].paid(p["ft"], t)  # the short receives positive funding
            equity += got
            totals["funding"] += got
            p["ft"] = t
            if prev["h"] >= p["ref"] * (1 + c["liq_pct"] / 100):  # spiked through the short's liquidation price
                totals["liquidations"] += 1
                close(coin, p["ref"] * (1 + c["liq_pct"] / 100))
                extra = p["size"] * p["ref"] * leg_cost  # forced fills: pay the costs twice, roughly
                equity -= extra
                totals["fees"] += extra
            elif abs(prev["c"] / p["ref"] - 1) * 100 >= c["rebalance_pct"]:
                # the spot leg made what the short lost (or the reverse), so the pair no longer matches the account:
                # resize both legs back to it. Keeping the coin count fixed would let a rally inflate the income.
                size = equity / c["max_positions"] * lev / (lev + 1) / prev["c"]
                fee = abs(size - p["size"]) * prev["c"] * leg_cost
                equity -= fee
                totals["fees"] += fee
                totals["rebalances"] += 1
                p["size"], p["ref"] = size, prev["c"]
        prices = {coin: px for coin, _, px in steps[t]}
        last_px.update(prices)
        for coin in list(pos):
            if coin in prices and trailing_apr(fund[coin], t, c["lookback_days"]) < c["exit_apr"]:
                close(coin, prices[coin])
        ranked = sorted(((trailing_apr(fund[coin], t, c["lookback_days"]), coin) for coin in prices if coin not in pos),
                        reverse=True)
        for apr, coin in ranked:
            if apr < c["entry_apr"] or len(pos) >= c["max_positions"]:
                break
            notional = equity / c["max_positions"] * lev / (lev + 1)  # spot leg; the short's margin is notional / lev
            if notional < 10.5:  # Hyperliquid's $10 minimum, per leg
                continue
            px = prices[coin]
            pos[coin] = {"size": notional / px, "ref": px, "ft": t}
            fee = notional * leg_cost
            equity -= fee
            totals["fees"] += fee
        held_steps += bool(pos)
        years[yr][1] = equity
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    for coin in list(pos):
        close(coin, last_px[coin])
    if years:
        years[max(years)][1] = equity
    span = (max(steps) - min(steps)) / DAY / 365 if len(steps) > 1 else 0
    return {
        "return_pct": 100 * (equity / begin - 1),
        "per_year_pct": 100 * ((equity / begin) ** (1 / span) - 1) if span and equity > 0 else 0.0,
        "max_drawdown_pct": 100 * max_dd,
        "in_market_pct": 100 * held_steps / len(steps) if steps else 0.0,
        "by_year_pct": {y: 100 * (b / a - 1) for y, (a, b) in years.items()},
        **{k: round(v, 2) if isinstance(v, float) else v for k, v in totals.items()},
    }


def always_on(markets, lev=DEFAULTS["leverage"]):
    """What plain carry would have paid with no timing: funding per year on the capital, before costs."""
    out = {}
    for coin, (candles, f) in markets.items():
        fund, t0, t1 = Funding(f), candles[0]["t"], candles[-1]["t"]
        years = (t1 - t0) / DAY / 365
        out[coin] = fund.paid(t0, t1) / years * 100 * lev / (lev + 1) if years else 0.0
    return out


def main():
    from data import Market
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["binance", "hyperliquid"], default="binance")
    ap.add_argument("--coins", help="comma separated; default: config.json coins")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--equity", type=float, default=100)
    for k in ("entry_apr", "exit_apr", "lookback_days", "leverage"):
        ap.add_argument("--" + k.replace("_", "-"), type=float, default=DEFAULTS[k])
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    m = Market() if a.source == "hyperliquid" else None  # Binance history doesn't need Hyperliquid
    if m:
        m.refresh()
    markets = {}
    for coin in a.coins.split(",") if a.coins else cfg["coins"]:
        markets[coin] = history(m, coin, "4h", source=a.source)
    c = {k: getattr(a, k) for k in ("entry_apr", "exit_apr", "lookback_days", "leverage")}
    r = simulate_carry(markets, c, a.equity)
    print("carry on %s from %s: open when the %g-day funding average pays >= %g%%/yr, close below %g%%, short at %gx, $%g\n"
          % (",".join(markets), a.source, a.lookback_days, a.entry_apr, a.exit_apr, a.leverage, a.equity))
    for y, v in r["by_year_pct"].items():
        print("  %s  %+6.1f%%" % (y, v))
    print("\n  total %+.1f%% (%+.1f%%/yr), worst drawdown %.1f%%, in the market %.0f%% of the time" % (
        r["return_pct"], r["per_year_pct"], r["max_drawdown_pct"], r["in_market_pct"]))
    print("  %d round trips, %d rebalances, %d liquidations; funding received $%.2f, fees paid $%.2f" % (
        r["trades"], r["rebalances"], r["liquidations"], r["funding"], r["fees"]))
    print("  holding carry all the time instead (no timing, before costs), per year on capital: " +
          "  ".join("%s %+.1f%%" % kv for kv in always_on(markets, a.leverage).items()))


if __name__ == "__main__":
    main()
