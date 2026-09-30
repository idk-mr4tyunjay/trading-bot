"""python test_strategy.py  -- offline checks of the strategy math and the backtester."""
import json
import math

import strategy
from backtest import backtest

cfg = json.load(open("config.json"))

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
print("ok")
