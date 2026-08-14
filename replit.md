# UX50 FreeFeed Signal Bot

Signal-only market advisory bot using free Yahoo Finance candle data. It generates filtered Entry/SL/TP alerts, persists open and settled outcomes locally, and never places broker orders.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `bash ux50_freefeed/run.sh start` — run the signal bot with its foreground watchdog
- `bash ux50_freefeed/run.sh stop` — stop the signal bot
- `python ux50_freefeed/results_report.py` — print persisted signal/result statistics
- `python ux50_freefeed/check_markets.py` — verify the free market feed
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Optional env: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` — notifications only; local logs/results work without them

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `ux50_freefeed/ux50_freefeed.py` — signal strategy, feed bridge, and result settlement
- `ux50_freefeed/run.sh` — persistent watchdog runner
- `ux50_freefeed/results_report.py` — local result summary
- `ux50_freefeed/premium_signal_history.json` — signal audit history generated at runtime

## Architecture decisions

- The bot is advisory-only and intentionally has no order-placement path.
- Yahoo Finance is used as a free, read-only feed; missing/temporary feed data is treated as unavailable rather than fabricated.
- Signal result tracking is local and independent of Telegram delivery.
- The workflow keeps the watchdog in the foreground so the process remains supervised.

## Product

The bot scans configured markets, applies multi-timeframe and quality gates, logs qualified signals, and settles them against later closed candles as wins or losses.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
