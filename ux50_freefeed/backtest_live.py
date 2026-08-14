import asyncio
import json
import os
import sys
import time
from pathlib import Path

os.environ["EXNESS_SESSION_FILTER"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ux50_freefeed as bot


async def main():
    bridge = bot.FreeFeedBridge()
    ok, msg = await bridge.connect()
    print(f"[connect] {ok}: {msg}")
    if not ok:
        return
    for symbol in ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]:
        candles = await bridge.candles(symbol, 15, count=220)
        if len(candles) < 120:
            print(f"[{symbol}] not enough candles: {len(candles)}")
            continue
        print(f"=== {symbol} ({len(candles)} candles) ===")
        print("backtest:", json.dumps(bot.backtest_strategy(candles, symbol, 15, lookahead=12), ensure_ascii=False))
        wf = bot.walk_forward_report(candles, symbol, 15)
        train, oos = wf["train"], wf["out_of_sample"]
        print(f"walk_forward: train(signals={train['signals']}, winrate={train['win_rate']}%) | out_of_sample(signals={oos['signals']}, winrate={oos['win_rate']}%) | validated={wf['validated']}")
        print("-" * 60)
        await asyncio.sleep(1)


asyncio.run(main())