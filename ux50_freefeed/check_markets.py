import asyncio
import os
import sys
import time
from pathlib import Path

os.environ["EXNESS_SESSION_FILTER"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ux50_freefeed as bot

CHECKS = ["EURUSD", "XAUUSD", "DOGEUSD", "US30", "USOIL", "CADJPY", "USDTRY"]

async def main():
    b = bot.FreeFeedBridge()
    print(await b.connect())
    for s in CHECKS:
        t0 = time.time()
        c = await b.candles(s, 15, count=120)
        q = await b.quote(s)
        dt = time.time() - t0
        last = f"n={len(c)}" + (f" last={c[-1]['close']:.5f}" if c else " EMPTY")
        quote = (
            f"bid{q['bid']:.5f} ask{q['ask']:.5f} digits={q['digits']} point={q['point']:.7g}"
            if q else "UNAVAILABLE"
        )
        print(f"{s:8s} {last:26s} quote={quote} ({dt:.1f}s)")
        await asyncio.sleep(1)
    b2 = bot.FreeFeedBridge()
    print("htf-test US30 60m:", len(await b2.candles("US30", 60, count=120)), "candles")

asyncio.run(main())