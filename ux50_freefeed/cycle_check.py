import asyncio
import os
import sys
from pathlib import Path

os.environ["EXNESS_SESSION_FILTER"] = "0"
DATA_DIR = Path(__file__).resolve().parent
os.environ["EXNESS_PREMIUM_STATS_FILE"] = str(DATA_DIR / "stats_live.json")
os.environ["EXNESS_OPEN_SIGNALS_FILE"] = str(DATA_DIR / "open_live.json")
os.environ["EXNESS_SIGNAL_HISTORY_FILE"] = str(DATA_DIR / "history_live.json")
os.environ["LOG_LEVEL"] = "ERROR"
sys.path.insert(0, str(DATA_DIR))
import ux50_freefeed as bot

SAMPLE = ["EURUSD", "XAUUSD", "US30", "BTCUSD", "USOIL", "UK100"]


async def main():
    b = bot.FreeFeedBridge()
    print(await b.connect(), flush=True)
    for s in SAMPLE:
        res = await bot.run_signal_cycle(b, s, 15)
        print(f"{s:7s} -> {'SIGNAL ' + str(res['direction']) if res else 'no signal (gated)'}", flush=True)
        await asyncio.sleep(1)


asyncio.run(main())