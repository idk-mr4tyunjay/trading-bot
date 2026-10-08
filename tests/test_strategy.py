"""python tests/test_strategy.py  -- offline checks of the strategy math and the backtester."""
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import strategy
from backtest import backtest

cfg = json.load(open(os.path.join(os.path.dirname(__file__), '..', 'config.json')))

# indicators
assert strategy.rsi([float(i) for i in range(30)]) == 100.0
assert abs(strategy.rsi([100 + (i % 2) for i in range(60)]) - 50) < 5
assert abs(strategy.atr([2.0] * 20, [1.0] * 20, [1.5] * 20) - 1.0) < 1e-9
assert strategy.factor_fear_greed(0) == 1 and strategy.factor_fear_greed(100) == -1
assert strategy.factor_funding(0.0001) < 0 < strategy.factor_funding(-0.0001)

# combine skips missing factors and zero weights
assert strategy.combine({"a": 1.0, "b": None, "c": -1.0}, {"a": 1, "b": 5, "c": 0}) == 1.0
assert strategy.blend_ai(0.5, {"long": 0.1, "short": 0.9, "flat": 0}, 0.5) == 0.5 * 0.5 + 0.5 * -0.8

# sizing: 1% risk of $100 with $2 stop -> 0.5 coins, capped by leverage/max notional, skips < $10
fees, rk = cfg["fees"], dict(cfg["risk"], min_atr_pct=0.1, fee_multiple=1)
size, sl, tp, why = strategy.size_position(100, 100, 1.0, rk, fees, 3)
assert why is None and abs(size - 0.5) < 1e-9 and sl == 2.0, (size, why)
assert strategy.size_position(5, 100, 1.0, rk, fees, 3)[3].startswith("notional")
assert "quiet" in strategy.size_position(100, 100, 0.01, rk, fees, 3)[3]

# backtest: a clean uptrend should produce a profitable long, a downtrend a profitable short
up = [{"t": i, "o": 100 * 1.004 ** i, "c": 100 * 1.004 ** (i + 1), "h": 100 * 1.004 ** (i + 1) * 1.003,
       "l": 100 * 1.004 ** i * 0.997, "v": 1} for i in range(400)]
r = backtest(up, cfg, 100, 5)
assert r["trades"] >= 1 and r["return_pct"] > 0, r
down = [dict(b, o=1e4 / b["o"], c=1e4 / b["c"], h=1e4 / b["l"], l=1e4 / b["h"]) for b in up]
r = backtest(down, cfg, 100, 5)
assert r["trades"] >= 1 and r["return_pct"] > 0, r

# like_live: historical funding / Fear & Greed feed the score. Crowded longs + extreme greed veto the uptrend.
H = 14_400_000
up4h = [dict(b, t=b["t"] * H) for b in up]
base = backtest(up4h, cfg, 100, 5)
assert backtest(up4h, cfg, 100, 5, {"funding": [(0, 0.0)], "fear_greed": [(0, 50)]})["trades"] == base["trades"]
assert backtest(up4h, cfg, 100, 5, {"funding": [(0, 0.0005)], "fear_greed": [(0, 100)]})["trades"] == 0
# max_hold_hours closes positions like the live bot, so a short hold means more round trips
assert backtest(up4h, dict(cfg, risk=dict(cfg["risk"], max_hold_hours=8)), 100, 5)["trades"] > base["trades"]

# several coins share one account: max_open_positions is enforced, earlier coins (config order) get the slot first
from backtest import prepare, simulate
rows = prepare(up4h, cfg)
one = simulate({"A": rows, "B": rows}, dict(cfg, risk=dict(cfg["risk"], max_open_positions=1)), 100, {"A": 5, "B": 5})
assert set(one["per_coin"]) == {"A"}, one
two = simulate({"A": rows, "B": rows}, dict(cfg, risk=dict(cfg["risk"], max_open_positions=2)), 100, {"A": 5, "B": 5})
assert set(two["per_coin"]) == {"A", "B"}, two
# a window only trades inside it, and close_at_end books the open position
mid = rows[len(rows) // 2]["t"]
w = simulate({"A": rows}, cfg, 100, {"A": 5}, start=mid, close_at_end=True)
assert 0 < w["trades"] < base["trades"], w

# live fills shown on the dashboard: side is the position's direction (a sell that closes a long is "long"), pnl net of fee
from broker import HyperliquidBroker
hb = object.__new__(HyperliquidBroker)
hb.addr = "0x0"
hb.info = type("Info", (), {"user_fills": lambda self, addr: [  # newest first, like the API
    {"coin": "BTC", "px": "100", "side": "A", "dir": "Close Long", "closedPnl": "5", "fee": "0.5", "time": 2000},
    {"coin": "BTC", "px": "90", "side": "B", "dir": "Open Long", "closedPnl": "0", "fee": "0.4", "time": 1000}]})()
fills = hb.closed()
assert [f["side"] for f in fills] == [1, 1] and fills[-1]["pnl"] == 4.5 and fills[-1]["at"] == 2.0, fills

# LLM answers are normalized over the allowed options; junk is rejected
import ai
q = {"x": {"type": "choice", "criteria": {"long": "", "short": "", "flat": ""}}}
assert ai.parse_llm('ok ```json\n{"x": {"long": 3, "short": 1, "flat": 0, "moon": 9}}```', q)["x"] == {"type": "choice", "choice": "long", "probabilities": {"long": 0.75, "short": 0.25, "flat": 0.0}}
try:
    ai.parse_llm('{"x": {}}', q)
    raise AssertionError("empty answer accepted")
except ValueError:
    pass
# noul (yes/no) answers from an LLM come back in TypeSafe's shape
nq = {"r": {"type": "noul", "criteria": {"true": "", "false": ""}}}
assert ai.parse_llm('{"r": {"true": 0.2, "false": 0.6}}', nq)["r"] == {"type": "noul", "noul": 0.25}

# TypeSafe path end to end with a canned API response: probabilities, noul, confidence gate
import os
os.environ["TYPESAFE_AI_API_KEY"] = "test"
ts = dict(cfg["ai"], provider="typesafe", model="jev-latest", min_confidence=0.5)
ai._post = lambda url, key, body, timeout: {"model": "jev-1.13.0", "usage": {"input_tokens": 500}, "answers": {
    "position": {"type": "choice", "choice": "long", "probabilities": {"long": 0.7, "short": 0.1, "flat": 0.2}, "confidence": 0.6},
    "sentiment": {"type": "choice", "choice": "bullish", "probabilities": {"bearish": 0.1, "neutral": 0.3, "bullish": 0.6}, "confidence": 0.4},
    "risk_event": {"type": "noul", "noul": 0.05}}}
st = ai.coin_state("BTC", "4h", None, {"trend": 0.7, "news": None}, 0.3,
                   {"price": 100.0, "atr": 1.5, "rsi": 72, "ema_fast": 101, "ema_slow": 99}, 0.0001, 80, ["h"])
assert st["signals"] == {"trend": "strongly bullish"} and st["rsi"] == "overbought" and st["market_mood"] == "extreme greed"
assert not any(isinstance(v, float) for v in st.values()), "no raw numbers for the model"
assert ai.coin_decision(st, ts, "1 day") == {"long": 0.7, "short": 0.1, "flat": 0.2, "confidence": 0.6}
assert ai.coin_decision(st, dict(ts, min_confidence=0.9), "1 day") is None
sent, risk = ai.news_view(["h"], ts)
assert abs(sent - 0.5) < 1e-9 and risk == 0.05 and ai.spend["model"] == "jev-1.13.0"

# UI config validation: same shape/types, sane bounds, real coins
import copy
import ui
ok = copy.deepcopy(cfg)
assert ui.validate(ok, cfg, {"BTC", "ETH", "SOL"}) is None
for mutate, expect in [(lambda c: c["risk"].update(risk_per_trade_pct=50), "between"),
                       (lambda c: c.update(coins=["BTC", "NOPE"]), "unknown coins"),
                       (lambda c: c.update(mode="yolo"), "mode"),
                       (lambda c: c["risk"].pop("sl_atr"), "keys changed"),
                       (lambda c: c.update(allow_short="yes"), "wrong type")]:
    bad = copy.deepcopy(cfg)
    mutate(bad)
    assert expect in (ui.validate(bad, cfg, {"BTC", "ETH", "SOL"}) or ""), expect
# funding: longs pay positive funding while open, shorts receive it; the backtest books it into equity
from backtest import Funding, luck
H1 = 3_600_000
assert abs(Funding([(1, 0.1), (2, 0.2), (3, 0.3)]).paid(1, 3) - 0.5) < 1e-12  # (t0, t1]
fund = [(t, 0.0001) for t in range(0, up4h[-1]["t"] + H, H1)]  # 0.01%/h, ~88%/yr
free = simulate({"A": rows}, cfg, 100, {"A": 5})
paid = simulate({"A": rows}, cfg, 100, {"A": 5}, funding={"A": fund})
assert paid["funding_paid_usd"] > 0 and paid["return_pct"] < free["return_pct"], (paid, free)
down4h = [dict(b, t=b["t"] * H) for b in down]
got = simulate({"A": prepare(down4h, cfg)}, cfg, 100, {"A": 5}, funding={"A": fund})
assert got["funding_paid_usd"] < 0, got  # the short collected it

# take profit is a resting limit: maker fee, no slippage. Without maker_pct it falls back to taker + slippage
taker_cfg = dict(cfg, fees={k: v for k, v in cfg["fees"].items() if k != "maker_pct"})
assert simulate({"A": rows}, cfg, 100, {"A": 5})["fees_paid_usd"] < simulate({"A": rows}, taker_cfg, 100, {"A": 5})["fees_paid_usd"]

# luck check: a steady winner is unlikely to be chance, a coin flip is
assert luck([1.0, -0.5] * 30)["t"] > 2 and luck([1.0, -0.5] * 30)["p_loss"] < 0.05
assert abs(luck([1.0, -1.0] * 30)["t"]) < 1e-9 and luck([-1.0, 0.5] * 30)["p_loss"] > 0.95

# Binance's 8-hourly funding becomes hourly rates, placed after it's paid (no peeking)
import data
hf = data.hourly_funding([(0, 0.0008), (8 * H1, 0.0004)])
assert len(hf) == 16 and hf[0] == (0, 0.0001) and hf[8] == (8 * H1, 0.00005) and abs(sum(r for _, r in hf) - 0.0012) < 1e-12

# paper broker: charges funding pro rata, and fills the take profit at its price with the maker fee
import tempfile
from broker import PaperBroker
tmp = tempfile.mkdtemp()
pb = PaperBroker(os.path.join(tmp, "p.json"), 100, cfg["fees"])
pb.open("BTC", True, 0.001, 50000, 49000, 52000, 3, 5)
pb.s["positions"]["BTC"]["fund_t"] -= 3600  # opened an hour ago
pb.accrue_funding({"BTC": 0.0001}, {"BTC": 50000})
assert abs(pb.s["positions"]["BTC"]["funding"] - 0.005) < 1e-6, pb.s  # 0.001 BTC x $50k x 0.01%
pb.check_stops({"BTC": 52100})
c = pb.closed()[-1]
assert c["exit"] == 52000 and c["reason"] == "take profit" and abs(c["pnl"] - (2.0 - 52 * cfg["fees"]["maker_pct"] / 100)) < 1e-3, c

# live broker: stop is an exchange trigger, target a resting reduce-only Gtc limit at the target price
orders = []
ok = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}
hb2 = object.__new__(HyperliquidBroker)
hb2.addr, hb2.path, hb2.opened = "0x0", os.path.join(tmp, "o.json"), {}
hb2.info = type("Info", (), {"open_orders": lambda self, a: []})()
hb2.ex = type("Ex", (), {
    "update_leverage": lambda self, *a, **k: None,
    "market_open": lambda self, *a: {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"totalSz": "0.001", "avgPx": "50000"}}]}}},
    "order": lambda self, coin, is_buy, sz, px, typ, reduce_only: orders.append((is_buy, sz, px, typ, reduce_only)) or ok})()
assert hb2.open("BTC", True, 0.001, 50000, 49000, 52000, 3, 5)
(sl_o, tp_o) = orders
assert sl_o[3]["trigger"]["tpsl"] == "sl" and sl_o[2] == 49000, sl_o
assert tp_o[3] == {"limit": {"tif": "Gtc"}} and tp_o[2] == 52000, tp_o
assert all(o[0] is False and o[1] == 0.001 and o[4] is True for o in orders), orders

# funding carry backtest: in only while funding pays, earns ~funding x lev/(lev+1) on capital, minus costs
import carry
flat = [{"t": i * H, "o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0, "v": 1} for i in range(2400)]  # 400 days of 4h
rate = lambda apr: [(t, apr / 8760) for t in range(0, 2400 * H, H1)]
assert abs(carry.trailing_apr(Funding(rate(0.2)), 30 * 86_400_000, 7) - 20) < 1e-6
r = carry.simulate_carry({"A": (flat, rate(0.2))})
assert r["trades"] == 1 and 10 < r["per_year_pct"] < 20 * 2 / 3, r
r = carry.simulate_carry({"A": (flat, rate(0.02))})
assert r["trades"] == 0 and r["return_pct"] == 0, r
flip = [(t, (0.2 if t < 1200 * H else -0.1) / 8760) for t, _ in rate(0)]
r = carry.simulate_carry({"A": (flat, flip)})
assert r["trades"] == 1 and r["in_market_pct"] < 55 and r["return_pct"] > 0, r  # left when funding turned
rising = [dict(b, o=100 * 1.3 ** (i / 2400), h=100 * 1.3 ** ((i + 1) / 2400), c=100 * 1.3 ** ((i + 1) / 2400))
          for i, b in enumerate(flat)]
r = carry.simulate_carry({"A": (rising, rate(0.2))})
assert r["rebalances"] == 1 and r["liquidations"] == 0, r
# a 10x rally must not inflate the income: rebalancing resizes the pair back to the account
moon = [dict(b, o=100 * 10 ** (i / 2400), h=100 * 10 ** ((i + 1) / 2400), c=100 * 10 ** ((i + 1) / 2400))
        for i, b in enumerate(flat)]
r = carry.simulate_carry({"A": (moon, rate(0.2))})
assert r["rebalances"] >= 8 and r["per_year_pct"] < 20 * 2 / 3 * 1.25, r  # at most the 25% drift between rebalances

# OpenRouter: thinking is switched off (DeepSeek V4 thinks by default), models go as a fallback list
sent = []
ai._post = lambda url, key, body, timeout: sent.append(body) or {"model": "m", "choices": [{"message": {
    "content": '{"sentiment": {"bullish": 1}, "risk_event": {"true": 0, "false": 1}}'}}]}
os.environ["OPENROUTER_API_KEY"] = "test"
assert ai.news_view(["h"], dict(cfg["ai"], provider="openrouter")) == (1.0, 0.0)
assert sent[-1]["reasoning"] == {"enabled": False} and sent[-1]["models"] == cfg["ai"]["model"], sent[-1]
print("ok")
