#!/usr/bin/env python3
"""Print the persisted signal results for a quick health/performance check."""
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
stats_path = Path(os.getenv("EXNESS_PREMIUM_STATS_FILE", ROOT / "premium_signal_stats.json"))
history_path = Path(os.getenv("EXNESS_SIGNAL_HISTORY_FILE", ROOT / "premium_signal_history.json"))


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


stats = read_json(stats_path, {})
history = read_json(history_path, [])
settled = [row for row in history if row.get("status") == "settled"]
open_signals = [row for row in history if row.get("status") == "open"]
wins = sum(row.get("result") == "win" for row in settled)
losses = sum(row.get("result") == "loss" for row in settled)
win_rate = 100 * wins / max(1, wins + losses)

print(f"signals settled: {len(settled)}")
print(f"wins: {wins} | losses: {losses} | win-rate: {win_rate:.1f}%")
print(f"open signals: {len(open_signals)}")
print(f"symbols tracked: {len(stats)}")
if settled:
    latest = max(settled, key=lambda row: row.get("settled_at", 0))
    print(
        f"latest: {latest.get('symbol')} {latest.get('direction')} "
        f"{latest.get('result')} (id={latest.get('id')})"
    )