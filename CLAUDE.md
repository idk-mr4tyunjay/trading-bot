# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Hyperliquid perps trading bot with an optional AI signal blend and a phone dashboard. **It trades real money in `live` mode** — treat strategy, sizing, and risk
code as safety-critical.

Deployed to the Oracle Cloud ARM64 homelab (see the separate `vps-docs` repo) as a Docker
container. The dashboard has **no auth of its own**, matching the other homelab apps:
- https://trading.devtown.lol: `oracle` tunnel → `127.0.0.1:8090`, gated by Cloudflare Access
- http://10.10.0.1:8090: WireGuard only

Never publish 8090 on `0.0.0.0` (Docker bypasses UFW). Dashboard POSTs must be
`Content-Type: application/json`; that's the CSRF guard, so keep it.

## Layout

```
src/                 application code (run from the repo root)
  bot.py             entrypoint: `run` (trade loop) and `scan` (funding-carry scanner, read-only)
  strategy.py        indicators, factor combine, position sizing — pure, no network
  backtest.py        backtester: prepare() scores bars once, simulate() trades many coins on one account
                     under max_open_positions; history() caches funding history in .cache/
  walkforward.py     out-of-sample test: pick thresholds on a train window, trade the next unseen window
  broker.py          PaperBroker (simulated) and HyperliquidBroker (testnet/live)
  data.py            Hyperliquid market data
  ai.py              optional AI signal (OpenRouter / TypeSafe Jev)
  ui.py, ui.html     dashboard server + page (ui.py loads ui.html from its own dir)
tests/
  test_strategy.py   offline checks of strategy math + backtester — the CI gate
config.json          bot settings; the dashboard writes to it at runtime
.env.example         template for secrets (.env is gitignored)
Dockerfile, docker-compose.yml, .github/workflows/deploy.yml
```

Runtime paths (`config.json`, `.env`, `state/`, `logs/`) are **relative to the working
directory**, so always run commands from the repo root (the container's WORKDIR is `/app`).

## Commands

```bash
pip install -r requirements.txt
python tests/test_strategy.py               # must pass before pushing
python src/backtest.py --coin ETH --interval 4h
python src/walkforward.py [--top 15]          # honest out-of-sample estimate; first run is slow (funding paging)
python src/bot.py run                       # mode comes from config.json
python src/bot.py scan
python src/ai.py                            # one live AI call to test your key
```

## Modes (`config.json` → `mode`)

- `paper` — simulated fills via `PaperBroker`, state in `state/<name>.paper.json`. No keys needed.
- `testnet` — Hyperliquid testnet; needs `HL_ACCOUNT_ADDRESS` + `HL_AGENT_PRIVATE_KEY`.
- `live` — real money. The dashboard requires typing `LIVE` to switch.

## Deployment

Push to `main` → `deploy.yml`: run tests → build `linux/arm64` image → push to
`ghcr.io/idk-mr4tyunjay/trading-bot` → SSH to the VPS as `deploy`.

On the VPS (`/home/ubuntu/docker/apps/trading-bot/`):
- `docker-compose.yml` is overwritten from the repo on every deploy (staged via `.deploy/`).
- `.env` and `config.json` are seeded from the repo **only if missing** — after that they are
  owned by the VPS (real keys, dashboard-saved settings) and CI never touches them.
- `state/` and `logs/` are bind-mounted and persist across deploys.
- The `10.10.0.1` port bind needs `wg0` up first, so Docker is ordered `After=wg-quick@wg0`.

## Conventions

- **Never `git push` (or anything that triggers a deploy) until the user explicitly says to.**
  Pushing to `main` deploys straight to the VPS. Committing locally is fine; pushing is not.

- Never commit `.env` or put secret values in code, logs, or docs. Keep `.env.example` in
  sync when adding a new env var.
- Changes to `strategy.py` / `backtest.py` / sizing need a matching assertion in
  `tests/test_strategy.py`.
- The VPS is ARM64 — any new dependency must have an `aarch64` wheel or build cleanly.
