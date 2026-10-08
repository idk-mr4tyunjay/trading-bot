# Hyperliquid trading bot (with optional AI: OpenRouter or Jev)

A small-account crypto bot for Hyperliquid perps. It blends technical signals, funding, order book, Fear & Greed
and news into one score. It can optionally ask an AI (any OpenRouter model, or TypeSafe's **Jev** if you get access) for a second opinion and a news risk veto.
Every weight, threshold and risk limit lives in `config.json`. It starts in **paper mode** (no money, no keys).

> Not financial advice. Most retail trading bots lose money. Run paper mode for weeks before risking anything,
> and only risk money you can lose entirely.

---

## 1. Research verdict (checked 2026-09-28)

### Jev / jev-trader / jev-trade.com: is it useful?

| Claim | Reality |
|---|---|
| "Fastest AI model for trading" | Jev is a real new model type (TypeSafe, launched 2026-09-15). It returns **typed choices with probabilities** in 70–500 ms instead of text. At $0.042 per 1M input tokens it is almost free. |
| "HFT with Jev" (jev-trader) | It's a **demo**. It trades MON-USDC on Kuru (Monad), one post-only order every 300 ms block. Its own SPEC.md says "the model is not trying to be profitable", and the dashboard exists to show latency, gas and spread bleed. |
| jev-trade.com | A live dashboard of a bot. It shows no track record and no strategy detail. |
| RohOnChain article | Paywalled (HTTP 402), so I couldn't read it. The linked posts ("it will probably make you a lot of money") are engagement marketing. |
| Trading accuracy | **No published trading benchmarks.** TypeSafe's own evals are non-finance tasks, with ~68% mean agreement against LLM-made reference labels. The docs say Jev can't do arithmetic and breaks on large irrelevant inputs. |
| Access | Early access. **Signups have been paused since 2026-09-22.** I confirmed the REST endpoint (`api.typesafe.ai/v1/systemone`) is live. On OpenRouter, only `typesafe/jev-router` exists. It uses Jev to *pick an LLM* for your chat request and returns text, not Jev's calibrated probabilities. |

**Conclusion for a small trader:** HFT is not your game. Your Oracle box is in Singapore, tens of milliseconds from
Hyperliquid's Tokyo infrastructure. You'd be competing against co-located firms, and at small size, fees are larger
than any per-trade edge. Jev *is* useful for what it's good at: **reading messy inputs (headlines, mixed signals)
fast and cheaply, and returning calibrated probabilities.** This bot uses it that way:
1. It gives a long/short/flat opinion that is blended with the rule score (`ai.weight`), and can veto trades when it says "flat".
2. It classifies news sentiment every hour, which feeds the `news` factor.
3. It flags news risk events (hack, depeg, ban, liquidation cascade), which block new entries or optionally flatten positions.

The bot works fully without AI. The same questions can go to **OpenRouter** (`ai.provider: "openrouter"`) today. The
LLM is asked to return the same probabilities as JSON, and the answer is validated and normalized. That's slower
(seconds rather than 100 ms, which doesn't matter on 4h bars) and less calibrated than Jev. The default is
`deepseek/deepseek-v4-flash` with `google/gemma-4-26b-a4b-it` as the fallback, both paid but cheap: about 40 calls a
day (3 coins × 6 bars, plus news hourly) comes to roughly $0.13 a month. Thinking is switched off in the request,
because DeepSeek V4 reasons by default and would bill the hidden tokens. Free (`:free`) models were tried first, but
their shared upstream pool kept answering 429 (rate limited), and they cap at 50 requests/day without purchased
credits. Paid models need a few dollars of OpenRouter credit. Test your key with `.venv/bin/python src/ai.py`.
A/B it: run `config.json` and a copy with AI enabled side by side in paper mode, then compare `logs/*.decisions.jsonl`.

### Arbitrage: realistic or not?

| Type | For a small account? |
|---|---|
| Cross-exchange price arb | **No.** You pay 0.05–0.1% taker fees on both legs, plus withdrawal and bridge fees and transfer delays, and pros close gaps in milliseconds. |
| Funding carry (long spot + short perp on HL) | **Real, safe-ish, and small right now.** `src/carry.py` backtests it: hold only while the 7-day funding average pays ≥ 15%/yr, leave below 5%. On BTC/ETH/SOL since 2020 that made +7.4%/yr with a 0.6% worst drawdown, but nearly all of it in 2020–21 (+15%, +29%); 2025 was +0.5% and 2026 +0.0%. On Hyperliquid since mid-2024, adding HYPE: +7.5%/yr, 2026 +2.5%. `python src/bot.py scan` shows live numbers. The bot doesn't trade it: Hyperliquid nets one position per coin, so it would need its own sub-account and spot-leg order code. |
| Directional (this bot) | Possible edge on **slow timeframes only**. See the backtest below. |

### Backtest (real Hyperliquid candles, fees and slippage included, $100, 1% risk per trade)

| Timeframe | BTC | ETH | SOL | Verdict |
|---|---|---|---|---|
| 15m (52 days) | −8.4% | −7.9% | +15.7% | noise and fees eat it |
| 1h (208 days) | −18.6% | −30.0% | −15.7% | loses |
| **4h (833 days)** | **+10.4%** (PF 1.13, DD 11.5%) | **+20.7%** (PF 1.21) | **+9.8%** (PF 1.11) | small edge, beat buy-and-hold in ETH/SOL downtrends |

(PF is profit factor: total winnings ÷ total losses. DD is maximum drawdown.)

The default is therefore **4h**. Be honest with yourself about what these numbers mean: this is a thin edge measured
in-sample, and it's sensitive to the thresholds (entry 0.3–0.5 gives PF 1.04–1.30). It's roughly $10–30 over
2+ years on $100. The value for you is learning with controlled risk, not getting rich. The backtest prints two runs:
`technical_only`, and `like_live`, which replays historical funding and Fear & Greed with a neutral order book.
News and Jev/AI have no history, so neither run includes them.

### Walk-forward (out of sample): the honest number

The table above is in-sample: the thresholds were chosen on the same history they're scored on. `src/walkforward.py`
fixes that. It picks entry/exit thresholds on 365 days, trades the **next 90 unseen days** with them, rolls forward,
and reports only the unseen windows. All coins share one account under `max_open_positions`, like the live bot,
with historical funding and Fear & Greed in the score (`like_live`).

5 unseen windows, 2025-07-03 → 2026-09-26 (~450 days), $100, fees included:

| Coins | Max open | Return | Per year | PF | Worst window DD | Trades |
|---|---|---|---|---|---|---|
| **BTC, ETH, SOL** (default) | 2 | **+31.5%** | +24.9% | **1.38** | **12.0%** | 146 |
| Top 15 by volume | 2 | +25.5% | +20.2% | 1.19 | 16.1% | 266 |
| Top 15 by volume | 3 | +20.2% | +16.1% | 1.12 | 22.5% | 383 |
| Top 15 by volume | 4 | +35.0% | +27.5% | 1.14 | 24.8% | 508 |

Top 15 = BTC, ETH, HYPE, ZEC, SOL, NEAR, XRP, SAND, WLD, ZRO, ENA, AAVE, SUI, ONDO, UNI (24h volume on 2026-10-03).
Over the same windows, equal-weight buy and hold was −9.9% for BTC/ETH/SOL and +108% for the top 15. The second
figure is inflated by survivorship: today's most-traded coins are partly the ones that pumped.

**What it says:** the edge survives out of sample on BTC/ETH/SOL, but it's thin (5 windows, 146 trades, one
losing window). Adding coins **didn't help**: every top-15 run has a lower PF and a deeper drawdown, and the one that
returns a bit more (4 open) only does it by doubling the drawdown, since 4 positions put up to 4% at risk at once.
So the default stays at three coins and 2 open positions. The thresholds picked in each window move around (0.40–0.45 entry), so don't read much into any single value.

**Update 2026-10-08: the longer, stricter test.** Those 450 days are too short to trust. Hyperliquid only serves the
latest 5000 bars, so the window slides: five days later the same run gave +21.5% (PF 1.21), not +31.5%. The
walk-forward now defaults to `--source binance`, Binance's USDT perps since 2020, which trade almost exactly like
Hyperliquid's for these coins. Every run now also pays historical funding while a position is open (~1%/yr), takes
profit at the maker fee, and ends with a luck check.

23 unseen windows, 2021-01-10 → 2026-09-11 (~5.7 years), BTC/ETH/SOL, 2 open, $100:

| | Return | Per year | PF | Worst window DD | Trades |
|---|---|---|---|---|---|
| Walk-forward | +135% | **+16.3%** | 1.20 | 14.6% | 821 |
| Buy & hold (equal weight) | +1316% | +59.6% | | (−86% at worst) | |

Luck check: t = 2.37, so this is unlikely to be chance over the whole period. But by year it's lumpy: most of the
profit came in 2020, the 2022 crash and 2023. Since mid-2024 the edge is weak (Hyperliquid's own 450 days:
t = 1.15, which can't be told apart from luck). Shorts are essential: long-only made about 0%/yr. Expect strings of
9–11 losing trades and up to ~9 months below a previous high.

### Costs of running this

| Item | Cost |
|---|---|
| Oracle Always Free ARM | $0. The bot uses ~60 MB RAM and negligible CPU. ⚠️ Oracle cut free A1 to 2 OCPU / 12 GB on 2026-06-15, and your box reports ~23 GiB, so check Billing that you're not being charged. |
| Hyperliquid perps | taker 0.045%, maker 0.015%, no gas. Round trip ≈ 0.09% plus slippage. On a $40 position that's about $0.04. |
| Funding | paid or received hourly while holding a perp (usually ~0.001%/h) |
| Deposit / withdraw | USDC on Arbitrum. Minimum deposit 5 USDC, and withdrawal costs $1. You also need a few cents of ETH on Arbitrum for gas. |
| AI (optional) | DeepSeek V4 Flash via OpenRouter: about **$0.13/month** (about 40 calls/day, thinking off). Jev: similar |
| News, Fear & Greed | free (RSS, alternative.me) |
| **Tax (India)** | 30% flat on crypto gains plus cess. Losses can't be offset against other income. TDS rules apply to transfers. Check with a CA. |

**Minimum sensible capital: about $50.** Hyperliquid rejects orders under $10 notional. With 1% risk and a 4h ATR stop,
accounts under about $25 get most trades skipped, which the bot logs as "under Hyperliquid $10 minimum".

**With $10 (4h backtest, 1 position at a time):**

| Risk per trade | BTC | ETH | SOL |
|---|---|---|---|
| 1% (safe) | 1 trade (every other trade is below the $10 minimum) | 0 trades | 0 trades |
| 3% | +24%, DD 28% | −1%, DD 24% | +16%, DD 25% |
| 5% | +50%, DD 48% | −44%, DD 58% | −25%, DD 47% |

To trade at all with $10, you have to risk 3–5% per trade. At that level the outcome is mostly luck, with 25–58%
drawdowns. Meanwhile the $1 withdrawal fee alone is 10% of the account, before the cost of getting USDC from INR onto
Arbitrum. **Verdict:** keep the $10 and run paper mode with `paper_start_equity: 100` to learn the system, and go live
once you have $50–100 you can afford to lose.

---

## 2. How it works

```
every 60 s:  prices → paper stops → equity → kill switch / daily limit
             Fear&Greed (hourly) + headlines (hourly) → [AI: sentiment, risk event]
  per coin, once per NEW closed candle:
     trend (EMA gap / ATR) · momentum · mean reversion (RSI) · funding (contrarian)
     · order book imbalance · fear & greed (contrarian) · news
        → weighted average = rules score (-1..1)
        → [AI long/short/flat probabilities blended in]
     in position?  exit on signal flip or max hold time
     flat?         enter if |score| ≥ entry_threshold and all risk gates pass
                   size = 1% equity ÷ (2×ATR stop), capped by leverage / max_position_usd
                   live: stop-loss + take-profit placed ON THE EXCHANGE (they survive if the bot dies)
```

**Safety built in:**
- An **API (agent) wallet**: the bot's key can trade but can never withdraw your money.
- Isolated margin per position.
- Stops live on the exchange, not only in the bot.
- A daily loss limit pauses new entries for the rest of the day.
- A max drawdown kill switch flattens everything and stops the bot.
- A fee filter skips trades whose target doesn't beat fees ×3.
- Decisions use closed candles only.
- Network errors are retried, and the container restarts automatically.

## 3. Quick start (local, paper)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
```bash
.venv/bin/python tests/test_strategy.py
```
```bash
.venv/bin/python src/backtest.py --coin ETH --interval 4h
```
```bash
.venv/bin/python src/walkforward.py
```
```bash
.venv/bin/python src/carry.py
```
```bash
.venv/bin/python src/bot.py run
```
```bash
.venv/bin/python src/bot.py scan
```

Output goes to `logs/<config>.log` (human-readable) and `logs/<config>.decisions.jsonl` (every factor for every
decision, for later analysis). Paper trades are recorded in `state/<config>.paper.json`.
Run several strategies at once with separate configs: `bot.py run --config aggressive.json`.

## 4. Config reference (`config.json`)

| Key | Meaning |
|---|---|
| `mode` | `paper` (simulated), `testnet` (fake money on HL testnet), `live` |
| `coins`, `interval` | any Hyperliquid perps; `15m`/`1h`/`4h`/`1d` (4h recommended) |
| `weights.*` | importance of each factor. Set to 0 to disable one; a negative weight inverts it |
| `entry_threshold` / `exit_threshold` | how strong the score must be to open a position / how far against you it must flip to close it |
| `allow_short` | `false` = longs only |
| `risk.risk_per_trade_pct` | % of equity lost if the stop hits (keep this 0.5–2) |
| `risk.sl_atr` / `tp_atr` | stop and target distance as multiples of ATR |
| `risk.max_leverage`, `max_position_usd`, `max_open_positions` | exposure caps |
| `risk.daily_loss_limit_pct`, `max_drawdown_pct` | circuit breakers |
| `risk.min_atr_pct`, `fee_multiple` | skip dead markets, and trades that can't beat fees |
| `ai.enabled/provider/model` | AI on/off; `openrouter` (model id, or a list to try in order) or `typesafe` (`jev-latest`) |
| `ai.weight/flat_veto/timeout_s` | how much the AI's opinion counts, the "flat" probability at which it vetoes, and the request timeout |
| `ai.min_confidence` | ignore the AI's long/short/flat answer below this confidence (Jev only; 0 = off). Tune it from the `confidence` values logged in `logs/*.decisions.jsonl`, not by guessing |
| `news.*` | RSS feeds, refresh rate, risk-event threshold, `flatten_on_risk_event` |

## 5. Going live (in order; don't skip steps)

1. Run paper mode for at least 2–4 weeks and read the log. Would you have been OK with those trades?
2. Get Hyperliquid testnet funds and set `"mode": "testnet"` with testnet keys, so the real order flow gets exercised.
3. Mainnet: fund your main wallet with USDC (Arbitrum) and deposit it at app.hyperliquid.xyz. Hyperliquid is geo-blocked in the US.
4. At app.hyperliquid.xyz → **API**, create an API wallet and authorize it. Put its private key in `HL_AGENT_PRIVATE_KEY`
   and your main wallet's *address* in `HL_ACCOUNT_ADDRESS`. **Never put your main wallet key on a server.**
5. Set `"mode": "live"` and start with $50–100.

## 6. Deploy on your Oracle box (same pipeline as growix)

Every push to `main` runs `.github/workflows/deploy.yml`. It runs the tests (a failing test blocks the deploy),
builds an **arm64** image, pushes it to `ghcr.io/idk-mr4tyunjay/trading-bot`, then connects over SSH as `deploy` and runs
`docker compose pull && docker compose up -d` in `/home/ubuntu/docker/apps/trading-bot`.

**One-time setup:**
1. Create a **private** GitHub repo, `idk-mr4tyunjay/trading-bot`, and push this folder to it.
2. In that repo, go to Settings → Secrets → Actions and add `VPS_HOST` and `VPS_SSH_KEY`, with the same values as growix.
3. On the VPS, create the app folder and copy the three files the container mounts, from the Mac:
   ```bash
   ssh -i <key> ubuntu@<ip> 'mkdir -p ~/docker/apps/trading-bot/state ~/docker/apps/trading-bot/logs'
   ```
   ```bash
   scp -i <key> docker-compose.yml config.json .env.example ubuntu@<ip>:~/docker/apps/trading-bot/
   ```
   `config.json` **must exist before the first `up`**. Otherwise Docker creates an empty *folder* with that name and the bot crashes.
4. On the VPS, create the secrets file:
   ```bash
   cd ~/docker/apps/trading-bot && mv .env.example .env && chmod 600 .env && nano .env
   ```
   Fill in `OPENROUTER_API_KEY`, and leave the Hyperliquid keys empty for paper mode. Then give the `deploy`
   user the same access it has to growix's folder: `sudo chown -R deploy:deploy ~/docker/apps/trading-bot`, or whatever owner growix's folder uses.
5. Push to `main` and watch the Actions tab. After that, every push deploys itself.

**Checks and notes:**
- Check the bot with `docker logs -f trading-bot`; it also shows up in Dozzle.
- `state/`, `logs/` and `config.json` live on the host, so redeploys keep your paper account, risk state and dashboard edits.
- The image is private because the repo is private. The VPS already logs into ghcr.io to pull growix. If the pull is
  denied, that login token was scoped to one package: run `docker login ghcr.io` as `deploy` with a PAT that has `read:packages`.
- Port 8090 must be free: `ss -ltn | grep 8090` should print nothing.

### Phone dashboard

Same setup as the other homelab apps: the app has **no login of its own**. It's reachable only through
Cloudflare Access or over WireGuard.

- **Off VPN:** https://trading.devtown.lol, through the `oracle` tunnel and gated by Cloudflare Access.
- **On VPN:** http://10.10.0.1:8090, with no login (`wg0` only, like Homepage and Dozzle).

The container publishes 8090 on `127.0.0.1` (for the tunnel) and `10.10.0.1` (for WireGuard) only. **Never** publish it on
`0.0.0.0`: Docker bypasses UFW, and anyone who reaches the port can switch the bot to live.

One-time VPS setup:
1. Add `- hostname: trading.devtown.lol` / `service: http://localhost:8090` to `/etc/cloudflared/config.yml`, above the 404 catch-all.
2. Run `cloudflared tunnel route dns oracle trading.devtown.lol`, then `sudo systemctl restart cloudflared`.
3. Nothing to do in the Cloudflare dashboard: the existing `*.devtown.lol` Access gate covers the new subdomain.
   Confirm it by opening the URL in a private window. You should get the Access login, not the dashboard.
4. Make Docker start after WireGuard, or the `10.10.0.1` bind fails at boot:
   `sudo systemctl edit docker` → `[Unit]` / `After=wg-quick@wg0.service` / `Wants=wg-quick@wg0.service`.
5. Add a tile to Homepage's `services.yaml`, then use Share → Add to Home Screen on your phone.

From the dashboard you can:
- see equity, the chart, open positions with live P&L, every coin's score and why it did or didn't trade, recent trades, news and risk, and the log
- pause or resume entries, close one position or all of them, and reset the paper account
- edit **every** setting in `config.json`. It applies within seconds, with no restart. The server rejects out-of-range risk
  values, and switching to `live` requires typing `LIVE`.

When the kill switch trips, the bot closes everything and **pauses** (it doesn't exit), and you resume it from the phone.
- Oracle can reclaim idle Always Free instances. Your existing Docker stack keeps the box active; this bot adds almost no load.

## Known simplifications

- Paper fills happen at the mid price ± slippage. Funding is charged every poll at the current rate, and stops are checked every 60 s, so gaps aren't modelled.
- The backtest has no order book, news or AI history (`like_live` treats the book as neutral). `src/backtest.py --coin` tests one coin; `src/walkforward.py` runs all coins on one account under `max_open_positions`.
- Carry is research only: `bot.py scan` shows live rates and `src/carry.py` backtests it (no spot/perp price gap, base-tier spot fees). Neither trades.
- Binance history stands in for Hyperliquid's before mid-2024. Prices track closely for BTC/ETH/SOL; funding differs a little.
