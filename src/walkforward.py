"""Walk-forward test: pick entry/exit thresholds on a training window, then trade the next unseen window with them.
    python src/walkforward.py [--source binance|hyperliquid] [--coins BTC,ETH,SOL | --top 15] [--train-days 365]
                              [--test-days 90] [--max-open N]
Rolls forward through history and reports only the unseen (out-of-sample) windows, all coins on one account,
with fees, slippage and historical funding payments. That's the honest estimate: config.json's own values were tuned
on Hyperliquid's history, so "as configured" is optimistic on that period.
--source binance (default) tests on Binance's USDT perps since 2020: ~5.7 unseen years instead of Hyperliquid's ~1.2,
which only serves its latest 5000 bars, so its result also shifts as that window slides.
--top picks today's most-traded perps, which favours coins that survived and grew (survivorship bias)."""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json

from backtest import history, luck, prepare, simulate
from data import Market, fear_greed_history

ENTRY = [0.3, 0.35, 0.4, 0.45, 0.5]
EXIT = [0.1, 0.2, 0.3]
DAY = 86_400_000


def top_coins(market, n, interval, min_days):
    """The n most-traded perps (24h notional) with at least min_days of candles."""
    ranked = sorted((c for c, meta in market.meta.items() if not meta.get("isDelisted")),
                    key=lambda c: -float(market.ctx[c]["dayNtlVlm"]))
    out = []
    for c in ranked:
        k = market.candles(c, interval, 5000)
        if k and (k[-1]["t"] - k[0]["t"]) / DAY >= min_days:
            out.append(c)
        if len(out) == n:
            break
    return out


def day(t):
    return dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def buy_hold(prepared, start, end):
    """Equal-weight buy and hold of the coins that trade in the window, % return."""
    rets = []
    for rows in prepared.values():
        w = [r for r in rows if start <= r["t"] < end]
        if len(w) > 1:
            rets.append(w[-1]["nxt"] / w[0]["nxt"] - 1)
    return 100 * sum(rets) / len(rets) if rets else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["binance", "hyperliquid"], default="binance")
    ap.add_argument("--coins", help="comma separated; default: config.json coins")
    ap.add_argument("--top", type=int, help="use the N most-traded perps instead")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--train-days", type=int, default=365)
    ap.add_argument("--test-days", type=int, default=90)
    ap.add_argument("--max-open", type=int, help="override risk.max_open_positions")
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    if a.max_open:
        cfg["risk"]["max_open_positions"] = a.max_open
    m = Market()
    m.refresh()
    coins = (top_coins(m, a.top, cfg["interval"], a.train_days + a.test_days) if a.top
             else a.coins.split(",") if a.coins else cfg["coins"])
    fng = fear_greed_history()
    prepared, szd, funds = {}, {}, {}
    for c in coins:
        try:
            candles, funding = history(m, c, cfg["interval"], source=a.source)
        except SystemExit as e:  # not listed on Binance
            print("skip   %-6s %s" % (c, e), flush=True)
            continue
        prepared[c] = prepare(candles, cfg, {"funding": funding, "fear_greed": fng})
        szd[c], funds[c] = m.meta[c]["szDecimals"], funding
        print("loaded %-6s %s .. %s (%s)" % (c, day(candles[0]["t"]), day(candles[-1]["t"]), a.source), flush=True)

    t0 = min(r[0]["t"] for r in prepared.values())
    t_end = max(r[-1]["t"] for r in prepared.values()) + 1
    grid = [dict(cfg, entry_threshold=e, exit_threshold=x) for e, x in itertools.product(ENTRY, EXIT)]
    print("\n%d coins, max %d open, train %dd -> test %dd, grid entry %s x exit %s\n" % (
        len(prepared), cfg["risk"]["max_open_positions"], a.train_days, a.test_days, ENTRY, EXIT))
    print("%-25s %-11s %9s %9s %7s %6s %9s %9s" % ("test window", "picked", "train", "test", "trades", "PF", "as-cfg", "buy&hold"))
    wf, cf, bh, wf_pnl, cf_pnl, worst_dd, picks, fund_usd = 1.0, 1.0, 1.0, [], [], 0.0, [], 0.0
    start = t0
    while True:
        tr_end = start + a.train_days * DAY
        te_end = min(tr_end + a.test_days * DAY, t_end)
        if te_end - tr_end < 30 * DAY:
            break
        best = max(grid, key=lambda g: simulate(prepared, g, 100, szd, start, tr_end, True, funding=funds)["return_pct"])
        train = simulate(prepared, best, 100, szd, start, tr_end, True, funding=funds)
        test = simulate(prepared, best, 100, szd, tr_end, te_end, True, wf_pnl, funds)
        conf = simulate(prepared, cfg, 100, szd, tr_end, te_end, True, cf_pnl, funds)
        hold = buy_hold(prepared, tr_end, te_end)
        wf *= 1 + test["return_pct"] / 100
        cf *= 1 + conf["return_pct"] / 100
        bh *= 1 + hold / 100
        fund_usd += test["funding_paid_usd"]
        worst_dd = max(worst_dd, test["max_drawdown_pct"])
        picks.append((best["entry_threshold"], best["exit_threshold"]))
        print("%-25s %-11s %+8.1f%% %+8.1f%% %7d %6s %+8.1f%% %+8.1f%%" % (
            day(tr_end) + " .. " + day(te_end), "%.2f / %.1f" % picks[-1], train["return_pct"], test["return_pct"],
            test["trades"], test["profit_factor"] or "-", conf["return_pct"], hold))
        start += a.test_days * DAY

    def pf(p):
        loss = -sum(x for x in p if x <= 0)
        return "%.2f" % (sum(x for x in p if x > 0) / loss) if loss else "-"
    if not picks:
        return print("not enough history for one train + test window")
    days = len(picks) * a.test_days
    yr = lambda g: 100 * (g ** (365 / days) - 1) if g > 0 else -100.0
    print("\nout of sample, %d windows (~%d days):" % (len(picks), days))
    print("  walk-forward   %+7.1f%%  (%+.1f%%/yr)  PF %s  %d trades  worst window drawdown %.1f%%" % (
        100 * (wf - 1), yr(wf), pf(wf_pnl), len(wf_pnl), worst_dd))
    print("  as configured  %+7.1f%%  (%+.1f%%/yr)  PF %s  %d trades  (thresholds %.2f / %.1f from config.json)" % (
        100 * (cf - 1), yr(cf), pf(cf_pnl), len(cf_pnl), cfg["entry_threshold"], cfg["exit_threshold"]))
    print("  buy & hold     %+7.1f%%  (%+.1f%%/yr)  equal weight, same windows" % (100 * (bh - 1), yr(bh)))
    stable = max(set(picks), key=picks.count)
    print("  most picked thresholds: %.2f / %.1f (%d of %d windows)" % (stable + (picks.count(stable), len(picks))))
    print("  funding paid: $%+.2f over all windows (negative = received)" % fund_usd)
    lk = luck(wf_pnl)
    print("  luck check: t = %+.2f, %.0f%% of resampled trade sets lose money -> %s" % (
        lk["t"], 100 * lk["p_loss"], "unlikely to be luck" if lk["t"] >= 2 else
        "can't tell apart from luck yet (needs t above ~2)"))


if __name__ == "__main__":
    main()
