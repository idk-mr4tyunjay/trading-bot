"""Phone dashboard + remote control. Stdlib only. No auth of its own: it is gated by Cloudflare Access / WireGuard.
The bot loop fills `snapshot` each iteration and drains `commands`; this thread never touches the broker."""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from data import INTERVAL_MS

log = logging.getLogger("ui")
commands = queue.Queue()
snapshot = {}
CMDS = {"pause", "resume", "close", "close_all", "reset_paper", "reload"}
BOUNDS = {"risk_per_trade_pct": (0.05, 10), "max_leverage": (1, 20), "max_drawdown_pct": (1, 90),
          "daily_loss_limit_pct": (0.5, 50), "poll_seconds": (10, 3600), "entry_threshold": (0, 1),
          "exit_threshold": (0, 1), "weight": (0, 1), "flat_veto": (0, 1), "min_confidence": (0, 1), "lookback_bars": (100, 5000),
          "max_open_positions": (1, 20), "sl_atr": (0.2, 20), "tp_atr": (0.2, 50), "timeout_s": (1, 120),
          "paper_start_equity": (1, 1e7), "max_position_usd": (10, 1e7), "refresh_minutes": (1, 1440)}


def validate(new, old, valid_coins):
    """New config must keep the current shape and types, and stay inside sane bounds. -> error string or None."""
    def walk(n, o, path):
        key = path.rsplit(".", 1)[-1]
        if isinstance(o, dict):
            if not isinstance(n, dict) or set(n) != set(o):
                return "%s: keys changed" % path
            return next((e for k in o for e in [walk(n[k], o[k], path + "." + k)] if e), None)
        if isinstance(o, bool):
            ok = isinstance(n, bool)
        elif isinstance(o, (int, float)):
            ok = isinstance(n, (int, float)) and not isinstance(n, bool)
            lo, hi = BOUNDS.get(key, (-1e9, 1e9))
            if ok and not lo <= n <= hi:
                return "%s must be between %s and %s" % (key, lo, hi)
        else:  # str or list of str (ai.model may be either)
            ok = isinstance(n, str) or (isinstance(n, list) and all(isinstance(x, str) for x in n))
        return None if ok else "%s: wrong type" % path
    err = walk(new, old, "config")
    if err:
        return err
    if new["mode"] not in ("paper", "testnet", "live"):
        return "mode must be paper, testnet or live"
    if new["interval"] not in INTERVAL_MS:
        return "interval must be one of " + ", ".join(INTERVAL_MS)
    bad = [c for c in new["coins"] if c not in valid_coins]
    if not new["coins"] or bad:
        return "unknown coins: %s" % bad if bad else "pick at least one coin"
    if new["lookback_bars"] < new["indicators"]["ema_slow"] + new["indicators"]["mom_len"] + 20:
        return "lookback_bars too small for ema_slow + mom_len"
    return None


def start(cfg_path, host, port):
    page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(min(n, 200_000)) or b"{}")

        def do_GET(self):
            if self.path == "/":
                with open(page, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                self._send(200, snapshot)
            elif self.path == "/api/config":
                with open(cfg_path) as f:
                    self._send(200, json.load(f))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            # JSON content type forces a CORS preflight, so other sites can't drive the bot from your browser.
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._send(415, {"error": "content-type must be application/json"})
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if self.path == "/api/config":
                with open(cfg_path) as f:
                    old = json.load(f)
                new = body.get("config")
                err = validate(new, old, set(snapshot.get("valid_coins") or old["coins"]))
                if not err and new["mode"] == "live" and old["mode"] != "live" and body.get("confirm") != "LIVE":
                    err = "switching to live needs confirm=LIVE"
                if err:
                    return self._send(400, {"error": err})
                with open(cfg_path, "w") as f:  # in place, not rename: the file may be a Docker bind mount
                    json.dump(new, f, indent=2)
                log.warning("config changed from UI")
                commands.put({"cmd": "reload"})
                return self._send(200, {"ok": True})
            if self.path == "/api/cmd":
                if body.get("cmd") not in CMDS:
                    return self._send(400, {"error": "unknown command"})
                log.warning("UI command: %s", body)
                commands.put({"cmd": body["cmd"], "coin": str(body.get("coin", ""))})
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("UI on http://%s:%d", host, port)
