"""Two brokers, same methods: equity(mids), positions(), open(...), close(...), check_stops(mids), accrue_funding(...).
Paper simulates fills at mid minus fees/slippage, and charges funding. Hyperliquid trades for real via an API (agent)
wallet, which can place/cancel orders but can NEVER withdraw funds."""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("broker")


def round_px(px, sz_decimals):
    """Hyperliquid perps: max 5 significant figures and max (6 - szDecimals) decimals."""
    return round(float("%.5g" % px), 6 - sz_decimals)


class PaperBroker:
    # ponytail: fills at mid +/- slippage, stops checked at poll time (gaps not modelled), funding accrued per poll
    def __init__(self, path, start_equity, fees):
        self.path, self.cost = path, (fees["taker_pct"] + fees["slippage_pct"]) / 100
        self.maker = fees.get("maker_pct", fees["taker_pct"]) / 100  # take profit rests on the book, like live
        try:
            with open(path) as f:
                self.s = json.load(f)
        except FileNotFoundError:
            self.s = {"cash": start_equity, "positions": {}, "closed": []}

    def _save(self):
        with open(self.path + ".tmp", "w") as f:
            json.dump(self.s, f, indent=1)
        os.replace(self.path + ".tmp", self.path)

    def equity(self, mids):
        return self.s["cash"] + sum(p["side"] * (mids.get(c, p["entry"]) - p["entry"]) * p["size"]
                                    for c, p in self.s["positions"].items())

    def positions(self):
        return self.s["positions"]

    def open(self, coin, is_buy, size, px, sl, tp, leverage, sz_decimals):
        self.s["cash"] -= size * px * self.cost
        now = time.time()
        self.s["positions"][coin] = {"side": 1 if is_buy else -1, "size": size, "entry": px,
                                     "sl": sl, "tp": tp, "opened": now, "funding": 0.0, "fund_t": now}
        self._save()
        return True

    def close(self, coin, px, reason, maker=False):
        p = self.s["positions"].pop(coin)
        pnl = p["side"] * (px - p["entry"]) * p["size"] - p["size"] * px * (self.maker if maker else self.cost)
        self.s["cash"] += pnl
        self.s["closed"].append({"coin": coin, "side": p["side"], "entry": p["entry"], "exit": px, "pnl": round(pnl, 4),
                                 "funding": round(p.get("funding", 0.0), 4), "reason": reason, "at": time.time()})
        self._save()
        log.info("PAPER close %s @ %s pnl $%.2f (%s)", coin, px, pnl, reason)

    def closed(self):
        return self.s["closed"]

    def check_stops(self, mids):
        for coin, p in list(self.s["positions"].items()):
            m = mids.get(coin)
            if m is None:
                continue
            if (m - p["sl"]) * p["side"] <= 0:
                self.close(coin, m, "stop loss")
            elif (m - p["tp"]) * p["side"] >= 0:
                self.close(coin, p["tp"], "take profit", maker=True)  # a resting limit fills at its own price

    def accrue_funding(self, rates, mids):
        """rates: {coin: hourly funding rate}. Longs pay positive funding and shorts receive it, pro rata to the time
        since the last call (Hyperliquid settles hourly; per poll is close enough)."""
        now, changed = time.time(), False
        for coin, p in self.s["positions"].items():
            r, m = rates.get(coin), mids.get(coin)
            since = p.setdefault("fund_t", now)  # positions from before funding was simulated start accruing now
            if r is None or m is None:
                continue
            amt = p["side"] * p["size"] * m * r * (now - since) / 3600
            self.s["cash"] -= amt
            p["funding"] = p.get("funding", 0.0) + amt
            p["fund_t"], changed = now, True
        if changed:
            self._save()


class HyperliquidBroker:
    def __init__(self, testnet, path):
        import eth_account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.addr = os.environ["HL_ACCOUNT_ADDRESS"]  # your main wallet (holds funds)
        agent = eth_account.Account.from_key(os.environ["HL_AGENT_PRIVATE_KEY"])  # API wallet (trade-only)
        self.ex = Exchange(agent, url, account_address=self.addr)
        self.info = Info(url, skip_ws=True)
        self.path = path  # remembers open times for max_hold
        try:
            with open(path) as f:
                self.opened = json.load(f)
        except FileNotFoundError:
            self.opened = {}

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self.opened, f)

    def equity(self, mids):
        return float(self.info.user_state(self.addr)["marginSummary"]["accountValue"])

    def positions(self):
        out = {}
        for ap in self.info.user_state(self.addr)["assetPositions"]:
            p = ap["position"]
            szi = float(p["szi"])
            if szi:
                out[p["coin"]] = {"side": 1 if szi > 0 else -1, "size": abs(szi), "entry": float(p["entryPx"]),
                                  "opened": self.opened.get(p["coin"])}  # None: opened outside the bot, time unknown
        return out

    def _cancel_all(self, coin):
        for o in self.info.open_orders(self.addr):
            if o["coin"] == coin:
                self.ex.cancel(coin, o["oid"])

    def open(self, coin, is_buy, size, px, sl, tp, leverage, sz_decimals):
        self._cancel_all(coin)  # never let an old reduce-only stop fire on a new position
        self.ex.update_leverage(leverage, coin, is_cross=False)  # isolated margin: one bad trade can't eat the account
        r = self.ex.market_open(coin, is_buy, size, None, 0.01)
        st = r.get("response", {}).get("data", {}).get("statuses", [{}])[0] if r.get("status") == "ok" else {}
        if "filled" not in st:
            log.error("open %s failed: %s", coin, r)
            return False
        filled = float(st["filled"]["totalSz"])
        # Exchange-side stop and target: they protect you even if this bot/server dies. The stop is a trigger that
        # fires a market order. The target is a plain resting limit (Gtc, not post-only, so it can never be rejected):
        # it fills at its own price as a maker, 0.015% instead of 0.045% plus slippage.
        sl_px, tp_px = round_px(sl, sz_decimals), round_px(tp, sz_decimals)
        for kind, t, typ in (("sl", sl_px, {"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}}),
                             ("tp", tp_px, {"limit": {"tif": "Gtc"}})):
            res = self.ex.order(coin, not is_buy, filled, t, typ, reduce_only=True)
            got = res.get("response", {}).get("data", {}).get("statuses", [{}])[0] if res.get("status") == "ok" else {}
            if not got or "error" in got:
                log.error("%s order for %s failed: %s", kind, coin, res)
        self.opened[coin] = time.time()
        self._save()
        log.info("LIVE open %s %s %s @ ~%s sl %s tp %s", "long" if is_buy else "short", filled, coin, st["filled"]["avgPx"], sl, tp)
        return True

    def close(self, coin, px, reason):
        self._cancel_all(coin)
        r = self.ex.market_close(coin, slippage=0.01)
        self.opened.pop(coin, None)
        self._save()
        log.info("LIVE close %s (%s): %s", coin, reason, r)

    def closed(self):
        """Recent fills, oldest first. side = the position's direction ("Close Long" -> 1), pnl is net of the fill's fee."""
        def side(f):
            d = f["dir"].split(">")[0]
            return 1 if "Long" in d else -1 if "Short" in d else (1 if f["side"] == "B" else -1)
        return [{"coin": f["coin"], "side": side(f), "exit": float(f["px"]), "pnl": float(f["closedPnl"]) - float(f.get("fee") or 0),
                 "reason": f["dir"], "at": f["time"] / 1000} for f in self.info.user_fills(self.addr)[:30]][::-1]

    def check_stops(self, mids):
        pass  # handled by the exchange-side stop and target orders

    def accrue_funding(self, rates, mids):
        pass  # the exchange settles funding into the account value itself
