#!/usr/bin/env python3
"""UX50 Auto-Trade Executor for Exness via MetaTrader 5.

Reads premium_open_signals.json (written by ux50_freefeed.py), places
market orders on the Exness account with Entry/SL/TP, monitors positions,
and reports fills + results to Telegram.

Requirements:
  - MT5 terminal installed and logged out (this script logs in with credentials)
  - pip install MetaTrader5
  - env: EXNESS_LOGIN, EXNESS_PASSWORD, EXNESS_SERVER (e.g. Exness-MT5)
  - optional: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID for notifications

Run:  python3 mt5_executor.py          (loop, 30s poll)
      python3 mt5_executor.py once     (single pass, for cron)
"""
import json
import os
import sys
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
os.chdir(DATA_DIR)

try:
    import MetaTrader5 as mt5
except ImportError:
    print("MetaTrader5 package not installed. Run: pip install MetaTrader5")
    sys.exit(2)

import requests

MAGIC = 5050
OPEN_FILE = os.getenv("EXNESS_OPEN_SIGNALS_FILE", "premium_open_signals.json")
STATS_FILE = os.getenv("EXNESS_PREMIUM_STATS_FILE", "premium_signal_stats.json")
HISTORY_FILE = os.getenv("EXNESS_SIGNAL_HISTORY_FILE", "premium_signal_history.json")


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path, value):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _tg(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[tg]", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception as exc:
        print("tg failed:", exc)


def _connect():
    login = int(os.getenv("EXNESS_LOGIN", "0") or 0)
    password = os.getenv("EXNESS_PASSWORD", "")
    server = os.getenv("EXNESS_SERVER", "Exness-MT5")
    if not login or not password:
        print("EXNESS_LOGIN / EXNESS_PASSWORD env missing")
        return False
    if not mt5.initialize(login=login, password=password, server=server):
        print("MT5 initialize failed:", mt5.last_error())
        return False
    acc = mt5.account_info()
    if acc is None:
        print("no account info:", mt5.last_error())
        return False
    print(f"MT5 connected | login={acc.login} server={acc.server} balance={acc.balance:.2f} equity={acc.equity:.2f}")
    return True


def _lot_for(symbol, entry, sl, risk_pct=None):
    risk_pct = float(os.getenv("EXNESS_RISK_PERCENT", "1.0")) if risk_pct is None else risk_pct
    acc = mt5.account_info()
    if not acc:
        return 0.01
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        return 0.01
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return 0.01
    is_buy = entry >= tick.bid
    risk_distance = abs(entry - sl)
    if risk_distance <= 0:
        return 0.01
    tick_value = symbol_info.trade_tick_value or 0.0
    tick_size = symbol_info.trade_tick_size or 0.0
    if tick_size <= 0:
        return 0.01
    value_per_point = tick_value / tick_size
    risk_money = acc.balance * risk_pct / 100.0
    lot = risk_money / (risk_distance * value_per_point)
    lot = max(symbol_info.volume_min or 0.01, lot)
    lot = min(lot, symbol_info.volume_max or 100.0)
    step = symbol_info.volume_step or 0.01
    lot = round(lot / step) * step
    return round(lot, 2)


def _place_order(signal):
    symbol = str(signal.get("symbol", "")).upper()
    is_buy = "UP" in str(signal.get("direction", "")).upper() or "BUY" in str(signal.get("direction", "")).upper()
    entry = float(signal.get("entry", 0))
    sl = float(signal.get("sl", 0))
    tp = float(signal.get("tp", 0))
    if not symbol or entry <= 0 or sl <= 0 or tp <= 0:
        return None
    if not mt5.symbol_select(symbol, True):
        print(f"symbol_select failed: {symbol} ({mt5.last_error()})")
        return None
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not info or not tick:
        print(f"no info/tick for {symbol}")
        return None
    price = tick.ask if is_buy else tick.bid
    lot = _lot_for(symbol, entry, sl)
    order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": int(os.getenv("EXNESS_DEVIATION", "20")),
        "magic": MAGIC,
        "comment": "UX50",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None:
        print(f"order_send returned None for {symbol}: {mt5.last_error()}")
        return None
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"order rejected {symbol}: retcode={result.retcode} {result.comment}")
        return None
    print(f"FILLED {symbol} {'BUY' if is_buy else 'SELL'} lot={lot} price={result.price} sl={sl} tp={tp} ticket={result.order}")
    _tg(
        f"*AUTO TRADE FILLED*\n"
        f"{symbol} {'BUY' if is_buy else 'SELL'} | lot {lot}\n"
        f"Fill: `{result.price}` | SL `{sl}` | TP `{tp}`\n"
        f"Ticket: `{result.order}`"
    )
    return result.order


def _sync_positions():
    """Mark signals as closed when MT5 closes them (SL/TP hit)."""
    positions = mt5.positions_get(magic=MAGIC) or []
    open_pos = {}
    for p in positions:
        open_pos[str(p.ticket)] = p
    opened = _read_json(OPEN_FILE, [])
    changed = False
    for signal in opened:
        ticket = signal.get("mt5_ticket")
        if not ticket or signal.get("status") != "open":
            continue
        pos = open_pos.get(str(ticket))
        if pos is None:
            signal["status"] = "closed_by_mt5"
            signal["mt5_closed_at"] = int(time.time())
            changed = True
            _tg(f"*POSITION CLOSED* {signal.get('symbol')} ticket {ticket} (SL/TP/manual)")
    if changed:
        _write_json(OPEN_FILE, opened)
    return changed


def _trade_pass():
    if not _connect():
        return
    try:
        _sync_positions()
        opened = _read_json(OPEN_FILE, [])
        for signal in opened:
            if signal.get("status") != "open" or signal.get("mt5_ticket"):
                continue
            ticket = _place_order(signal)
            if ticket:
                signal["mt5_ticket"] = ticket
                signal["mt5_placed_at"] = int(time.time())
                _write_json(OPEN_FILE, opened)
    finally:
        mt5.shutdown()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "loop"
    if mode == "once":
        _trade_pass()
        return
    print("UX50 MT5 executor | poll 30s | magic", MAGIC)
    while True:
        try:
            _trade_pass()
        except Exception as exc:
            print("pass failed:", exc)
        time.sleep(30)


if __name__ == "__main__":
    main()
