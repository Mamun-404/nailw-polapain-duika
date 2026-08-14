import asyncio
import json
import os
import time
from pathlib import Path

os.environ["LOG_LEVEL"] = "INFO"
os.environ["EXNESS_SESSION_FILTER"] = "0"
DATA_DIR = Path(__file__).resolve().parent
os.environ["EXNESS_PREMIUM_STATS_FILE"] = str(DATA_DIR / "stats_live.json")
os.environ["EXNESS_OPEN_SIGNALS_FILE"] = str(DATA_DIR / "open_live.json")
os.environ["EXNESS_SIGNAL_HISTORY_FILE"] = str(DATA_DIR / "history_live.json")

import sys
sys.path.insert(0, str(DATA_DIR))
import ux50_freefeed as bot

CHROMA = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X", "XAUUSD": "GC=F", "XAGUSD": "SI=F"
}

async def main():
    bridge = bot.FreeFeedBridge()
    ok, msg = await bridge.connect()
    print(f"[connect] {ok}: {msg}")
    if not ok:
        return
    for symbol in ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]:
        t0 = time.time()
        candles = await bridge.candles(symbol, 15, count=220)
        quote = await bridge.quote(symbol)
        print(f"[{symbol}] candles={len(candles)} took={time.time()-t0:.1f}s quote={quote}")
        if len(candles) < 60:
            continue
        print(f"  last closed candle: t={candles[-1]['at']} O={candles[-1]['open']:.5f} H={candles[-1]['high']:.5f} L={candles[-1]['low']:.5f} C={candles[-1]['close']:.5f}")
        quality_ok, reasons = bot._premium_market_quality(candles)
        print(f"  market quality: {quality_ok} {reasons}")
        result = await bot.run_signal_cycle(bridge, symbol, 15)
        print(f"  cycle result: {result}")
        print("-" * 60)
        await asyncio.sleep(2)

asyncio.run(main())