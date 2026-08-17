#!/usr/bin/env python3
"""Single scan pass for GitHub Actions (cron) runs.

Runs one full cycle: settle open signals, scan all configured symbols,
persist state, then print a summary. Exit code 0 on success.

State JSON files are committed back to the repo by the workflow so the
next scheduled run continues from where the previous one left off.
"""
import asyncio
import os
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
os.chdir(DATA_DIR)
sys.path.insert(0, str(DATA_DIR))
import ux50_freefeed as bot


async def main() -> int:
    bridge = bot.FreeFeedBridge()
    connected, message = await bridge.connect()
    if not connected:
        print(f"feed connect failed: {message}")
        return 1
    symbols = [x.strip() for x in os.getenv("EXNESS_SYMBOLS", "EURUSD,GBPUSD,USDJPY,XAUUSD").split(",") if x.strip()]
    timeframe = int(os.getenv("EXNESS_TIMEFRAME_MINUTES", "60"))
    print(f"run_once | connected | symbols={len(symbols)} timeframe={timeframe}m")

    try:
        settled = await bot._settle_open_signals_with_bridge(bridge)
        print(f"settled signals: {settled}")
    except Exception as exc:
        print(f"settlement pass failed: {exc}", flush=True)

    found = 0
    for symbol in symbols:
        try:
            result = await bot.run_signal_cycle(bridge, symbol, timeframe)
            if result:
                found += 1
                print(f"signal: {symbol} {result.get('direction')} entry={result.get('entry')}", flush=True)
        except Exception as exc:
            print(f"cycle failed {symbol}: {exc}", flush=True)
        await asyncio.sleep(1)

    stats = bot._read_json(bot.PREMIUM_STATS_FILE, {})
    sigs = sum(int(r.get("signals", 0)) for r in stats.values())
    wins = sum(int(r.get("wins", 0)) for r in stats.values())
    losses = sum(int(r.get("losses", 0)) for r in stats.values())
    wr = 100.0 * wins / max(1, wins + losses)
    open_count = len(bot._read_json(bot.OPEN_SIGNALS_FILE, []))
    print(f"PASS OK | scanned={len(symbols)} new_signals={found} open={open_count} total={sigs} winrate={wr:.1f}%")
    await bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))