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

**Conclusion for a small trader:** HFT is not your game. Your Oracle box is in Bangalore, about 100 ms or more from
Hyperliquid's Tokyo infrastructure. You'd be competing against co-located firms, and at small size, fees are larger
than any per-trade edge. Jev *is* useful for what it's good at: **reading messy inputs (headlines, mixed signals)
fast and cheaply, and returning calibrated probabilities.** This bot uses it that way:
1. It gives a long/short/flat opinion that is blended with the rule score (`ai.weight`), and can veto trades when it says "flat".
2. It classifies news sentiment every hour, which feeds the `news` factor.
3. It flags news risk events (hack, depeg, ban, liquidation cascade), which block new entries or optionally flatten positions.

The bot works fully without AI. The same questions can go to **OpenRouter** (`ai.provider: "openrouter"`) today. The
LLM is asked to return the same probabilities as JSON, and the answer is validated and normalized. That's slower
(seconds rather than 100 ms, which doesn't matter on 4h bars) and less calibrated than Jev. The defaults are two free
models with automatic fallback. OpenRouter free models allow about 50 requests/day without purchased credits, and the
defaults use about 40/day (3 coins × 6 bars, plus news hourly). A paid fast model such as
`deepseek/deepseek-v4-flash-0731` costs well under $0.01/day. Test your key with `.venv/bin/python ai.py`.
A/B it: run `config.json` and a copy with AI enabled side by side in paper mode, then compare `logs/*.decisions.jsonl`.

### Arbitrage: realistic or not?

| Type | For a small account? |
|---|---|
| Cross-exchange price arb | **No.** You pay 0.05–0.1% taker fees on both legs, plus withdrawal and bridge fees and transfer delays, and pros close gaps in milliseconds. |
| Funding carry (long spot + short perp on HL) | **Real but tiny.** Typical funding is 11–35% APR on alts. Entry and exit cost ~0.23% across 4 legs, so break-even takes 2–8 days, and funding flips negative in sell-offs. $100 at 15% APR is about $0.04/day. `python bot.py scan` shows live numbers. It's scan-only on purpose. |
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
2+ years on $100. The value for you is learning with controlled risk, not getting rich. The backtest covers only the
technical factors, because funding, order book, news and Jev have no history.

### Costs of running this

| Item | Cost |
|---|---|
| Oracle Always Free ARM | $0. The bot uses ~60 MB RAM and negligible CPU. ⚠️ Oracle cut free A1 to 2 OCPU / 12 GB on 2026-06-15, and your box reports ~23 GiB, so check Billing that you're not being charged. |
| Hyperliquid perps | taker 0.045%, maker 0.015%, no gas. Round trip ≈ 0.09% plus slippage. On a $40 position that's about $0.04. |
| Funding | paid or received hourly while holding a perp (usually ~0.001%/h) |
| Deposit / withdraw | USDC on Arbitrum. Minimum deposit 5 USDC, and withdrawal costs $1. You also need a few cents of ETH on Arbitrum for gas. |
| AI (optional) | OpenRouter free models: $0 (about 40 calls/day). Paid cheap model or Jev: under **$0.01/day** |
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
.venv/bin/python test_strategy.py
```
```bash
.venv/bin/python backtest.py --coin ETH --interval 4h
```
```bash
.venv/bin/python bot.py run
```
```bash
.venv/bin/python bot.py scan
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
   Fill in `OPENROUTER_API_KEY` and `UI_PASSWORD`, and leave the Hyperliquid keys empty for paper mode. Then give the `deploy`
   user the same access it has to growix's folder: `sudo chown -R deploy:deploy ~/docker/apps/trading-bot`, or whatever owner growix's folder uses.
5. Push to `main` and watch the Actions tab. After that, every push deploys itself.

**Checks and notes:**
- Check the bot with `docker logs -f trading-bot`; it also shows up in Dozzle.
- `state/`, `logs/` and `config.json` live on the host, so redeploys keep your paper account, risk state and dashboard edits.
- The image is private because the repo is private. The VPS already logs into ghcr.io to pull growix. If the pull is
  denied, that login token was scoped to one package: run `docker login ghcr.io` as `deploy` with a PAT that has `read:packages`.
- Port 8090 must be free: `ss -ltn | grep 8090` should print nothing.

### Phone dashboard

Set `UI_PASSWORD` in `.env`. The dashboard listens on `127.0.0.1:8090` on the VPS, so it's never a public port.
Expose it through your existing tunnel:

1. Add `- hostname: bot.<your-domain>` / `service: http://localhost:8090` to `/etc/cloudflared/config.yml`, above the 404 catch-all.
2. Run `cloudflared tunnel route dns oracle bot.<your-domain>`, then `sudo systemctl restart cloudflared`.
3. **Put `bot.<your-domain>` behind Cloudflare Access**, like your other subdomains. That gives two locks: Access, then the password.
4. On your phone, open it and use Share → Add to Home Screen.

From the dashboard you can:
- see equity, the chart, open positions with live P&L, every coin's score and why it did or didn't trade, recent trades, news and risk, and the log
- pause or resume entries, close one position or all of them, and reset the paper account
- edit **every** setting in `config.json`. It applies within seconds, with no restart. The server rejects out-of-range risk
  values, and switching to `live` requires typing `LIVE`.

When the kill switch trips, the bot closes everything and **pauses** (it doesn't exit), and you resume it from the phone.
- Oracle can reclaim idle Always Free instances. Your existing Docker stack keeps the box active; this bot adds almost no load.

## Known simplifications

- Paper fills happen at the mid price ± slippage. Funding isn't simulated, and stops are checked every 60 s, so gaps aren't modelled.
- The backtest only covers technical factors (the others have no history).
- The funding carry scanner doesn't execute trades. Add execution only if the scan shows carry worth your capital.
