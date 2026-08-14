#!/usr/bin/env bash
# UX50 FreeFeed signal bot - free Yahoo feed, no MT5/Wine needed.
# The start command stays in the foreground so a Replit workflow can keep
# supervising it continuously. Use `bash run.sh stop` to stop the bot.
set -euo pipefail
cd "$(dirname "$0")"

COMMAND="${1:-start}"

export EXNESS_SYMBOLS="${EXNESS_SYMBOLS:-EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,NZDUSD,USDCHF,EURGBP,EURJPY,GBPJPY,EURCHF,GBPCHF,EURAUD,EURNZD,EURCAD,GBPAUD,GBPNZD,GBPCAD,AUDJPY,AUDNZD,AUDCAD,AUDCHF,NZDJPY,NZDCAD,NZDCHF,CADJPY,CADCHF,CHFJPY,USDZAR,USDTRY,USDMXN,USDSGD,USDPLN,USDNOK,USDSEK,USDCNH,USDINR,XAUUSD,XAGUSD,XPTUSD,XPDUSD,USOIL,UKOIL,NGAS,US30,US500,NAS100,GER40,FR40,EU50,UK100,JPN225,HK50,AU200,ES35,BTCUSD,ETHUSD,XRPUSD,SOLUSD,DOGEUSD,LTCUSD,BCHUSD}"
# 15m entries with 1H + 4H confirmation for a better accuracy/signal balance.
export EXNESS_TIMEFRAME_MINUTES="15"
export EXNESS_SCAN_SECONDS="120"
export EXNESS_MIN_SIGNAL_CONFIDENCE="90"
export EXNESS_MIN_RR="1.30"
export EXNESS_MAX_SPREAD_POINTS="200"
export EXNESS_SIGNAL_COOLDOWN_SECONDS="900"
export EXNESS_STATUS_EVERY_SECONDS="1800"
export EXNESS_ADAPTIVE_LEVEL="3"
export EXNESS_DIVERSIFICATION="1"
export EXNESS_MAX_OPEN_SIGNALS="3"
export EXNESS_RISK_PERCENT="1.0"
export EXNESS_ACCOUNT_BALANCE="1000"
export EXNESS_SESSION_FILTER="1"
export EXNESS_PREMIUM_STATS_FILE="$PWD/premium_signal_stats.json"
export EXNESS_OPEN_SIGNALS_FILE="$PWD/premium_open_signals.json"
export EXNESS_SIGNAL_HISTORY_FILE="$PWD/premium_signal_history.json"
export EXNESS_MIN_STATS_SAMPLES="20"
export EXNESS_MIN_SYMBOL_WINRATE="48"
export EXNESS_MAX_CONSECUTIVE_LOSSES="3"
# 12 x 15m candles = 3-hour maximum holding window before expiry-close result.
export EXNESS_OUTCOME_LOOKAHEAD="12"
export EXNESS_MAX_CANDLE_ATR="3.0"
export EXNESS_MIN_CANDLE_ATR="0.12"
export EXNESS_MIN_OOS_WINRATE="48"

# --- Telegram is optional. Keep credentials in Replit Secrets/env only. ---
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

if [ "$COMMAND" = "stop" ]; then
    if [ -f bot.pid ]; then
        kill "$(cat bot.pid)" 2>/dev/null && echo "bot stopped"
        rm -f bot.pid
    fi
    pkill -f "[u]x50_freefeed.py" 2>/dev/null
    echo "bot stopped"
    exit 0
fi

if [ "$COMMAND" != "start" ]; then
    echo "Usage: bash run.sh [start|stop]" >&2
    exit 2
fi

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
    echo "WARNING: Telegram credentials are not configured; signals will be logged and saved locally."
fi

# Watchdog loop: restart automatically if the bot dies. This process remains
# attached to the workflow instead of forking into the background.
cleanup() {
    if [ -f bot.pid ]; then
        kill "$(cat bot.pid)" 2>/dev/null || true
        rm -f bot.pid
    fi
}
trap cleanup EXIT INT TERM

echo "[watchdog] $(date "+%F %T") started; log: $PWD/bot.log"
while true; do
    if [ ! -f bot.pid ] || ! kill -0 "$(cat bot.pid 2>/dev/null)" 2>/dev/null; then
        echo "[watchdog] $(date "+%F %T") starting bot" >> bot.log
        python3 ux50_freefeed.py >> bot.log 2>&1 &
        echo $! > bot.pid
    fi
    sleep 10
done
