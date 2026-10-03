"""Pure strategy math: indicators -> factor scores in [-1, 1] -> one blended score -> position size.
No network here, so the backtest and the live bot run the exact same logic."""
from __future__ import annotations

import math


def ema(xs, n):
    k, e, out = 2 / (n + 1), xs[0], []
    for x in xs:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    """Wilder RSI of the last bar."""
    ag = sum(max(closes[i] - closes[i - 1], 0) for i in range(1, n + 1)) / n
    al = sum(max(closes[i - 1] - closes[i], 0) for i in range(1, n + 1)) / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag, al = (ag * (n - 1) + max(d, 0)) / n, (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(h, l, c, n=14):
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def technical(candles, ind):
    """candles: oldest..newest closed bars {o,h,l,c,v}. Returns (factors, info)."""
    c = [x["c"] for x in candles]
    a = atr([x["h"] for x in candles], [x["l"] for x in candles], c, ind["atr_len"]) or 1e-12
    ef, es = ema(c, ind["ema_fast"])[-1], ema(c, ind["ema_slow"])[-1]
    r = rsi(c, ind["rsi_len"])
    m = ind["mom_len"]
    return {
        "trend": math.tanh((ef - es) / a),  # EMA gap measured in ATRs
        "momentum": math.tanh((c[-1] - c[-1 - m]) / (a * math.sqrt(m))),  # move vs. random-walk expectation
        "mean_reversion": -(r - 50) / 50,  # overbought -> negative
    }, {"price": c[-1], "atr": a, "rsi": r, "ema_fast": ef, "ema_slow": es}


def factor_funding(hourly_rate):
    """Contrarian: high positive funding = crowded longs paying shorts. 50% APR -> ~-0.76."""
    return -math.tanh(hourly_rate * 24 * 365 / 0.5)


def factor_fear_greed(value):
    """Contrarian: extreme greed (100) -> -1, extreme fear (0) -> +1."""
    return (50 - value) / 50


def combine(factors, weights):
    """Weighted average of available factors (None = unavailable, skipped). Result in [-1, 1]."""
    num = den = 0.0
    for k, v in factors.items():
        w = weights.get(k, 0)
        if v is None or not w:
            continue
        num += w * v
        den += abs(w)
    return num / den if den else 0.0


def blend_ai(rules_score, ai_probs, weight):
    if not ai_probs:
        return rules_score
    return (1 - weight) * rules_score + weight * (ai_probs["long"] - ai_probs["short"])


def size_position(equity, price, atr_, risk, fees, sz_decimals):
    """Risk-based sizing: lose `risk_per_trade_pct` of equity if the ATR stop hits.
    Returns (size, sl_dist, tp_dist, skip_reason)."""
    sl_d, tp_d = atr_ * risk["sl_atr"], atr_ * risk["tp_atr"]
    if atr_ / price * 100 < risk["min_atr_pct"]:
        return 0, sl_d, tp_d, "too quiet: ATR %.3f%% < min_atr_pct" % (atr_ / price * 100)
    roundtrip_pct = 2 * (fees["taker_pct"] + fees["slippage_pct"])
    if tp_d / price * 100 < roundtrip_pct * risk["fee_multiple"]:
        return 0, sl_d, tp_d, "target %.3f%% doesn't beat fees x%s" % (tp_d / price * 100, risk["fee_multiple"])
    size = equity * risk["risk_per_trade_pct"] / 100 / sl_d
    size = min(size, equity * risk["max_leverage"] / price, risk["max_position_usd"] / price)
    size = math.floor(size * 10 ** sz_decimals) / 10 ** sz_decimals
    if size * price < 10.5:  # Hyperliquid rejects orders under $10 notional
        return 0, sl_d, tp_d, "notional $%.2f under Hyperliquid $10 minimum" % (size * price)
    return size, sl_d, tp_d, None
