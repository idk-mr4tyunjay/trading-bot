"""Hyperliquid trading bot.
    python src/bot.py run   [--config config.json]   trade (paper/testnet/live per config "mode")
    python src/bot.py scan  [--config config.json]   funding-rate carry scanner (read-only)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import queue
import time

import data
import ai
import strategy
import ui
from broker import HyperliquidBroker, PaperBroker

log = logging.getLogger("bot")
HORIZON = {"1m": "30 minutes", "5m": "2 hours", "15m": "6 hours", "30m": "12 hours", "1h": "1 day", "4h": "4 days", "1d": "2 weeks"}


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip())


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(path + ".tmp", path)


def make_broker(cfg, name):
    if cfg["mode"] == "paper":
        return PaperBroker("state/%s.paper.json" % name, cfg["paper_start_equity"], cfg["fees"])
    return HyperliquidBroker(cfg["mode"] == "testnet", "state/%s.opened.json" % name)


def run(cfg, name, cfg_path):
    mode = cfg["mode"]
    assert mode in ("paper", "testnet", "live"), "mode must be paper, testnet or live"
    market = data.Market(testnet=mode == "testnet")
    broker = make_broker(cfg, name)
    st = load_json("state/%s.risk.json" % name, {"day": "", "day_start": 0, "peak": 0, "last_bar": {}})
    st.setdefault("paused", None)
    st.setdefault("equity_hist", [])
    if os.environ.get("UI_PASSWORD"):
        ui.start(cfg_path, os.environ["UI_PASSWORD"], os.environ.get("UI_HOST", "127.0.0.1"), int(os.environ.get("UI_PORT", "8090")))
    last, mids, eq, err = {}, {}, 0.0, None
    decisions = open("logs/%s.decisions.jsonl" % name, "a")
    market.refresh()
    unknown = [c for c in cfg["coins"] if c not in market.meta]
    assert not unknown, "not Hyperliquid perps: %s" % unknown
    log.info("starting %s mode=%s coins=%s interval=%s ai=%s", name, mode, cfg["coins"], cfg["interval"], cfg["ai"]["provider"] if cfg["ai"]["enabled"] else "off")

    while True:
        rk, ind = cfg["risk"], cfg["indicators"]
        try:
            market.refresh()
            mids = market.mids()
            broker.check_stops(mids)
            eq = broker.equity(mids)
            positions = broker.positions()

            # --- account-level risk ---
            today = dt.datetime.utcnow().strftime("%Y-%m-%d")
            if st["day"] != today:
                st["day"], st["day_start"] = today, eq
            st["peak"] = max(st["peak"], eq)
            if eq < st["peak"] * (1 - rk["max_drawdown_pct"] / 100) and not (st["paused"] or "").startswith("kill"):
                log.critical("KILL SWITCH: equity $%.2f is %.0f%% below peak $%.2f. Flattening and pausing.",
                             eq, rk["max_drawdown_pct"], st["peak"])
                for c in positions:
                    broker.close(c, mids.get(c), "kill switch")
                positions = broker.positions()
                st["paused"] = "kill switch: equity %.0f%% below peak. Review, then Resume." % rk["max_drawdown_pct"]
            day_halt = eq < st["day_start"] * (1 - rk["daily_loss_limit_pct"] / 100)

            # --- market-wide context ---
            fng = data.fear_greed() if cfg["weights"].get("fear_greed") else None
            titles = data.headlines(cfg["news"]["feeds"], cfg["news"]["refresh_minutes"] * 60) if cfg["news"]["enabled"] else []
            news_sent, risk_p = (data.cached("news_view", cfg["news"]["refresh_minutes"] * 60,
                                             lambda: ai.news_view(titles, cfg["ai"]))
                                 if cfg["ai"]["enabled"] and titles else (None, 0.0))
            risk_event = risk_p >= cfg["news"]["risk_event_threshold"]
            if risk_event:
                log.warning("news risk event p=%.2f: no new entries", risk_p)
                if cfg["news"]["flatten_on_risk_event"]:
                    for c in list(positions):
                        broker.close(c, mids.get(c), "news risk event")
                    positions = broker.positions()

            for coin in cfg["coins"]:
                candles = market.candles(coin, cfg["interval"], cfg["lookback_bars"] + 1)[:-1]  # closed bars only
                if len(candles) < ind["ema_slow"] + ind["mom_len"] + 2 or st["last_bar"].get(coin) == [candles[-1]["t"], cfg["interval"]]:
                    continue  # decide once per new closed bar
                st["last_bar"][coin] = [candles[-1]["t"], cfg["interval"]]

                factors, info = strategy.technical(candles, ind)
                ctx = market.ctx[coin]
                factors["funding"] = strategy.factor_funding(float(ctx["funding"]))
                factors["orderbook"] = market.book_imbalance(coin) if cfg["weights"].get("orderbook") else None
                factors["fear_greed"] = strategy.factor_fear_greed(fng) if fng is not None else None
                factors["news"] = news_sent
                rules = strategy.combine(factors, cfg["weights"])

                pos = positions.get(coin)
                jp = None
                if cfg["ai"]["enabled"]:
                    jp = ai.coin_decision(ai.coin_state(coin, cfg["interval"], pos, factors, rules, info,
                                                        float(ctx["funding"]), fng, titles),
                                          cfg["ai"], HORIZON.get(cfg["interval"], "several bars"))
                score = strategy.blend_ai(rules, jp, cfg["ai"]["weight"])
                price = mids.get(coin, info["price"])
                decisions.write(json.dumps({"t": int(time.time()), "coin": coin, "price": price, "factors": factors,
                                            "rules": round(rules, 3), "ai": jp, "score": round(score, 3)}) + "\n")
                decisions.flush()
                log.info("%s px=%s score=%+.2f rules=%+.2f ai=%s", coin, price, score, rules,
                         {k: round(v, 2) for k, v in jp.items() if v is not None} if jp else "-")

                last[coin] = {"t": int(time.time()), "price": price, "score": round(score, 3), "rules": round(rules, 3),
                              "ai": jp, "factors": {k: None if v is None else round(v, 3) for k, v in factors.items()},
                              "rsi": round(info["rsi"]), "note": "holding"}
                if pos:  # exits: signal flipped against us, or held too long
                    if pos["side"] * score <= -cfg["exit_threshold"]:
                        broker.close(coin, price, "signal flipped (%+.2f)" % score)
                        last[coin]["note"] = "closed: signal flipped"
                    elif time.time() - pos["opened"] > rk["max_hold_hours"] * 3600:
                        broker.close(coin, price, "max hold time")
                        last[coin]["note"] = "closed: max hold time"
                    continue

                # entries
                why = None
                if st["paused"]:
                    why = "paused"
                elif day_halt:
                    why = "daily loss limit hit"
                elif risk_event:
                    why = "news risk event"
                elif abs(score) < cfg["entry_threshold"]:
                    why = "score below entry_threshold"
                elif score < 0 and not cfg["allow_short"]:
                    why = "shorts disabled"
                elif jp and jp["flat"] >= cfg["ai"]["flat_veto"]:
                    why = "ai says flat (%.2f)" % jp["flat"]
                elif len(broker.positions()) >= rk["max_open_positions"]:
                    why = "max_open_positions"
                if why:
                    log.debug("%s no entry: %s", coin, why)
                    last[coin]["note"] = "no entry: " + why
                    continue
                szd = market.meta[coin]["szDecimals"]
                size, sl_d, tp_d, why = strategy.size_position(eq, price, info["atr"], rk, cfg["fees"], szd)
                if why:
                    log.info("%s skip: %s", coin, why)
                    last[coin]["note"] = "skip: " + why
                    continue
                is_buy = score > 0
                lev = max(1, min(int(rk["max_leverage"]), int(market.meta[coin]["maxLeverage"])))
                sl = price - sl_d if is_buy else price + sl_d
                tp = price + tp_d if is_buy else price - tp_d
                last[coin]["note"] = "open failed, see log"
                if broker.open(coin, is_buy, size, price, sl, tp, lev, szd):
                    last[coin]["note"] = "opened " + ("long" if is_buy else "short")
                    log.info("OPEN %s %s %s ($%.0f) @ %s sl=%.6g tp=%.6g score=%+.2f", "LONG" if is_buy else "SHORT",
                             size, coin, size * price, price, sl, tp, score)

            if not st["equity_hist"] or time.time() - st["equity_hist"][-1][0] > 1800:
                st["equity_hist"] = (st["equity_hist"] + [[int(time.time()), round(eq, 2)]])[-1500:]
            save_json("state/%s.risk.json" % name, st)
            positions = broker.positions()
            ui.snapshot.update({
                "t": time.time(), "name": name, "mode": cfg["mode"], "interval": cfg["interval"], "equity": eq,
                "day_start": st["day_start"], "peak": st["peak"], "paused": st["paused"], "day_halt": day_halt,
                "positions": [dict(p, coin=c, mark=mids.get(c), upnl=p["side"] * (mids.get(c, p["entry"]) - p["entry"]) * p["size"])
                              for c, p in positions.items()],
                "decisions": last, "closed": broker.closed()[-30:][::-1], "equity_hist": st["equity_hist"],
                "fear_greed": fng, "news_sentiment": news_sent, "news_risk": risk_p, "headlines": titles[:8],
                "ai": dict(ai.spend, enabled=cfg["ai"]["enabled"], provider=cfg["ai"]["provider"]),
                "valid_coins": sorted(market.meta), "error": None, "log": tail("logs/%s.log" % name, 40)})
            err = None
            log.info("equity $%.2f | day start $%.2f | peak $%.2f | open %s | ai calls %d ($%.4f)", eq, st["day_start"],
                     st["peak"], list(broker.positions()), ai.spend["calls"], ai.spend["usd"])
        except Exception as e:
            log.exception("loop error (will retry)")
            err = "%s: %s" % (type(e).__name__, e)
            ui.snapshot.update({"t": time.time(), "error": err, "log": tail("logs/%s.log" % name, 40)})

        # sleep until the next poll, waking early for UI commands
        deadline = time.time() + cfg["poll_seconds"]
        while True:
            try:
                c = ui.commands.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                break
            try:
                cmd, coin = c["cmd"], c.get("coin")
                if cmd == "pause":
                    st["paused"] = "paused from UI"
                elif cmd == "resume":
                    st["paused"], st["peak"] = None, eq  # re-arms the kill switch from today's equity
                    for d in last.values():
                        d["note"] = "resumed: next decision on the next closed bar"
                elif cmd == "close" and coin in broker.positions():
                    broker.close(coin, mids.get(coin), "closed from UI")
                elif cmd == "close_all":
                    for p in list(broker.positions()):
                        broker.close(p, mids.get(p), "closed from UI")
                elif cmd == "reset_paper" and cfg["mode"] == "paper":
                    if os.path.exists("state/%s.paper.json" % name):
                        os.remove("state/%s.paper.json" % name)
                    broker = make_broker(cfg, name)
                    st.update(day="", peak=0, paused=None, equity_hist=[])
                elif cmd == "reload":
                    new = load_json(cfg_path, cfg)
                    if new["mode"] != cfg["mode"]:
                        log.warning("mode %s -> %s", cfg["mode"], new["mode"])
                        market, broker = data.Market(testnet=new["mode"] == "testnet"), make_broker(new, name)
                        market.refresh()
                        st.update(day="", peak=0)
                    cfg = new
                save_json("state/%s.risk.json" % name, st)
            except Exception:
                log.exception("UI command %s failed", c)
            break  # refresh the snapshot right away


def tail(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 12000))
            return f.read().decode(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def scan(cfg):
    """Funding carry: long spot + short perp earns funding while price-neutral. Shows the honest net."""
    m = data.Market()
    m.refresh()
    spot_meta, _ = m.info.spot_meta_and_asset_ctxs()
    spot_tokens = {t["name"] for t in spot_meta["tokens"]}
    rows = sorted(((float(c["funding"]) * 24 * 365 * 100, n) for n, c in m.ctx.items() if float(c.get("dayNtlVlm", 0)) > 5e6),
                  reverse=True)[:15]
    entry_exit_cost = 2 * 0.07 + 2 * 0.045  # spot taker in+out, perp taker in+out (% of notional)
    since = int((time.time() - 7 * 86400) * 1000)
    print("%-8s %10s %12s %6s %16s" % ("coin", "APR now", "APR 7d avg", "spot?", "break-even days"))
    for apr, n in rows:
        hist = m.info.funding_history(n, since)
        avg = sum(float(h["fundingRate"]) for h in hist) / max(len(hist), 1) * 24 * 365 * 100
        has_spot = n in spot_tokens or "U" + n in spot_tokens
        be = entry_exit_cost / (avg / 365) if avg > 0 else float("inf")
        print("%-8s %9.1f%% %11.1f%% %6s %16.1f" % (n, apr, avg, "yes" if has_spot else "no", be))
    print("\n$100 at 15% APR = ~$0.04/day. Funding flips negative in sell-offs. Liquidation risk on the short leg if unhedged margin is thin.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "scan"])
    ap.add_argument("--config", default="config.json")
    a = ap.parse_args()
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    load_env()
    os.makedirs("state", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    cfg = load_json(a.config, None)
    name = os.path.splitext(os.path.basename(a.config))[0]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("logs/%s.log" % name)])
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    run(cfg, name, a.config) if a.cmd == "run" else scan(cfg)


if __name__ == "__main__":
    main()
