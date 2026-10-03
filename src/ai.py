"""AI second opinion. Two providers, same typed questions ({id: {type: "choice", instructions, criteria}}):
  typesafe    Jev System One REST (same endpoint/body as @ai-sdk/typesafe-ai): calibrated probabilities, ~100 ms.
  openrouter  any chat LLM via OpenRouter, asked to return the same probabilities as JSON (slower, less calibrated).
Per TypeSafe's docs (Jev 1.13 jaggedness), keep numbers and math in code and hand the model named buckets."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger("ai")
spend = {"calls": 0, "usd": 0.0, "model": None}
SYSTEM = ("You are a disciplined, risk-averse crypto trading analyst. For each question, assign a probability to every "
          "option in its criteria. Reply with JSON only, shaped {\"<question id>\": {\"<option>\": probability}}. "
          "Each question's probabilities sum to 1. Be calibrated: stay near uniform when the evidence is weak.")


def _post(url, key, body, timeout):
    req = urllib.request.Request(url, json.dumps(body).encode(), {
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "X-Title": "trading-bot"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if attempt == 2 or e.code not in (429, 529):  # rate limited / overloaded: one backoff retry
                raise
            time.sleep(float(e.headers.get("retry-after") or 3))


def parse_llm(text, questions):
    """LLM text -> the same answer shapes TypeSafe returns (choice/probabilities, or noul), normalized. Raises if unusable."""
    raw = json.loads(text[text.index("{"):text.rindex("}") + 1])
    out = {}
    for qid, q in questions.items():
        p = {k: max(float(raw[qid].get(k, 0)), 0.0) for k in q["criteria"]}
        total = sum(p.values())
        if not total:
            raise ValueError("no probabilities for " + qid)
        p = {k: v / total for k, v in p.items()}
        out[qid] = ({"type": "noul", "noul": p["true"]} if q["type"] == "noul" else
                    {"type": "choice", "choice": max(p, key=p.get), "probabilities": p})
    return out


def evaluate(state, questions, cfg):
    t0 = time.time()
    try:
        if cfg["provider"] == "typesafe":
            key = os.environ.get("TYPESAFE_AI_API_KEY")
            if not key:
                return None
            res = _post("https://api.typesafe.ai/v1/systemone", key,
                        {"model": cfg["model"], "state": state, "questions": questions}, cfg["timeout_s"])
            spend["usd"] += ((res.get("usage") or {}).get("input_tokens") or 0) * 0.042e-6
            answers = res["answers"]
            spend["model"] = res.get("model")  # versioned id behind the alias, e.g. jev-1.13.0
        else:
            key = os.environ.get("OPENROUTER_API_KEY")
            if not key:
                return None
            m = cfg["model"]
            res = _post("https://openrouter.ai/api/v1/chat/completions", key, dict(
                {"models": m} if isinstance(m, list) else {"model": m},  # a list = OpenRouter tries them in order
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": json.dumps({"state": state, "questions": questions})}],
                response_format={"type": "json_object"}, temperature=0), cfg["timeout_s"])
            spend["usd"] += (res.get("usage") or {}).get("cost") or 0
            answers = parse_llm(res["choices"][0]["message"]["content"], questions)
            spend["model"] = res.get("model")
    except Exception as e:
        log.warning("%s call failed (%s); rules only this time", cfg["provider"], e)
        return None
    spend["calls"] += 1
    log.debug("%s %.0f ms", cfg["provider"], (time.time() - t0) * 1000)
    return answers


def word(v):
    """-1..1 signal -> named bucket (Jev reads words far better than numbers)."""
    return ("strongly bearish" if v <= -0.6 else "bearish" if v <= -0.2 else "neutral" if v < 0.2
            else "bullish" if v < 0.6 else "strongly bullish")


def coin_state(coin, interval, pos, factors, rules, info, hourly_funding, fng, titles):
    """All the math happens here, in code; the model only sees labelled judgments."""
    apr = hourly_funding * 24 * 365 * 100
    atr_pct = info["atr"] / info["price"] * 100
    r = info["rsi"]
    return {
        "market": "%s perpetual on Hyperliquid" % coin,
        "timeframe": "%s candles" % interval,
        "current_position": "long" if pos and pos["side"] > 0 else "short" if pos else "flat",
        "rule_based_view": word(rules),
        "signals": {k: word(v) for k, v in factors.items() if v is not None},
        "trend": "fast moving average above slow (uptrend)" if info["ema_fast"] > info["ema_slow"] else "fast moving average below slow (downtrend)",
        "rsi": "oversold" if r < 30 else "weak" if r < 45 else "neutral" if r <= 55 else "strong" if r <= 70 else "overbought",
        "volatility": "low" if atr_pct < 1 else "normal" if atr_pct < 3 else "high",
        "funding": ("negative: shorts are crowded and pay longs" if apr < 0 else "normal" if apr < 15
                    else "elevated: longs are crowded" if apr < 50 else "extreme: longs are very crowded"),
        "market_mood": None if fng is None else ("extreme fear" if fng < 25 else "fear" if fng < 45 else "neutral" if fng <= 55
                                                 else "greed" if fng <= 75 else "extreme greed"),
        "news_headlines": titles[:10],
    }


def coin_decision(state, cfg, horizon):
    """-> {"long": p, "short": p, "flat": p, "confidence": c or None} or None (no key, failure, or low confidence)."""
    q = {"position": {
        "type": "choice",
        "instructions": {
            "question": "Over the next %s, should a small account be long, short, or flat on `market`?" % horizon,
            "goal": "Only take a side when a meaningful move in that direction is clearly more likely than a move against it. When the signals disagree or are weak, choose flat.",
            "inputs": "`rule_based_view` and `signals` summarize technical indicators. `funding` shows crowding: crowded sides tend to get squeezed. `market_mood` is the crypto Fear & Greed index, where extremes often reverse. `news_headlines` are the latest crypto headlines.",
        },
        "criteria": {
            "long": "A meaningful rise over the horizon is clearly more likely than a meaningful fall.",
            "short": "A meaningful fall over the horizon is clearly more likely than a meaningful rise.",
            "flat": "Signals conflict or are weak, or the risk is too high to hold a position.",
        },
    }}
    a = evaluate(state, q, cfg)
    if not a:
        return None
    pos = a["position"]
    conf = pos.get("confidence")
    if conf is not None and conf < cfg.get("min_confidence", 0):
        log.info("ai answer ignored: confidence %.2f < min_confidence", conf)
        return None
    p = pos.get("probabilities") or {pos["choice"]: 1.0}
    return dict({k: p.get(k, 0.0) for k in ("long", "short", "flat")}, confidence=conf)


def news_view(titles, cfg):
    """-> (sentiment -1..1, risk_event_probability) or (None, 0)."""
    if not titles:
        return None, 0.0
    q = {
        "sentiment": {
            "type": "choice",
            "instructions": "What is the overall short-term (next 24 hours) implication of `news_headlines` for crypto prices?",
            "criteria": {"bearish": "Mostly negative for crypto prices.", "neutral": "Mixed or not price relevant.",
                         "bullish": "Mostly positive for crypto prices."},
        },
        "risk_event": {
            "type": "noul",
            "instructions": "Does any headline in `news_headlines` report an acute crash-risk event happening right now: a major exchange hack or insolvency, a stablecoin losing its peg, a sudden regulatory ban, or a market-wide liquidation cascade?",
            "criteria": {"true": "At least one headline reports such an event happening now.",
                         "false": "No headline reports such an event happening now; routine news, opinion, and past events do not count."},
        },
    }
    a = evaluate({"news_headlines": titles}, q, cfg)
    if not a:
        return None, 0.0
    s = a["sentiment"].get("probabilities") or {a["sentiment"]["choice"]: 1.0}
    return s.get("bullish", 0) - s.get("bearish", 0), a["risk_event"]["noul"]


if __name__ == "__main__":  # python src/ai.py  -> one live news + BTC call with your key
    import sys
    import data
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root: config.json, .env, state/, logs/
    for line in open(".env") if os.path.exists(".env") else []:
        k, sep, v = line.strip().partition("=")
        if sep and not k.startswith("#"):
            os.environ.setdefault(k, v)
    logging.basicConfig(level=logging.DEBUG)
    cfg = json.load(open("config.json"))
    t = data.headlines(cfg["news"]["feeds"], 60)
    t0 = time.time()
    print("news (sentiment, risk):", news_view(t, cfg["ai"]), "%.1fs" % (time.time() - t0))
    st = coin_state("BTC", "4h", None, {"trend": 0.6, "momentum": -0.4}, 0.2,
                    {"price": 100.0, "atr": 1.5, "rsi": 58, "ema_fast": 101, "ema_slow": 99}, 0.0000125, 74, t)
    print("BTC:", coin_decision(st, cfg["ai"], "1 day"))
    print("spend:", spend)
