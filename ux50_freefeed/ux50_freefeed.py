#!/usr/bin/env python3
"""Clean Wine/MT5 Exness signal-only bot extracted from the original UX strategy.

This file intentionally contains no binary-options, payout, expiry, tournament,
win-check, or order-placement logic. It reads MT5 candles, runs the retained
ultra-hybrid strategy, and sends Telegram signals with Entry/SL/TP.
"""
import asyncio
import json
import logging
import os
import tempfile
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

import requests

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ux50_wine_mt5_clean")
TRADING_MODE = "NORMAL"
_htf_ex = None
last_signal_meta = {}
_TOUCH_NEAR = 0.0025
_TOUCH_CLOSE = 0.0012


def print_status_message(message, level="info"):
    getattr(logger, level if level in {"debug", "info", "warning", "error"} else "info")(str(message))


def get_pattern_tier_info(pattern_name: str) -> dict:
    name = str(pattern_name or "").lower()
    tier = "T1" if any(x in name for x in ("morning", "evening", "engulfing", "soldiers", "crows", "breakout")) else "T2"
    if "engulfing" in name:
        family = "engulfing"
    elif "hammer" in name:
        family = "hammer"
    elif "shooting star" in name:
        family = "shooting star"
    elif "morning star" in name or "evening star" in name:
        family = "star reversal"
    elif "pivot" in name:
        family = "pivot bounce"
    elif "pin bar" in name:
        family = "pin bar"
    elif "support bounce" in name or "resistance rejection" in name or "range support" in name or "range resistance" in name:
        family = "support/resistance bounce"
    elif "divergence" in name:
        family = "rsi divergence"
    elif "soldiers" in name or "crows" in name:
        family = "three soldiers/crows"
    elif "breakout continuation" in name:
        family = "breakout continuation"
    elif "pullback continuation" in name:
        family = "pullback continuation"
    elif "inside bar breakout" in name:
        family = "inside bar breakout"
    else:
        family = ""
    return {"tier": tier, "pattern_tier": tier, "family": family, "required_delta": 0.0, "confidence_bonus": 0.0}


def get_pair_pattern_learning_adjustment(asset: str, timeframe: int, pattern_name: str = "", direction: str = None, lookback: int = 220) -> dict:
    return {"score": 0.0, "adjustment": 0.0, "confidence_adjustment": 0.0}


def get_recent_adaptive_state(asset: str = "", timeframe: int = 1, lookback: int = 20) -> dict:
    return {"pause": False, "elite_only": False, "required_delta": 0.0, "min_conf_delta": 0.0, "mode": "neutral"}


_INTERVAL_SECONDS = {1: 60, 5: 300, 15: 900, 30: 1800, 60: 3600, 240: 14400, 1440: 86400}
_RANGE_BY_INTERVAL = {"1m": "1d", "5m": "5d", "15m": "5d", "30m": "5d", "1h": "1mo", "4h": "1mo", "1d": "3mo"}

# Exness market family tables: base name -> (yahoo ticker, digits, spread in price units)
_SYMBOLS = {
    # FX majors/minors
    "EURUSD": ("EURUSD=X", 5, 0.00012), "GBPUSD": ("GBPUSD=X", 5, 0.00016),
    "USDJPY": ("USDJPY=X", 3, 0.013), "AUDUSD": ("AUDUSD=X", 5, 0.00014),
    "USDCAD": ("USDCAD=X", 5, 0.00018), "NZDUSD": ("NZDUSD=X", 5, 0.00020),
    "USDCHF": ("USDCHF=X", 5, 0.00018), "EURGBP": ("EURGBP=X", 5, 0.00015),
    "EURJPY": ("EURJPY=X", 3, 0.014), "GBPJPY": ("GBPJPY=X", 3, 0.020),
    "EURCHF": ("EURCHF=X", 5, 0.00022), "GBPCHF": ("GBPCHF=X", 5, 0.00026),
    "EURAUD": ("EURAUD=X", 5, 0.00028), "EURNZD": ("EURNZD=X", 5, 0.00032),
    "EURCAD": ("EURCAD=X", 5, 0.00030), "GBPAUD": ("GBPAUD=X", 5, 0.00035),
    "GBPNZD": ("GBPNZD=X", 5, 0.00040), "GBPCAD": ("GBPCAD=X", 5, 0.00038),
    "AUDJPY": ("AUDJPY=X", 3, 0.016), "AUDNZD": ("AUDNZD=X", 5, 0.00022),
    "AUDCAD": ("AUDCAD=X", 5, 0.00020), "AUDCHF": ("AUDCHF=X", 5, 0.00020),
    "NZDJPY": ("NZDJPY=X", 3, 0.018), "NZDCAD": ("NZDCAD=X", 5, 0.00024),
    "NZDCHF": ("NZDCHF=X", 5, 0.00024), "CADJPY": ("CADJPY=X", 3, 0.015),
    "CADCHF": ("CADCHF=X", 5, 0.00020), "CHFJPY": ("CHFJPY=X", 3, 0.018),
    # FX exotics
    "USDZAR": ("ZAR=X", 5, 0.03), "USDTRY": ("TRY=X", 5, 0.05),
    "USDMXN": ("MXN=X", 5, 0.008), "USDSGD": ("SGD=X", 5, 0.0012),
    "USDPLN": ("PLN=X", 5, 0.003), "USDNOK": ("NOK=X", 5, 0.002),
    "USDSEK": ("SEK=X", 5, 0.002), "USDCNH": ("CNH=X", 5, 0.002),
    "USDINR": ("INR=X", 5, 0.006),
    # Metals (XAU/XAG spot-corrected)
    "XAUUSD": ("GC=F", 2, 0.35), "XAGUSD": ("SI=F", 3, 0.02),
    "XPTUSD": ("PL=F", 2, 0.30), "XPDUSD": ("PA=F", 2, 0.80),
    # Energy
    "USOIL": ("CL=F", 2, 0.05), "UKOIL": ("BZ=F", 2, 0.05), "NGAS": ("NG=F", 3, 0.02),
    # Indices
    "US30": ("^DJI", 1, 2.5), "US500": ("^GSPC", 1, 2.5), "NAS100": ("^NDX", 1, 3.5),
    "GER40": ("^GDAXI", 1, 2.5), "FR40": ("^FCHI", 1, 2.5), "EU50": ("^STOXX50E", 1, 2.0),
    "UK100": ("^FTSE", 1, 2.5), "JPN225": ("^N225", 1, 15.0), "HK50": ("^HSI", 1, 5.0),
    "AU200": ("^AXJO", 1, 2.0), "ES35": ("^IBEX", 1, 2.5),
    # Crypto
    "BTCUSD": ("BTC-USD", 2, 30.0), "ETHUSD": ("ETH-USD", 2, 2.5),
    "XRPUSD": ("XRP-USD", 4, 0.001), "SOLUSD": ("SOL-USD", 3, 0.05),
    "DOGEUSD": ("DOGE-USD", 5, 0.0001), "LTCUSD": ("LTC-USD", 3, 0.05),
    "BCHUSD": ("BCH-USD", 2, 0.50),
}
_SPOT_CODE = {"XAUUSD": "XAU", "XAGUSD": "XAG"}
_CRYPTO_FAMILY = {"BTCUSD", "ETHUSD", "XRPUSD", "SOLUSD", "DOGEUSD", "LTCUSD", "BCHUSD"}
_INDEX_FAMILY = {"US30", "US500", "NAS100", "GER40", "FR40", "EU50", "UK100", "JPN225", "HK50", "AU200", "ES35"}
_METAL_ENERGY_FAMILY = {"XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD", "USOIL", "UKOIL", "NGAS"}


class FreeFeedBridge:
    """Free Yahoo Finance OHLCV bridge; no MT5, no broker account, no cost.

    Data source: query2.finance.yahoo.com chart API (open, no API key).
    Read-only: all order methods are intentionally absent.
    """
    _cache = {}
    _last_fetch_at = {}
    _spot_cache = {}
    _spot_last_at = {}
    SYMBOL_MAP = {name: entry[0] for name, entry in _SYMBOLS.items()}
    DIGITS = {name: entry[1] for name, entry in _SYMBOLS.items()}
    SPREAD_PRICE = {name: entry[2] for name, entry in _SYMBOLS.items()}
    INTERVALS = {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1d"}
    CACHE_TTL = 600.0
    _last_http_at = 0.0
    MIN_GAP = 2.0

    async def _throttle(self):
        now = time.time()
        gap = FreeFeedBridge.MIN_GAP - (now - FreeFeedBridge._last_http_at)
        if gap > 0:
            await asyncio.sleep(gap)
            now = time.time()
        FreeFeedBridge._last_http_at = now

    def __init__(self):
        self.connected = False
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36", "Accept": "application/json"})

    async def connect(self):
        try:
            await self._throttle()
            url = "https://query2.finance.yahoo.com/v8/finance/chart/EURUSD=X?interval=15m&range=5d"
            response = self._session.get(url, timeout=12)
            self.connected = response.ok and '"result"' in response.text
            return (True, "Free Yahoo feed connected") if self.connected else (False, f"HTTP {response.status_code}")
        except Exception as exc:
            self.connected = False
            return False, str(exc)

    def _ticker(self, symbol):
        base = str(symbol or "").strip().upper().replace("/", "").replace("-", "")
        if base in self.SYMBOL_MAP:
            return self.SYMBOL_MAP[base]
        return base + "=X"

    @staticmethod
    def timeframe(minutes: int):
        return FreeFeedBridge.INTERVALS.get(int(minutes), "15m")

    async def _fetch_spot(self, code):
        key = f"spot:{code}"
        now = time.time()
        cached = FreeFeedBridge._spot_cache.get(key)
        if cached and now - FreeFeedBridge._spot_last_at.get(key, 0.0) < 60.0:
            return cached
        try:
            response = requests.get(f"https://api.gold-api.com/price/{code}", timeout=10)
            if response.ok:
                payload = response.json()
                price = float(payload.get("price", 0.0))
                if price > 0:
                    FreeFeedBridge._spot_cache[key] = price
                    FreeFeedBridge._spot_last_at[key] = now
                    return price
        except Exception:
            pass
        return cached

    async def candles(self, symbol: str, minutes: int = 15, count: int = 160, force_refresh: bool = False):
        if not self.connected:
            return []
        base = str(symbol or "").strip().upper().replace("/", "").replace("-", "")
        interval = self.timeframe(minutes)
        key = f"{base}:{interval}"
        now = time.time()
        cached = FreeFeedBridge._cache.get(key)
        if cached and not force_refresh and now - FreeFeedBridge._last_fetch_at.get(key, 0.0) < FreeFeedBridge.CACHE_TTL:
            return cached["candles"]
        last_price = cached.get("price") if cached else None
        last_out = cached["candles"] if cached else []
        range_str = _RANGE_BY_INTERVAL.get(interval, "5d")
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{self._ticker(base)}?interval={interval}&range={range_str}"
        seconds = _INTERVAL_SECONDS.get(int(minutes), 900)
        want = max(40, min(500, int(count)))
        for attempt in (0, 1, 2):
            await self._throttle()
            try:
                response = self._session.get(url, timeout=15)
                if not response.ok:
                    if attempt < 2:
                        await asyncio.sleep(6)
                        continue
                    return last_out
                payload = response.json()
                result = (payload.get("chart") or {}).get("result")
                if not result:
                    if attempt < 2:
                        await asyncio.sleep(6)
                        continue
                    return last_out
                meta = result[0].get("meta") or {}
                timestamps = result[0].get("timestamp") or []
                quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0] or {}
                opens = quote.get("open") or []
                highs = quote.get("high") or []
                lows = quote.get("low") or []
                closes = quote.get("close") or []
                volumes = quote.get("volume") or []
                out = []
                for i in range(len(timestamps)):
                    t = int(timestamps[i])
                    if t + seconds > now:
                        continue
                    o = opens[i] if i < len(opens) and opens[i] is not None else 0.0
                    h = highs[i] if i < len(highs) and highs[i] is not None else o
                    l = lows[i] if i < len(lows) and lows[i] is not None else o
                    c = closes[i] if i < len(closes) and closes[i] is not None else o
                    v = volumes[i] if i < len(volumes) and volumes[i] is not None else 0.0
                    if o <= 0 or c <= 0:
                        continue
                    out.append({"at": t, "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v)})
                price_ref = meta.get("regularMarketPrice")
                factor = 1.0
                if out and price_ref:
                    price_ref = float(price_ref)
                    if price_ref > 0:
                        factor = out[-1]["close"] / price_ref
                        factor = max(1e-9, min(1e12, factor))
                if factor < 1.05:
                    factor = 1.0
                out = [{"at": x["at"], "open": x["open"] / factor, "high": x["high"] / factor, "low": x["low"] / factor, "close": x["close"] / factor, "volume": x["volume"]} for x in out]
                out = sorted(out, key=lambda x: x["at"])
                if base in _SPOT_CODE:
                    spot = await self._fetch_spot(_SPOT_CODE[base])
                    if spot and spot > 0:
                        shift = spot - out[-1]["close"] if out else 0.0
                        if out and abs(shift) < 0.5 * out[-1]["close"]:
                            out = [{"at": x["at"], "open": x["open"] + shift, "high": x["high"] + shift, "low": x["low"] + shift, "close": x["close"] + shift, "volume": x["volume"]} for x in out]
                if len(out) >= 60 or attempt == 2:
                    break
                await asyncio.sleep(2)
            except Exception as exc:
                logger.warning("Free feed fetch failed for %s: %s", symbol, exc)
                return last_out
        if len(out) > want:
            out = out[-want:]
        if out:
            FreeFeedBridge._cache[key] = {"candles": out, "price": meta.get("regularMarketPrice", last_price)}
            FreeFeedBridge._last_fetch_at[key] = now
        return out

    async def quote(self, symbol: str, minutes: int = 5, force_refresh: bool = False):
        if not self.connected:
            return None
        base = str(symbol or "").strip().upper().replace("/", "").replace("-", "")
        interval = self.timeframe(minutes)
        key = f"{base}:{interval}"
        entry = FreeFeedBridge._cache.get(key)
        price = float(entry["price"]) if entry and entry.get("price") else None
        if force_refresh or price is None:
            candles = await self.candles(symbol, minutes, count=5, force_refresh=force_refresh)
            if not candles:
                return None
            price = float(candles[-1]["close"])
        if base in _SPOT_CODE:
            spot = FreeFeedBridge._spot_cache.get(f"spot:{_SPOT_CODE[base]}")
            if spot and spot > 0:
                price = spot
        entry_spec = self.SYMBOL_MAP.get(base)
        digits = self.DIGITS.get(base, 5)
        point = 10.0 ** -digits
        spread = self.SPREAD_PRICE.get(base, point * 3.0)
        if base in _CRYPTO_FAMILY and spread > 0:
            point = spread / 30.0
        return {"symbol": base, "bid": price, "ask": price + spread, "digits": digits, "point": point}

    async def close(self):
        self.connected = False


def _find_swings(candles, window=3, lookback=70):
    lows = [float(c["low"]) for c in candles[-lookback:]]
    highs = [float(c["high"]) for c in candles[-lookback:]]
    swing_lows, swing_highs = [], []
    for i in range(window, len(lows) - window):
        seg_low = lows[i - window:i + window + 1]
        seg_high = highs[i - window:i + window + 1]
        if lows[i] == min(seg_low):
            swing_lows.append(lows[i])
        if highs[i] == max(seg_high):
            swing_highs.append(highs[i])
    return swing_lows, swing_highs


def levels_from_candles(symbol, candles, direction, bid=None, ask=None):
    if not candles:
        return None, None, None
    last = candles[-1]
    atr = _calc_atr(candles, 14) or max(float(last["high"]) - float(last["low"]), float(last["close"]) * 0.0005)
    is_buy = "UP" in str(direction).upper() or "BUY" in str(direction).upper()
    ref = float(last["close"])
    if is_buy and ask:
        entry = float(ask)
    elif (not is_buy) and bid:
        entry = float(bid)
    else:
        entry = ref
    swing_lows, swing_highs = _find_swings(candles)
    min_dist = atr * 0.8
    if is_buy:
        below = [l for l in swing_lows if l < entry - atr * 0.3]
        nearest_low = max(below) if below else None
        structure_sl = (nearest_low - atr * 0.2) if nearest_low else entry - atr * 1.5
        sl_dist = max(entry - structure_sl, min_dist)
        if sl_dist > atr * 3.0:
            return None, None, None
        sl = entry - sl_dist
        candidates = [h for h in swing_highs if h > entry + atr * 0.5]
        if candidates:
            tp = min(candidates)
        else:
            tp = entry + atr * 2.25
        tp = max(tp, entry + 1.35 * (entry - sl))
        tp = min(tp, entry + atr * 4.5)
    else:
        above = [h for h in swing_highs if h > entry + atr * 0.3]
        nearest_high = min(above) if above else None
        structure_sl = (nearest_high + atr * 0.2) if nearest_high else entry + atr * 1.5
        sl_dist = max(structure_sl - entry, min_dist)
        if sl_dist > atr * 3.0:
            return None, None, None
        sl = entry + sl_dist
        candidates = [l for l in swing_lows if l < entry - atr * 0.5]
        if candidates:
            tp = max(candidates)
        else:
            tp = entry - atr * 2.25
        tp = min(tp, entry - 1.35 * (sl - entry))
        tp = max(tp, entry - atr * 4.5)
    compact = str(symbol).upper().replace("/", "")
    digits = 3 if compact.endswith("JPY") else (2 if compact.startswith("XAU") else 5)
    return round(entry, digits), round(sl, digits), round(tp, digits)


def _timeframe_label(minutes):
    minutes = int(minutes or 0)
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}H"
    return f"{minutes}M"


def _price_digits(symbol):
    compact = str(symbol or "").upper().replace("/", "").replace("-", "")
    return FreeFeedBridge.DIGITS.get(compact, 3 if compact.endswith("JPY") else (2 if compact.startswith("XAU") else 5))


def telegram_send(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning("Telegram credentials missing; signal printed only")
        print(text)
        return False
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=15)
        response.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


def telegram_send_photo(image_path, caption):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id or not image_path:
        return False
    try:
        with open(image_path, "rb") as image:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": image},
                timeout=30,
            )
        response.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Telegram chart send failed: %s", exc)
        return False


def _render_signal_chart(symbol, candles, direction, entry, sl, tp, timeframe=60):
    """Render a professional dark candlestick chart for Telegram."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle

        recent = candles[-80:]
        if len(recent) < 10:
            return None
        fig, (axis, volume_axis) = plt.subplots(
            2,
            1,
            figsize=(11, 6.5),
            dpi=150,
            sharex=True,
            gridspec_kw={"height_ratios": [4, 1], "hspace": 0.05},
        )
        fig.patch.set_facecolor("#08111f")
        for current_axis in (axis, volume_axis):
            current_axis.set_facecolor("#0d1b2a")
            current_axis.tick_params(colors="#cbd5e1", labelsize=8)
            for spine in current_axis.spines.values():
                spine.set_color("#334155")
            current_axis.grid(color="#64748b", alpha=0.18, linewidth=0.6)

        candle_dates = []
        for index, candle in enumerate(recent):
            timestamp = candle.get("at")
            candle_dates.append(
                datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
                if timestamp
                else datetime.now(timezone.utc)
            )
            opened = float(candle["open"])
            high = float(candle["high"])
            low = float(candle["low"])
            close = float(candle["close"])
            color = "#22c55e" if close >= opened else "#ef4444"
            axis.vlines(index, low, high, color=color, linewidth=1.0, zorder=2)
            body_bottom = min(opened, close)
            body_height = max(abs(close - opened), (high - low) * 0.015, 1e-12)
            axis.add_patch(
                Rectangle(
                    (index - 0.32, body_bottom),
                    0.64,
                    body_height,
                    facecolor=color,
                    edgecolor=color,
                    linewidth=0.7,
                    alpha=0.95,
                    zorder=3,
                )
            )
            volume_axis.bar(
                index,
                float(candle.get("volume", 0.0) or 0.0),
                color=color,
                width=0.64,
                alpha=0.55,
            )

        digits = _price_digits(symbol)
        axis.axhline(entry, color="#f8fafc", linewidth=1.15, linestyle=(0, (5, 3)), label=f"Entry {entry:.{digits}f}")
        axis.axhline(sl, color="#fb7185", linewidth=1.15, linestyle=(0, (5, 3)), label=f"SL {sl:.{digits}f}")
        axis.axhline(tp, color="#4ade80", linewidth=1.15, linestyle=(0, (5, 3)), label=f"TP {tp:.{digits}f}")
        # professional setup markers
        try:
            swing_lows, swing_highs = _find_swings(candles, window=3, lookback=50)
            last_close = float(recent[-1]["close"])
            nearby_lows = [l for l in swing_lows if l < last_close]
            nearby_highs = [h for h in swing_highs if h > last_close]
            if nearby_lows:
                nearest_low = max(nearby_lows)
                axis.axhline(nearest_low, color="#94a3b8", linewidth=0.8, linestyle=(0, (2, 4)), alpha=0.55)
                axis.text(len(recent) - 0.5, nearest_low, f"  SUP {nearest_low:.{digits}f}", color="#94a3b8", fontsize=7, va="center")
            if nearby_highs:
                nearest_high = min(nearby_highs)
                axis.axhline(nearest_high, color="#94a3b8", linewidth=0.8, linestyle=(0, (2, 4)), alpha=0.55)
                axis.text(len(recent) - 0.5, nearest_high, f"  RES {nearest_high:.{digits}f}", color="#94a3b8", fontsize=7, va="center")
        except Exception:
            pass
        entry_arrow_color = "#22c55e" if "UP" in str(direction).upper() or "BUY" in str(direction).upper() else "#ef4444"
        axis.annotate(
            "", xy=(len(recent) - 2.2, entry), xytext=(len(recent) - 0.2, entry),
            arrowprops=dict(arrowstyle="->", color=entry_arrow_color, lw=2.4),
        )
        axis.annotate(
            "SETUP", xy=(len(recent) - 1, entry),
            xytext=(len(recent) - 6, 0.96), textcoords=("data", "axes fraction"),
            color="#e2e8f0", fontsize=9, fontweight="bold", ha="center",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#0f172a", edgecolor="#475569", alpha=0.92),
        )
        axis.set_title(
            f"{symbol}  |  {_timeframe_label(timeframe)}  |  {direction}  |  UX50 SIGNAL SETUP",
            color="#f8fafc",
            fontsize=13,
            fontweight="bold",
            loc="left",
            pad=12,
        )
        axis.set_ylabel("Price", color="#cbd5e1")
        volume_axis.set_ylabel("Volume", color="#cbd5e1")
        volume_axis.set_xlabel("UTC time", color="#cbd5e1")
        axis.legend(loc="upper left", fontsize=8, facecolor="#0d1b2a", edgecolor="#475569", labelcolor="#e2e8f0")
        axis.set_xlim(-1, len(recent))
        axis.margins(y=0.08)
        tick_count = min(8, len(recent))
        tick_indexes = [round(i * (len(recent) - 1) / max(1, tick_count - 1)) for i in range(tick_count)]
        axis.set_xticks(tick_indexes)
        axis.set_xticklabels([])
        volume_axis.set_xticks(tick_indexes)
        volume_axis.set_xticklabels(
            [candle_dates[index].strftime("%d %b\n%H:%M") for index in tick_indexes],
            color="#cbd5e1",
            fontsize=8,
        )
        volume_axis.set_xlim(-1, len(recent))
        fig.subplots_adjust(left=0.08, right=0.98, top=0.92, bottom=0.12, hspace=0.05)
        image_path = tempfile.NamedTemporaryFile(prefix="ux50_signal_", suffix=".png", delete=False).name
        fig.savefig(image_path, format="png")
        plt.close(fig)
        return image_path
    except Exception as exc:
        logger.warning("Signal chart render failed: %s", exc)
        return None


def _render_result_chart(signal, candles, result, exit_price, exit_at):
    """Render the trade outcome chart: entry -> exit path with SL/TP hit marked."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle

        symbol = str(signal.get("symbol", ""))
        direction = str(signal.get("direction", "")).upper()
        is_buy = "UP" in direction or "BUY" in direction
        entry = float(signal.get("entry", 0))
        sl = float(signal.get("sl", 0))
        tp = float(signal.get("tp", 0))
        opened_at = int(signal.get("opened_at", 0))
        if not candles or entry <= 0:
            return None
        start_idx = 0
        for i, c in enumerate(candles):
            if int(c.get("at", 0)) >= opened_at:
                start_idx = max(0, i - 2)
                break
        end_idx = len(candles) - 1
        for i, c in enumerate(candles):
            if int(c.get("at", 0)) >= exit_at:
                end_idx = i
                break
        recent = candles[start_idx:end_idx + 1]
        if len(recent) < 4:
            return None
        fig, (axis, volume_axis) = plt.subplots(
            2, 1, figsize=(11, 6.5), dpi=150, sharex=True,
            gridspec_kw={"height_ratios": [4, 1], "hspace": 0.05},
        )
        fig.patch.set_facecolor("#08111f")
        for current_axis in (axis, volume_axis):
            current_axis.set_facecolor("#0d1b2a")
            current_axis.tick_params(colors="#cbd5e1", labelsize=8)
            for spine in current_axis.spines.values():
                spine.set_color("#334155")
            current_axis.grid(color="#64748b", alpha=0.18, linewidth=0.6)
        candle_dates = []
        for index, candle in enumerate(recent):
            timestamp = candle.get("at")
            candle_dates.append(
                datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
                if timestamp else datetime.now(timezone.utc)
            )
            opened = float(candle["open"]); high = float(candle["high"])
            low = float(candle["low"]); close = float(candle["close"])
            color = "#22c55e" if close >= opened else "#ef4444"
            axis.vlines(index, low, high, color=color, linewidth=1.0, zorder=2)
            body_bottom = min(opened, close)
            body_height = max(abs(close - opened), (high - low) * 0.015, 1e-12)
            axis.add_patch(
                Rectangle((index - 0.32, body_bottom), 0.64, body_height,
                          facecolor=color, edgecolor=color, linewidth=0.7, alpha=0.95, zorder=3)
            )
            volume_axis.bar(index, float(candle.get("volume", 0.0) or 0.0), color=color, width=0.64, alpha=0.55)
        digits = _price_digits(symbol)
        entry_idx = 0
        for i, c in enumerate(recent):
            if int(c.get("at", 0)) >= opened_at:
                entry_idx = i
                break
        exit_idx = len(recent) - 1
        # entry line + arrow
        axis.axhline(entry, color="#f8fafc", linewidth=1.15, linestyle=(0, (5, 3)))
        axis.annotate(
            "ENTRY", xy=(entry_idx, entry), xytext=(entry_idx, entry),
            color="#f8fafc", fontsize=9, fontweight="bold", ha="center", va="bottom",
            bbox=dict(boxstyle="round,pad=0.25", facecolor="#08111f", edgecolor="#f8fafc", alpha=0.9),
        )
        arrow_color = "#22c55e" if is_buy else "#ef4444"
        axis.annotate(
            "", xy=(entry_idx - 1.2, entry), xytext=(entry_idx + 1.2, entry),
            arrowprops=dict(arrowstyle="->", color=arrow_color, lw=2.2),
        )
        # SL/TP lines
        axis.axhline(sl, color="#fb7185", linewidth=1.15, linestyle=(0, (5, 3)))
        axis.axhline(tp, color="#4ade80", linewidth=1.15, linestyle=(0, (5, 3)))
        axis.text(len(recent) - 0.5, sl, f"  SL {sl:.{digits}f}", color="#fb7185", fontsize=8, va="center")
        axis.text(len(recent) - 0.5, tp, f"  TP {tp:.{digits}f}", color="#4ade80", fontsize=8, va="center")
        # price path entry -> exit
        axis.plot([entry_idx, exit_idx], [entry, exit_price], color="#94a3b8", linewidth=1.2, linestyle="--", alpha=0.8)
        # outcome marker at the hit candle
        if result == "win":
            axis.scatter([exit_idx], [tp], s=140, color="#22c55e", marker="o", zorder=6, edgecolor="#08111f")
            axis.annotate("TP HIT", xy=(exit_idx, tp), xytext=(exit_idx + 1.2, tp),
                          color="#22c55e", fontsize=10, fontweight="bold", va="center",
                          arrowprops=dict(arrowstyle="->", color="#22c55e"))
        elif result == "loss":
            axis.scatter([exit_idx], [sl], s=150, color="#ef4444", marker="X", zorder=6, edgecolor="#08111f")
            axis.annotate("SL HIT", xy=(exit_idx, sl), xytext=(exit_idx + 1.2, sl),
                          color="#ef4444", fontsize=10, fontweight="bold", va="center",
                          arrowprops=dict(arrowstyle="->", color="#ef4444"))
        else:
            axis.scatter([exit_idx], [exit_price], s=120, color="#eab308", marker="o", zorder=6, edgecolor="#08111f")
            axis.annotate("EXPIRY", xy=(exit_idx, exit_price), xytext=(exit_idx + 1.2, exit_price),
                          color="#eab308", fontsize=10, fontweight="bold", va="center",
                          arrowprops=dict(arrowstyle="->", color="#eab308"))
        outcome_tag = "PROFIT" if result == "win" else ("LOSS" if result == "loss" else "PUSH")
        outcome_color = "#22c55e" if result == "win" else ("#ef4444" if result == "loss" else "#eab308")
        axis.text(0.01, 0.955, f"{outcome_tag}  |  {symbol}  {('BUY' if is_buy else 'SELL')}  |  {_timeframe_label(signal.get('timeframe', 0))}",
                  transform=axis.transAxes, color=outcome_color, fontsize=12, fontweight="bold",
                  bbox=dict(boxstyle="round,pad=0.35", facecolor="#08111f", edgecolor=outcome_color, alpha=0.95))
        axis.set_title(
            f"{symbol} | Trade result | Entry {entry:.{digits}f} -> Exit {exit_price:.{digits}f} | {outcome_tag}",
            color="#f8fafc", fontsize=12, fontweight="bold", loc="left", pad=12,
        )
        axis.set_ylabel("Price", color="#cbd5e1")
        volume_axis.set_ylabel("Volume", color="#cbd5e1")
        axis.set_xlim(-0.5, len(recent) + 2.5)
        axis.margins(y=0.12)
        tick_count = min(6, len(recent))
        tick_indexes = [round(i * (len(recent) - 1) / max(1, tick_count - 1)) for i in range(tick_count)]
        axis.set_xticks(tick_indexes); axis.set_xticklabels([])
        volume_axis.set_xticks(tick_indexes)
        volume_axis.set_xticklabels([candle_dates[i].strftime("%d %b %H:%M") for i in tick_indexes], color="#cbd5e1", fontsize=8)
        volume_axis.set_xlim(-0.5, len(recent) + 2.5)
        fig.subplots_adjust(left=0.08, right=0.98, top=0.92, bottom=0.12, hspace=0.05)
        image_path = tempfile.NamedTemporaryFile(prefix="ux50_result_", suffix=".png", delete=False).name
        fig.savefig(image_path, format="png")
        plt.close(fig)
        return image_path
    except Exception as exc:
        logger.warning("Result chart render failed: %s", exc)
        return None


def ux_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    return sum(trs[-period:]) / period


def ux_bollinger(closes, period=20, stdev_mult=2.0):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    mean = mid
    var = sum((x - mean) ** 2 for x in window) / period
    sd = var ** 0.5
    upper = mid + stdev_mult * sd
    lower = mid - stdev_mult * sd
    return mid, upper, lower

def ux_volatility_analysis(candles, lookback=20):
    if not candles or len(candles) < 5:
        return {"avg_range": 0.0, "atr": None, "regime": "unknown", "vol_score": 0.0}
    highs = [c.get("high", 0) for c in candles]
    lows = [c.get("low", 0) for c in candles]
    closes = [c.get("close", 0) for c in candles]
    ranges = [(h - l) for h, l in zip(highs[-lookback:], lows[-lookback:]) if h and l]
    avg_range = (sum(ranges) / len(ranges)) if ranges else 0.0
    atr = ux_atr(highs, lows, closes, period=14)
    if avg_range <= 0:
        regime = "unknown"
        vol_score = 0.0
    else:
        if avg_range < 0.25:
            regime = "calm"
        elif avg_range > 0.8:
            regime = "volatile"
        else:
            regime = "normal"
        vol_score = max(0.0, min(1.0, (avg_range / 1.25)))
    return {"avg_range": avg_range, "atr": atr, "regime": regime, "vol_score": vol_score}

def _cluster_sr_levels(levels, pct=0.0015):
    if not levels:
        return []
    levels = sorted(set(levels))
    clustered = []
    group = [levels[0]]
    for lv in levels[1:]:
        if (lv - group[-1]) / max(group[-1], 1e-9) <= pct:
            group.append(lv)
        else:
            clustered.append(sum(group) / len(group))
            group = [lv]
    clustered.append(sum(group) / len(group))
    return clustered

def _calc_ema(prices, period):
    if len(prices) < period:
        return sum(prices) / len(prices)
    multiplier = 2 / (period + 1)
    ema = [prices[0]]
    for price in prices[1:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema[-1]

def _calc_rsi(prices, period=14):
    if len(prices) <= period:
        return 50
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def _calc_atr(candles_list, period=14):
    if not candles_list or len(candles_list) < 2:
        return None
    highs = [c.get("high", c.get("max", 0)) for c in candles_list]
    lows = [c.get("low", c.get("min", 0)) for c in candles_list]
    closes = [c.get("close", c.get("close", 0)) for c in candles_list]
    trs = []
    for i in range(1, len(candles_list)):
        h = highs[i]; lo = lows[i]; prev_c = closes[i-1]
        tr = max(h - lo, abs(h - prev_c), abs(lo - prev_c))
        trs.append(tr)
    if len(trs) < 1:
        return None
    n = min(period, len(trs))
    return sum(trs[-n:]) / n

def _calc_adx(candles_list, period=14):
    if not candles_list or len(candles_list) < period + 2:
        return None
    highs = [c.get("high", c.get("max", 0)) for c in candles_list]
    lows = [c.get("low", c.get("min", 0)) for c in candles_list]
    closes = [c.get("close", c.get("close", 0)) for c in candles_list]
    trs = []; plus_dm = []; minus_dm = []
    for i in range(1, len(candles_list)):
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
        mdm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr); plus_dm.append(pdm); minus_dm.append(mdm)
    n = period
    atr = sum(trs[-n:]) / n if len(trs) >= n else None
    if not atr or atr == 0:
        return None
    pdi = 100.0 * (sum(plus_dm[-n:]) / n) / atr
    mdi = 100.0 * (sum(minus_dm[-n:]) / n) / atr
    denom = pdi + mdi
    if denom == 0:
        return 0.0
    dx = 100.0 * abs(pdi - mdi) / denom
    dx_hist = []
    window = min(len(trs), n * 2)
    for k in range(window - n, window):
        if k < n:
            continue
        atr_k = sum(trs[k-n:k]) / n
        if atr_k == 0:
            continue
        pdi_k = 100.0 * (sum(plus_dm[k-n:k]) / n) / atr_k
        mdi_k = 100.0 * (sum(minus_dm[k-n:k]) / n) / atr_k
        d = pdi_k + mdi_k
        if d == 0:
            continue
        dx_hist.append(100.0 * abs(pdi_k - mdi_k) / d)
    if not dx_hist:
        return dx
    return sum(dx_hist[-n:]) / min(n, len(dx_hist))

def ultra_hybrid_strategy(candles, current_candle, volatility_analysis, asset: str = "", timeframe: int = 1):
    try:
        if len(candles) < 30:
            print_status_message(
                "Insufficient candles for ultra binary analysis", "warning")
            return None

        closes = [candle['close'] for candle in candles[-30:]]
        opens = [candle['open'] for candle in candles[-30:]]
        highs = [candle['high'] for candle in candles[-30:]]
        lows = [candle['low'] for candle in candles[-30:]]

        current_close = current_candle['close']
        current_open = current_candle['open']
        current_high = current_candle['high']
        current_low = current_candle['low']

        if len(candles) >= 2:
            prev_candle = candles[-2]
            prev_close = prev_candle['close']
            prev_open = prev_candle['open']
            prev_high = prev_candle['high']
            prev_low = prev_candle['low']
        else:
            return None

        if len(candles) >= 3:
            prev2_candle = candles[-3]
            prev2_close = prev2_candle['close']
            prev2_open = prev2_candle['open']
            prev2_high = prev2_candle['high']
            prev2_low = prev2_candle['low']
        else:
            return None

        recent_high = max(highs[-20:]) if len(highs) >= 20 else max(highs)
        recent_low = min(lows[-20:]) if len(lows) >= 20 else min(lows)

        if recent_high != recent_low:
            price_position = (current_close - recent_low) / (recent_high - recent_low)
        else:
            price_position = 0.5

        current_body_size = abs(current_close - current_open)
        current_upper_shadow = current_high - max(current_open, current_close)
        current_lower_shadow = min(current_open, current_close) - current_low
        current_range = current_high - current_low
        current_range_safe = max(current_range, 1e-9)
        current_body_ratio = current_body_size / current_range_safe
        current_upper_ratio = current_upper_shadow / current_range_safe
        current_lower_ratio = current_lower_shadow / current_range_safe

        prev_body_size = abs(prev_close - prev_open)
        prev_upper_shadow = prev_high - max(prev_open, prev_close)
        prev_lower_shadow = min(prev_open, prev_close) - prev_low
        prev_range = prev_high - prev_low

        sma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else closes[-1]
        sma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else closes[-1]
        sma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else closes[-1]

        ema8 = _calc_ema(closes, 8)
        ema13 = _calc_ema(closes, 13)
        ema21 = _calc_ema(closes, 21)

        if not isinstance(volatility_analysis, dict):
            volatility_analysis = ux_volatility_analysis(candles)

        atr14 = volatility_analysis.get("atr", None)
        bb_mid, bb_upper, bb_lower = ux_bollinger(closes, period=20, stdev_mult=2.0)

        trend_up = (ema8 > ema13 > ema21)
        trend_down = (ema8 < ema13 < ema21)

        if atr14 is not None and current_close:
            atr_ratio = atr14 / max(abs(current_close), 1e-9)
            if atr_ratio < 0.00025:
                print_status_message("ATR too low (choppy market) - skipping signal", "warning")
                return None
            if atr_ratio > 0.02:
                print_status_message("ATR too high (spike risk) - skipping signal", "warning")
                return None

        bullish_pattern = False
        bearish_pattern = False
        pattern_strength = 0  # 1-10 scale, higher is stronger
        pattern_name = ""

        if current_close > current_open:  # Current candle is bullish
            if (current_lower_shadow > 2 * current_body_size and
                current_upper_shadow < 0.2 * current_body_size and
                price_position < 0.3):
                bullish_pattern = True
                pattern_strength = 9
                pattern_name = "Hammer"
                print_status_message("Bullish Hammer detected at support", "info")

            elif (current_open <= prev_close and
                  current_close > prev_open and
                  prev_close < prev_open and
                  current_body_size > 1.5 * prev_body_size and
                  price_position < 0.4):
                bullish_pattern = True
                pattern_strength = 8
                pattern_name = "Bullish Engulfing"
                print_status_message("Bullish Engulfing pattern detected", "info")

            elif (len(candles) >= 4 and
                  prev2_close < prev2_open and  # First candle bearish
                  abs(prev_close - prev_open) < 0.3 * abs(prev2_close - prev2_open) and  # Small middle candle
                  current_body_size > 0.6 * abs(prev2_close - prev2_open) and  # Large bullish candle
                  current_close > (prev2_open + prev2_close) / 2 and  # Closed above midpoint
                  price_position < 0.4):  # Near support
                bullish_pattern = True
                pattern_strength = 10
                pattern_name = "Morning Star"
                print_status_message("Morning Star pattern detected - strong bullish signal", "info")

            elif (prev_close < prev_open and  # Previous bearish
                  current_open > prev_close and
                  current_close < prev_open and
                  current_body_size < prev_body_size and
                  price_position < 0.35):  # Near support
                bullish_pattern = True
                pattern_strength = 6
                pattern_name = "Bullish Harami"
                print_status_message("Bullish Harami pattern detected", "info")

            elif (len(candles) >= 4 and
                  candles[-3]['close'] > candles[-3]['open'] and
                  candles[-2]['close'] > candles[-2]['open'] and
                  current_close > current_open and
                  candles[-2]['close'] > candles[-3]['close'] and
                  current_close > candles[-2]['close']):
                bullish_pattern = True
                pattern_strength = 8
                pattern_name = "Three White Soldiers"
                print_status_message("Three White Soldiers - strong bullish trend", "info")

            elif (prev_close < prev_open and  # Previous bearish
                  current_open < prev_low and  # Opened below previous low
                  current_close > (prev_open + prev_close) / 2 and  # Closed above 50% of previous candle
                  price_position < 0.4):  # Near support
                bullish_pattern = True
                pattern_strength = 7
                pattern_name = "Bullish Piercing Line"
                print_status_message("Bullish Piercing Line pattern detected", "info")

        elif current_close < current_open:  # Current candle is bearish
            if (current_upper_shadow > 2 * current_body_size and
                current_lower_shadow < 0.2 * current_body_size and
                price_position > 0.7):
                bearish_pattern = True
                pattern_strength = 9
                pattern_name = "Shooting Star"
                print_status_message("Bearish Shooting Star detected at resistance", "info")

            elif (current_open >= prev_close and
                  current_close < prev_open and
                  prev_close > prev_open and
                  current_body_size > 1.5 * prev_body_size and
                  price_position > 0.6):
                bearish_pattern = True
                pattern_strength = 8
                pattern_name = "Bearish Engulfing"
                print_status_message("Bearish Engulfing pattern detected", "info")

            elif (len(candles) >= 4 and
                  prev2_close > prev2_open and  # First candle bullish
                  abs(prev_close - prev_open) < 0.3 * abs(prev2_close - prev2_open) and  # Small middle candle
                  current_body_size > 0.6 * abs(prev2_close - prev2_open) and  # Large bearish candle
                  current_close < (prev2_open + prev2_close) / 2 and  # Closed below midpoint
                  price_position > 0.6):  # Near resistance
                bearish_pattern = True
                pattern_strength = 10
                pattern_name = "Evening Star"
                print_status_message("Evening Star pattern detected - strong bearish signal", "info")

            elif (prev_close > prev_open and  # Previous bullish
                  current_open < prev_close and
                  current_close > prev_open and
                  current_body_size < prev_body_size and
                  price_position > 0.65):  # Near resistance
                bearish_pattern = True
                pattern_strength = 6
                pattern_name = "Bearish Harami"
                print_status_message("Bearish Harami pattern detected", "info")

            elif (len(candles) >= 4 and
                  candles[-3]['close'] < candles[-3]['open'] and
                  candles[-2]['close'] < candles[-2]['open'] and
                  current_close < current_open and
                  candles[-2]['close'] < candles[-3]['close'] and
                  current_close < candles[-2]['close']):
                bearish_pattern = True
                pattern_strength = 8
                pattern_name = "Three Black Crows"
                print_status_message("Three Black Crows - strong bearish trend", "info")

            elif (prev_close > prev_open and  # Previous bullish
                  current_open > prev_high and  # Opened above previous high
                  current_close < (prev_open + prev_close) / 2 and  # Closed below 50% of previous candle
                  price_position > 0.6):  # Near resistance
                bearish_pattern = True
                pattern_strength = 7
                pattern_name = "Dark Cloud Cover"
                print_status_message("Dark Cloud Cover pattern detected", "info")

        if not bullish_pattern and not bearish_pattern:
            recent_break_high = max(highs[-6:-1]) if len(highs) >= 6 else recent_high
            recent_break_low = min(lows[-6:-1]) if len(lows) >= 6 else recent_low
            inside_bar = prev_high < prev2_high and prev_low > prev2_low

            if (current_lower_ratio >= 0.52 and current_body_ratio <= 0.32 and current_close > current_open and price_position < 0.45):
                bullish_pattern = True
                pattern_strength = 7.8
                pattern_name = "Strong Bullish Pin Bar"
                print_status_message("Strong Bullish Pin Bar detected", "info")
            elif (current_upper_ratio >= 0.52 and current_body_ratio <= 0.32 and current_close < current_open and price_position > 0.55):
                bearish_pattern = True
                pattern_strength = 7.8
                pattern_name = "Strong Bearish Pin Bar"
                print_status_message("Strong Bearish Pin Bar detected", "info")
            elif trend_up and current_close > recent_break_high and current_body_ratio >= 0.55:
                bullish_pattern = True
                pattern_strength = 8.2
                pattern_name = "Bullish Breakout Continuation"
                print_status_message("Bullish Breakout Continuation detected", "info")
            elif trend_down and current_close < recent_break_low and current_body_ratio >= 0.55:
                bearish_pattern = True
                pattern_strength = 8.2
                pattern_name = "Bearish Breakout Continuation"
                print_status_message("Bearish Breakout Continuation detected", "info")
            elif trend_up and prev_close < prev_open and current_close > prev_high and current_body_ratio >= 0.45:
                bullish_pattern = True
                pattern_strength = 7.4
                pattern_name = "Bullish Pullback Continuation"
                print_status_message("Bullish Pullback Continuation detected", "info")
            elif trend_down and prev_close > prev_open and current_close < prev_low and current_body_ratio >= 0.45:
                bearish_pattern = True
                pattern_strength = 7.4
                pattern_name = "Bearish Pullback Continuation"
                print_status_message("Bearish Pullback Continuation detected", "info")
            elif inside_bar and trend_up and current_close > prev_high and current_body_ratio >= 0.40:
                bullish_pattern = True
                pattern_strength = 7.2
                pattern_name = "Bullish Inside Bar Breakout"
                print_status_message("Bullish Inside Bar Breakout detected", "info")
            elif inside_bar and trend_down and current_close < prev_low and current_body_ratio >= 0.40:
                bearish_pattern = True
                pattern_strength = 7.2
                pattern_name = "Bearish Inside Bar Breakout"
                print_status_message("Bearish Inside Bar Breakout detected", "info")

        # ── UX V42 PROPER SWING S/R ─────────────────────────────────────
        # Old logic: price_position < 0.2 was too loose (just range position).
        # New logic: real swing pivot detection with strength scoring.
        # Support = confirmed swing low (lower than 2 bars each side, touched ≥2x).
        # Resistance = confirmed swing high (higher than 2 bars each side, touched ≥2x).

        support_levels = []
        resistance_levels = []

        # Multi-strength swing detection (look back up to 30 bars)
        _lb = min(30, len(lows) - 3)
        for i in range(2, _lb):
            # Swing low: lower than neighbours on both sides
            if lows[-i] < lows[-(i-1)] and lows[-i] < lows[-(i+1)] and lows[-i] < lows[-(i+2)]:
                support_levels.append(lows[-i])
        for i in range(2, _lb):
            # Swing high: higher than neighbours on both sides
            if highs[-i] > highs[-(i-1)] and highs[-i] > highs[-(i+1)] and highs[-i] > highs[-(i+2)]:
                resistance_levels.append(highs[-i])

        # Cluster nearby levels (within 0.15% = same zone)
# level clustering now calls module-level _cluster_sr_levels

        support_levels    = _cluster_sr_levels(support_levels)
        resistance_levels = _cluster_sr_levels(resistance_levels)

        # Proximity thresholds
        _TOUCH_NEAR   = 0.0025   # within 0.25% = touching zone
        _TOUCH_CLOSE  = 0.0012   # within 0.12% = very tight

        at_strong_support    = False
        at_strong_resistance = False
        sr_strength_bonus    = 0.0

        for level in support_levels:
            dist = abs(current_low - level) / max(current_close, 1e-9)
            if dist < _TOUCH_NEAR and current_close > current_open:
                at_strong_support = True
                # Stronger bonus the tighter the touch
                bonus = 2.2 if dist < _TOUCH_CLOSE else 1.5
                sr_strength_bonus = max(sr_strength_bonus, bonus)
                print_status_message(f"📍 Swing support zone: {level:.5f} dist={dist*100:.2f}%", "info")

        for level in resistance_levels:
            dist = abs(current_high - level) / max(current_close, 1e-9)
            if dist < _TOUCH_NEAR and current_close < current_open:
                at_strong_resistance = True
                bonus = 2.2 if dist < _TOUCH_CLOSE else 1.5
                sr_strength_bonus = max(sr_strength_bonus, bonus)
                print_status_message(f"📍 Swing resistance zone: {level:.5f} dist={dist*100:.2f}%", "info")

        if at_strong_support:
            print_status_message(f"✅ Price at confirmed swing support (bonus +{sr_strength_bonus:.1f})", "info")
            if bullish_pattern:
                pattern_strength += sr_strength_bonus
            else:
                bullish_pattern  = True
                pattern_strength = 7.0 + sr_strength_bonus * 0.4
                pattern_name     = "Swing Support Bounce"

        if at_strong_resistance:
            print_status_message(f"✅ Price at confirmed swing resistance (bonus +{sr_strength_bonus:.1f})", "info")
            if bearish_pattern:
                pattern_strength += sr_strength_bonus
            else:
                bearish_pattern  = True
                pattern_strength = 7.0 + sr_strength_bonus * 0.4
                pattern_name     = "Swing Resistance Rejection"

        # Fallback: simple range-position check only if NO swing levels found
        _no_swing_sup = len(support_levels) == 0
        _no_swing_res = len(resistance_levels) == 0
        if _no_swing_sup and price_position < 0.15 and current_close > current_open and not bullish_pattern:
            bullish_pattern  = True
            pattern_strength = 6.5
            pattern_name     = "Range Support Bounce"
            print_status_message("Range support bounce (fallback — no swing levels)", "info")
        if _no_swing_res and price_position > 0.85 and current_close < current_open and not bearish_pattern:
            bearish_pattern  = True
            pattern_strength = 6.5
            pattern_name     = "Range Resistance Rejection"
            print_status_message("Range resistance rejection (fallback — no swing levels)", "info")
        # ── END SWING S/R ───────────────────────────────────────────────

        recent_momentum_bars = 5
        recent_momentum = 0

        for i in range(1, min(recent_momentum_bars+1, len(closes))):
            if i < len(closes) and i+1 < len(closes):
                if closes[-i] > closes[-(i+1)]:
                    recent_momentum += 1
                elif closes[-i] < closes[-(i+1)]:
                    recent_momentum -= 1

        if recent_momentum >= 3:
            print_status_message(f"Strong bullish momentum detected: {recent_momentum}/5", "info")
            if bullish_pattern:
                pattern_strength += 2  # Boost existing pattern
        elif recent_momentum <= -3:
            print_status_message(f"Strong bearish momentum detected: {recent_momentum}/5", "info")
            if bearish_pattern:
                pattern_strength += 2  # Boost existing pattern

        ema12 = _calc_ema(closes, 12)
        ema26 = _calc_ema(closes, 26)
        macd_line = ema12 - ema26

        if len(closes) >= 35:  # Need enough data for MACD history
            macd_history = []
            for i in range(9):
                ema12_hist = _calc_ema(closes[-(35-i):-(26-i)], 12)
                ema26_hist = _calc_ema(closes[-(35-i):-(26-i)], 26)
                macd_history.append(ema12_hist - ema26_hist)
            signal_line = _calc_ema(macd_history, 9)
            macd_histogram = macd_line - signal_line

            if macd_line > signal_line and macd_histogram > 0 and macd_histogram > 0:
                print_status_message("MACD bullish crossover detected", "info")
                if bullish_pattern:
                    pattern_strength += 1.5
            elif macd_line < signal_line and macd_histogram < 0 and macd_histogram < 0:
                print_status_message("MACD bearish crossover detected", "info")
                if bearish_pattern:
                    pattern_strength += 1.5

# RSI now calls module-level _calc_rsi

# ATR now calls module-level _calc_atr

# ADX now calls module-level _calc_adx
        rsi = _calc_rsi(closes)
        rsi_history = []

        if len(closes) >= 30:
            for i in range(5):
                rsi_history.append(_calc_rsi(closes[:-i] if i > 0 else closes))

            if current_close > closes[-3] and rsi < rsi_history[2]:
                print_status_message("Bearish RSI divergence detected", "info")
                if bearish_pattern:
                    pattern_strength += 2
                else:
                    bearish_pattern = True
                    pattern_strength = 7
                    pattern_name = "RSI Bearish Divergence"

            if current_close < closes[-3] and rsi > rsi_history[2]:
                print_status_message("Bullish RSI divergence detected", "info")
                if bullish_pattern:
                    pattern_strength += 2
                else:
                    bullish_pattern = True
                    pattern_strength = 7
                    pattern_name = "RSI Bullish Divergence"

        if rsi < 30 and bullish_pattern:
            print_status_message(f"Oversold RSI ({rsi:.1f}) confirms bullish pattern", "info")
            pattern_strength += 2
        elif rsi > 70 and bearish_pattern:
            print_status_message(f"Overbought RSI ({rsi:.1f}) confirms bearish pattern", "info")
            pattern_strength += 2

        if len(closes) >= 14:
            k_period = 14
            lowest_low = min(lows[-k_period:])
            highest_high = max(highs[-k_period:])

            if highest_high != lowest_low:
                k_value = 100 * (current_close - lowest_low) / (highest_high - lowest_low)
            else:
                k_value = 50

            if k_value < 20 and bullish_pattern:
                print_status_message(f"Oversold Stochastic ({k_value:.1f}) confirms bullish signal", "info")
                pattern_strength += 1.5
            elif k_value > 80 and bearish_pattern:
                print_status_message(f"Overbought Stochastic ({k_value:.1f}) confirms bearish signal", "info")
                pattern_strength += 1.5

        if len(candles) >= 20:
            try:
                recent_high = max(h for h in highs[-20:] if h is not None)
                recent_low = min(l for l in lows[-20:] if l is not None)
                recent_close = closes[-1]

                pivot = (recent_high + recent_low + recent_close) / 3

                s1 = (2 * pivot) - recent_high
                s2 = pivot - (recent_high - recent_low)
                s3 = s2 - (recent_high - recent_low)
                r1 = (2 * pivot) - recent_low
                r2 = pivot + (recent_high - recent_low)
                r3 = r2 + (recent_high - recent_low)

                near_s1 = abs(current_low - s1) / current_close < 0.003  # Within 0.3%
                near_s2 = abs(current_low - s2) / current_close < 0.003
                near_s3 = abs(current_low - s3) / current_close < 0.003
                near_r1 = abs(current_high - r1) / current_close < 0.003
                near_r2 = abs(current_high - r2) / current_close < 0.003
                near_r3 = abs(current_high - r3) / current_close < 0.003

                if (near_s1 or near_s2 or near_s3) and current_close > current_open:
                    print_status_message("Price bounced off pivot support - strong bullish signal", "info")
                    if bullish_pattern:
                        pattern_strength += 2.5  # Increased from 2
                    else:
                        bullish_pattern = True
                        pattern_strength = 8.5  # Increased from 8
                        pattern_name = "Pivot Support Bounce"

                if (near_r1 or near_r2 or near_r3) and current_close < current_open:
                    print_status_message("Price rejected at pivot resistance - strong bearish signal", "info")
                    if bearish_pattern:
                        pattern_strength += 2.5  # Increased from 2
                    else:
                        bearish_pattern = True
                        pattern_strength = 8.5  # Increased from 8
                        pattern_name = "Pivot Resistance Rejection"
            except Exception as e:
                print_status_message(f"Error in pivot point calculation: {str(e)}", "warning")

        trend_strength = 0
        trend_direction = None

        if ema8 > ema13 > ema21:
            trend_direction = "up"
            trend_strength = (ema8 - ema21) / ema21 * 100  # Percentage difference
            print_status_message(f"Strong uptrend detected (EMA alignment), strength: {trend_strength:.2f}%", "info")
            if bullish_pattern and trend_strength > 0.2:
                pattern_strength += 1.5
        elif ema8 < ema13 < ema21:
            trend_direction = "down"
            trend_strength = (ema21 - ema8) / ema21 * 100  # Percentage difference
            print_status_message(f"Strong downtrend detected (EMA alignment), strength: {trend_strength:.2f}%", "info")
            if bearish_pattern and trend_strength > 0.2:
                pattern_strength += 1.5

        if 'volume' in candles[0]:
            volumes = [candle.get('volume', 0) for candle in candles[-10:]]
            avg_volume = sum(volumes) / len(volumes)
            current_volume = candles[-1].get('volume', 0)

            if current_volume > avg_volume * 1.5:  # 50% above average
                volume_confirmation = 1.5
                print_status_message(f"High volume confirmation detected ({current_volume} vs avg {avg_volume})", "info")
                if bullish_pattern and current_close > current_open:
                    pattern_strength += volume_confirmation
                if bearish_pattern and current_close < current_open:
                    pattern_strength += volume_confirmation

        confidence_threshold = volatility_analysis.get('confidence_threshold', 45)
        print_status_message(f"Pattern detected: {pattern_name}, Strength: {pattern_strength}/10", "info")

        is_volatile = volatility_analysis.get('avg_range', 0) > 1.0
        is_calm = volatility_analysis.get('avg_range', 0) < 0.2
        if is_volatile and pattern_strength > 0:
            print_status_message("High volatility detected — applying extra caution to pattern strength", "info")
            pattern_strength -= 1.2
        elif is_calm and pattern_strength > 0:
            print_status_message("Low volatility detected — slight pattern boost only", "info")
            pattern_strength += 0.4

        pattern_tier_info = get_pattern_tier_info(pattern_name)
        pattern_tier = pattern_tier_info["tier"]
        confidence = float(pattern_strength) + float(pattern_tier_info.get("confidence_bonus", 0.0))

        if current_body_ratio >= 0.58:
            confidence += 0.8
        elif current_body_ratio < 0.18:
            confidence -= 0.9

        if bullish_pattern and current_lower_ratio >= 0.45:
            confidence += 0.35
        if bearish_pattern and current_upper_ratio >= 0.45:
            confidence += 0.35

        indecision_cluster = 0
        for c in candles[-3:]:
            _rng = max(float(c.get('high', 0)) - float(c.get('low', 0)), 1e-9)
            _body_ratio = abs(float(c.get('close', 0)) - float(c.get('open', 0))) / _rng
            if _body_ratio < 0.18:
                indecision_cluster += 1
        if indecision_cluster >= 2:
            confidence -= 1.25
            print_status_message("Indecision cluster detected — reducing setup quality", "warning")

        if pattern_tier == "T3":
            print_status_message("Reject-only / indecision pattern state detected — skipping signal", "warning")
            return None

        if bullish_pattern:
            confidence += 1.0 if trend_up else -0.5
        if bearish_pattern:
            confidence += 1.0 if trend_down else -0.5

        if bb_lower is not None and bb_upper is not None:
            if bullish_pattern and current_close <= (bb_lower * 1.003):
                confidence += 0.8
            if bearish_pattern and current_close >= (bb_upper * 0.997):
                confidence += 0.8

        regime = volatility_analysis.get("regime", "normal")
        if regime == "volatile":
            confidence -= 0.7
        elif regime == "calm":
            confidence += 0.3

        adx14 = _calc_adx(candles, period=14)
        if adx14 is not None:
            if adx14 < 18:
                confidence -= 0.8
                print_status_message(f"Choppy market (ADX={adx14:.1f}) - reducing confidence", "info")
            elif adx14 > 25:
                confidence += 0.5
                print_status_message(f"Strong trend (ADX={adx14:.1f}) - boosting confidence", "info")

        if bullish_pattern and rsi is not None and rsi > 70:
            confidence -= 0.7
        if bearish_pattern and rsi is not None and rsi < 30:
            confidence -= 0.7

        # ── UX V42 HTF HARD BLOCK ───────────────────────────────────────
        # Higher timeframe (5-candle aggregate = 5m on 1m chart) must
        # AGREE with signal direction. Opposing HTF → hard block.
        # Exception: T1 pattern with confluence_net >= 4 may pass with penalty.
        htf_trend_up = None
        htf_trend_down = None
        family = str(pattern_tier_info.get("family") or "")
        try:
            if len(candles) >= 60:
                group = 5
                agg = []
                for i in range(0, len(candles) - group + 1, group):
                    chunk = candles[i:i+group]
                    if not chunk:
                        continue
                    agg.append({
                        "open": chunk[0].get("open"),
                        "close": chunk[-1].get("close"),
                        "high": max(c.get("high", c.get("max", 0)) for c in chunk),
                        "low": min(c.get("low", c.get("min", 0)) for c in chunk),
                        "volume": sum(c.get("volume", 0) for c in chunk)
                    })
                if len(agg) >= 30:
                    htf_closes = [c["close"] for c in agg if c.get("close") is not None]
                    htf_ema21 = _calc_ema(htf_closes, 21)
                    htf_ema55 = _calc_ema(htf_closes, 55)
                    htf_rsi   = _calc_rsi(htf_closes, period=14) if len(htf_closes) >= 15 else 50
                    htf_trend_up   = htf_ema21 > htf_ema55
                    htf_trend_down = htf_ema21 < htf_ema55
                    htf_neutral    = abs(htf_ema21 - htf_ema55) / max(htf_ema55, 1e-9) < 0.0008

                    if not htf_neutral:
                        if bullish_pattern and htf_trend_down:
                            # Strong reversal T1 patterns can survive with -1.5 penalty
                            if pattern_tier == "T1" and family in {"engulfing","hammer","star reversal","pivot bounce","pin bar"}:
                                confidence -= 1.5
                                print_status_message(f"⚠️ HTF bearish vs bullish T1 reversal — penalty -1.5", "warning")
                            else:
                                print_status_message(f"🚫 HTF HARD BLOCK: HTF bearish, bullish signal ({pattern_name}) blocked", "warning")
                                return None
                        if bearish_pattern and htf_trend_up:
                            if pattern_tier == "T1" and family in {"engulfing","shooting star","star reversal","pivot bounce","pin bar"}:
                                confidence -= 1.5
                                print_status_message(f"⚠️ HTF bullish vs bearish T1 reversal — penalty -1.5", "warning")
                            else:
                                print_status_message(f"🚫 HTF HARD BLOCK: HTF bullish, bearish signal ({pattern_name}) blocked", "warning")
                                return None
                    else:
                        print_status_message("HTF neutral — no bias applied", "info")

                    # HTF RSI extreme filter
                    if bullish_pattern and htf_rsi > 72:
                        confidence -= 0.8
                        print_status_message(f"⚠️ HTF RSI overbought ({htf_rsi:.0f}) — penalty", "warning")
                    elif bearish_pattern and htf_rsi < 28:
                        confidence -= 0.8
                        print_status_message(f"⚠️ HTF RSI oversold ({htf_rsi:.0f}) — penalty", "warning")

                    last_signal_meta["htf_trend_up"]   = htf_trend_up
                    last_signal_meta["htf_trend_down"]  = htf_trend_down
                    last_signal_meta["htf_rsi"]         = round(htf_rsi, 1)
        except Exception as _htf_ex:
            try: logger.warning(f"HTF block error: {_htf_ex}")
            except Exception: pass
        # ── END HTF HARD BLOCK ──────────────────────────────────────────

        atr_fallback = volatility_analysis.get("atr", None)
        if atr_fallback is None:
            atr_fallback = _calc_atr(candles, period=14)
        try:
            ema_sep = abs(ema8 - ema21) if (ema8 is not None and ema21 is not None) else 0.0
            if atr_fallback and atr_fallback > 0 and ema_sep / atr_fallback < 0.12:
                confidence -= 0.6
        except Exception:
            pass

        pair_learning = get_pair_pattern_learning_adjustment(
            asset, timeframe, pattern_name,
            "UP" if bullish_pattern else "DOWN" if bearish_pattern else "",
            lookback=220
        )
        confidence += float(pair_learning.get("score", 0.0))

        family = str(pattern_tier_info.get("family") or "")
        reversal_families = {"engulfing", "hammer", "shooting star", "star reversal", "pivot bounce", "pin bar", "support/resistance bounce", "rsi divergence"}
        continuation_families = {"three soldiers/crows", "breakout continuation", "pullback continuation", "inside bar breakout"}
        setup_quality = 0.0
        setup_notes = []

        try:
            recent_ranges = [max(float(h) - float(l), 1e-9) for h, l in zip(highs[-5:], lows[-5:])]
            recent_bodies = [abs(float(c.get("close", 0)) - float(c.get("open", 0))) for c in candles[-5:]]
            avg_range5 = sum(recent_ranges) / max(len(recent_ranges), 1)
            avg_body5 = sum(recent_bodies) / max(len(recent_bodies), 1)
        except Exception:
            avg_range5 = current_range_safe
            avg_body5 = current_body_size

        close_near_high = current_close >= (current_high - current_range_safe * 0.22)
        close_near_low = current_close <= (current_low + current_range_safe * 0.22)

        if bullish_pattern and current_body_ratio >= 0.50 and close_near_high:
            setup_quality += 0.90
            setup_notes.append("bull close strength")
        elif bearish_pattern and current_body_ratio >= 0.50 and close_near_low:
            setup_quality += 0.90
            setup_notes.append("bear close strength")

        if bullish_pattern and current_upper_ratio >= 0.34 and current_body_ratio <= 0.42:
            setup_quality -= 0.75
            setup_notes.append("upper wick trap")
        if bearish_pattern and current_lower_ratio >= 0.34 and current_body_ratio <= 0.42:
            setup_quality -= 0.75
            setup_notes.append("lower wick trap")

        if family in continuation_families:
            if (bullish_pattern and trend_up) or (bearish_pattern and trend_down):
                setup_quality += 0.75
                setup_notes.append("trend continuation aligned")
            else:
                setup_quality -= 0.60
                setup_notes.append("continuation vs trend")

        if family in reversal_families:
            if bullish_pattern and price_position <= 0.38:
                setup_quality += 0.65
                setup_notes.append("bullish at value zone")
            elif bearish_pattern and price_position >= 0.62:
                setup_quality += 0.65
                setup_notes.append("bearish at premium zone")
            else:
                setup_quality -= 0.35
                setup_notes.append("reversal away from edge")

        if pattern_name == "Bullish Breakout Continuation":
            if current_close > recent_break_high + (current_range_safe * 0.08):
                setup_quality += 0.45
                setup_notes.append("clean bull breakout")
            else:
                setup_quality -= 0.35
                setup_notes.append("weak bull breakout")
        elif pattern_name == "Bearish Breakout Continuation":
            if current_close < recent_break_low - (current_range_safe * 0.08):
                setup_quality += 0.45
                setup_notes.append("clean bear breakout")
            else:
                setup_quality -= 0.35
                setup_notes.append("weak bear breakout")

        if current_range > avg_range5 * 1.15 and current_body_size > max(avg_body5 * 1.08, current_range_safe * 0.42):
            setup_quality += 0.35
            setup_notes.append("impulse candle")
        elif current_range < avg_range5 * 0.75 and current_body_ratio < 0.28:
            setup_quality -= 0.45
            setup_notes.append("thin candle")

        last3_dirs = []
        for c in candles[-3:]:
            if float(c.get("close", 0)) > float(c.get("open", 0)):
                last3_dirs.append(1)
            elif float(c.get("close", 0)) < float(c.get("open", 0)):
                last3_dirs.append(-1)
            else:
                last3_dirs.append(0)
        alternating_last3 = len(last3_dirs) == 3 and last3_dirs[0] != 0 and last3_dirs[1] != 0 and last3_dirs[2] != 0 and last3_dirs[0] != last3_dirs[1] and last3_dirs[1] != last3_dirs[2]
        if alternating_last3:
            setup_quality -= 0.35
            setup_notes.append("choppy sequence")

        if bb_lower is not None and bb_upper is not None:
            if bullish_pattern and family in reversal_families and current_close <= bb_mid:
                setup_quality += 0.25
            if bearish_pattern and family in reversal_families and current_close >= bb_mid:
                setup_quality += 0.25

        confidence += setup_quality

        # ── UX V42 CONFLUENCE GATE ──────────────────────────────────────
        # Minimum 3 confirming signals required for any trade.
        # Each indicator votes +1 (confirm) or -1 (oppose).
        # Net score < 1 → block; score >= 3 → confidence boost.
        confluence_votes = []
        confluence_labels = []

        # Vote 1: EMA trend alignment
        if bullish_pattern:
            if trend_up:
                confluence_votes.append(1); confluence_labels.append("EMA_trend_up✅")
            elif trend_down:
                confluence_votes.append(-1); confluence_labels.append("EMA_trend_down❌")
            else:
                confluence_votes.append(0); confluence_labels.append("EMA_neutral")
        else:
            if trend_down:
                confluence_votes.append(1); confluence_labels.append("EMA_trend_down✅")
            elif trend_up:
                confluence_votes.append(-1); confluence_labels.append("EMA_trend_up❌")
            else:
                confluence_votes.append(0); confluence_labels.append("EMA_neutral")

        # Vote 2: RSI zone
        if rsi is not None:
            if bullish_pattern and rsi < 55:
                confluence_votes.append(1); confluence_labels.append(f"RSI_ok({rsi:.0f})✅")
            elif bullish_pattern and rsi > 68:
                confluence_votes.append(-1); confluence_labels.append(f"RSI_overbought({rsi:.0f})❌")
            elif bearish_pattern and rsi > 45:
                confluence_votes.append(1); confluence_labels.append(f"RSI_ok({rsi:.0f})✅")
            elif bearish_pattern and rsi < 32:
                confluence_votes.append(-1); confluence_labels.append(f"RSI_oversold({rsi:.0f})❌")
            else:
                confluence_votes.append(0); confluence_labels.append(f"RSI_neutral({rsi:.0f})")

        # Vote 3: ADX trend strength
        if adx14 is not None:
            if adx14 >= 20:
                confluence_votes.append(1); confluence_labels.append(f"ADX_trend({adx14:.0f})✅")
            elif adx14 < 16:
                confluence_votes.append(-1); confluence_labels.append(f"ADX_choppy({adx14:.0f})❌")
            else:
                confluence_votes.append(0); confluence_labels.append(f"ADX_weak({adx14:.0f})")

        # Vote 4: BB position
        if bb_lower is not None and bb_upper is not None and bb_mid is not None:
            if bullish_pattern and current_close < bb_mid:
                confluence_votes.append(1); confluence_labels.append("BB_value_zone✅")
            elif bullish_pattern and current_close > bb_upper * 0.998:
                confluence_votes.append(-1); confluence_labels.append("BB_at_upper❌")
            elif bearish_pattern and current_close > bb_mid:
                confluence_votes.append(1); confluence_labels.append("BB_premium_zone✅")
            elif bearish_pattern and current_close < bb_lower * 1.002:
                confluence_votes.append(-1); confluence_labels.append("BB_at_lower❌")
            else:
                confluence_votes.append(0); confluence_labels.append("BB_mid")

        # Vote 5: Momentum direction
        if bullish_pattern:
            if recent_momentum >= 2:
                confluence_votes.append(1); confluence_labels.append(f"MOM_bull({recent_momentum})✅")
            elif recent_momentum <= -3:
                confluence_votes.append(-1); confluence_labels.append(f"MOM_bear({recent_momentum})❌")
            else:
                confluence_votes.append(0); confluence_labels.append("MOM_neutral")
        else:
            if recent_momentum <= -2:
                confluence_votes.append(1); confluence_labels.append(f"MOM_bear({recent_momentum})✅")
            elif recent_momentum >= 3:
                confluence_votes.append(-1); confluence_labels.append(f"MOM_bull({recent_momentum})❌")
            else:
                confluence_votes.append(0); confluence_labels.append("MOM_neutral")

        # Vote 6: Price structure (swing S/R proximity)
        near_sr_bull = any(abs(current_low - lv) / max(current_close, 1e-9) < 0.004 for lv in support_levels[:5])
        near_sr_bear = any(abs(current_high - lv) / max(current_close, 1e-9) < 0.004 for lv in resistance_levels[:5])
        if bullish_pattern and near_sr_bull:
            confluence_votes.append(1); confluence_labels.append("SR_support✅")
        elif bearish_pattern and near_sr_bear:
            confluence_votes.append(1); confluence_labels.append("SR_resistance✅")
        else:
            confluence_votes.append(0); confluence_labels.append("SR_none")

        confluence_net = sum(confluence_votes)
        confluence_positive = sum(1 for v in confluence_votes if v > 0)
        confluence_negative = sum(1 for v in confluence_votes if v < 0)

        print_status_message(
            f"Confluence: net={confluence_net} pos={confluence_positive} neg={confluence_negative} | {', '.join(confluence_labels)}",
            "info"
        )

        # Mode-based confluence thresholds
        _conf_rules = {
            "SAFE":       {"min_net": 2, "min_pos": 3, "max_neg": 1},
            "NORMAL":     {"min_net": 2, "min_pos": 3, "max_neg": 1},
            "AGGRESSIVE": {"min_net": 0, "min_pos": 1, "max_neg": 3},
            "ELITE":      {"min_net": -1,"min_pos": 1, "max_neg": 4},
            "CUSTOM":     {"min_net": 1, "min_pos": 2, "max_neg": 2},
        }
        _cr = _conf_rules.get(TRADING_MODE, _conf_rules["NORMAL"])

        if confluence_net < _cr["min_net"] or confluence_positive < _cr["min_pos"] or confluence_negative > _cr["max_neg"]:
            print_status_message(
                f"❌ CONFLUENCE GATE [{TRADING_MODE}]: net={confluence_net} pos={confluence_positive} neg={confluence_negative} "
                f"(need net≥{_cr['min_net']} pos≥{_cr['min_pos']} neg≤{_cr['max_neg']}) — signal blocked",
                "warning"
            )
            return None

        # Confidence boost for strong confluence (mode-scaled)
        _boost_threshold = {"SAFE": 4, "NORMAL": 4, "AGGRESSIVE": 3, "ELITE": 2, "CUSTOM": 3}.get(TRADING_MODE, 3)
        if confluence_positive >= _boost_threshold + 1 and confluence_negative == 0:
            confidence += 0.80
            print_status_message(f"🔥 Strong confluence ({confluence_positive}/6) [{TRADING_MODE}] — +0.80 boost", "info")
        elif confluence_positive >= _boost_threshold and confluence_negative <= 1:
            confidence += 0.40
            print_status_message(f"✅ Good confluence ({confluence_positive}/6) [{TRADING_MODE}] — +0.40 boost", "info")

        last_signal_meta["confluence_net"]  = confluence_net
        last_signal_meta["confluence_pos"]  = confluence_positive
        last_signal_meta["confluence_neg"]  = confluence_negative
        last_signal_meta["confluence_labels"] = confluence_labels
        # ── END CONFLUENCE GATE ──────────────────────────────────────────

        adaptive_state = get_recent_adaptive_state(asset, timeframe, lookback=20)
        if adaptive_state.get("pause"):
            print_status_message("Drawdown protection pause active — skipping setup", "warning")
            return None
        if adaptive_state.get("elite_only") and pattern_tier != "T1":
            print_status_message("Drawdown protection active — only Tier-1 patterns allowed", "warning")
            return None

        confidence = max(0.0, min(12.8, confidence))
        required = 7.5 if regime == "volatile" else 6.65
        if pattern_tier == "T1":
            required -= 0.50
        elif pattern_tier == "T2":
            required += 0.05
        else:
            required += 0.75
        required += float(adaptive_state.get("required_delta", 0.0))
        if adx14 is not None and adx14 < 18:
            required += 0.35
        if current_body_ratio < 0.22:
            required += 0.35
        elif current_body_ratio >= 0.56:
            required -= 0.12
        if setup_quality >= 1.50:
            required -= 0.35
        elif setup_quality <= -0.85:
            required += 0.45
        if float(pair_learning.get("score", 0.0)) <= -1.5:
            required += 0.35
        elif float(pair_learning.get("score", 0.0)) >= 1.5:
            required -= 0.18

        if bullish_pattern and confidence >= required:
            print_status_message(
                f"UX PRO SIGNAL ✅ BULLISH: {pattern_name} | confidence {confidence:.1f}/{required}",
                "success"
            )
            last_signal_meta["pattern_name"] = pattern_name
            last_signal_meta["pattern_tier"] = pattern_tier
            last_signal_meta["pattern_family"] = pattern_tier_info.get("family")
            last_signal_meta["why"] = f"{pattern_name} + Trend/Vol confirm"
            last_signal_meta["confidence"] = float(round(min(99.0, max(60.0, 55.0 + (confidence * 4.25))), 1))
            last_signal_meta["technical_pattern_strength"] = round(pattern_strength, 2)
            last_signal_meta["technical_confidence_raw"] = round(confidence, 2)
            last_signal_meta["pair_learning_score"] = round(float(pair_learning.get("score", 0.0)), 2)
            last_signal_meta["pair_learning_reasons"] = list(pair_learning.get("reasons", []))
            last_signal_meta["adaptive_mode"] = adaptive_state.get("mode")
            last_signal_meta["setup_quality"] = round(setup_quality, 2)
            last_signal_meta["setup_notes"] = list(setup_notes[:5])
            try:
                last_signal_meta["recommended"] = bool(float(last_signal_meta.get("confidence") or 0) >= 90.0)
            except Exception:
                last_signal_meta["recommended"] = False
            return "UP 🟩"
        elif bearish_pattern and confidence >= required:
            print_status_message(
                f"UX PRO SIGNAL ✅ BEARISH: {pattern_name} | confidence {confidence:.1f}/{required}",
                "success"
            )
            last_signal_meta["pattern_name"] = pattern_name
            last_signal_meta["pattern_tier"] = pattern_tier
            last_signal_meta["pattern_family"] = pattern_tier_info.get("family")
            last_signal_meta["why"] = f"{pattern_name} + Trend/Vol confirm"
            last_signal_meta["confidence"] = float(round(min(99.0, max(60.0, 55.0 + (confidence * 4.25))), 1))
            last_signal_meta["technical_pattern_strength"] = round(pattern_strength, 2)
            last_signal_meta["technical_confidence_raw"] = round(confidence, 2)
            last_signal_meta["pair_learning_score"] = round(float(pair_learning.get("score", 0.0)), 2)
            last_signal_meta["pair_learning_reasons"] = list(pair_learning.get("reasons", []))
            last_signal_meta["adaptive_mode"] = adaptive_state.get("mode")
            last_signal_meta["setup_quality"] = round(setup_quality, 2)
            last_signal_meta["setup_notes"] = list(setup_notes[:5])
            try:
                last_signal_meta["recommended"] = bool(float(last_signal_meta.get("confidence") or 0) >= 90.0)
            except Exception:
                last_signal_meta["recommended"] = False
            return "DOWN 🔻"
        else:
            print_status_message(
                f"No strong signal (pattern_strength={pattern_strength}/15, confidence={confidence:.1f}, required={required})",
                "warning"
            )
            return None

    except Exception as e:
        print_status_message(f"Error in ultra hybrid strategy: {str(e)}", "error")
        traceback.print_exc()
        return None
PREMIUM_STATS_FILE = os.getenv("EXNESS_PREMIUM_STATS_FILE", "premium_signal_stats.json")
OPEN_SIGNALS_FILE = os.getenv("EXNESS_OPEN_SIGNALS_FILE", "premium_open_signals.json")
SIGNAL_HISTORY_FILE = os.getenv("EXNESS_SIGNAL_HISTORY_FILE", "premium_signal_history.json")


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh)
        return value
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path, value):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _symbol_stats(symbol):
    stats = _read_json(PREMIUM_STATS_FILE, {})
    row = stats.setdefault(str(symbol), {"signals": 0, "wins": 0, "losses": 0, "pushes": 0, "consecutive_losses": 0})
    return stats, row


def _quality_status(symbol):
    stats, row = _symbol_stats(symbol)
    signals = int(row.get("signals", 0))
    wins = int(row.get("wins", 0))
    losses = int(row.get("losses", 0))
    min_samples = max(10, int(os.getenv("EXNESS_MIN_STATS_SAMPLES", "20")))
    min_winrate = float(os.getenv("EXNESS_MIN_SYMBOL_WINRATE", "48"))
    cooldown_losses = max(2, int(os.getenv("EXNESS_MAX_CONSECUTIVE_LOSSES", "3")))
    winrate = (wins / max(1, wins + losses)) * 100.0
    if int(row.get("consecutive_losses", 0)) >= cooldown_losses:
        return False, f"{row.get('consecutive_losses')} consecutive losses", row
    if signals >= min_samples and (wins + losses) >= min_samples and winrate < min_winrate:
        return False, f"win-rate {winrate:.1f}% below {min_winrate:.1f}%", row
    return True, f"quality {winrate:.1f}% over {signals} signals", row


def _record_open_signal(
    symbol,
    direction,
    entry,
    sl,
    tp,
    timeframe,
    confidence,
    notification_sent=False,
    chart_sent=False,
):
    opened_at = int(time.time())
    signal = {
        "id": f"{symbol}-{opened_at}",
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "timeframe": timeframe,
        "confidence": confidence,
        "opened_at": opened_at,
        "status": "open",
        "last_status": "AT ENTRY",
        "current_price": entry,
        "notification_sent": bool(notification_sent),
        "chart_sent": bool(chart_sent),
    }
    opened = _read_json(OPEN_SIGNALS_FILE, [])
    opened.append(signal)
    _write_json(OPEN_SIGNALS_FILE, opened[-500:])
    history = _read_json(SIGNAL_HISTORY_FILE, [])
    history.append(signal)
    _write_json(SIGNAL_HISTORY_FILE, history[-1000:])
    return signal


def _settle_signals_from_candles(candles, symbol=None, timeframe=None):
    opened = _read_json(OPEN_SIGNALS_FILE, [])
    if not opened or not candles:
        return 0
    remaining = []
    changed = 0
    stats = _read_json(PREMIUM_STATS_FILE, {})
    history = _read_json(SIGNAL_HISTORY_FILE, [])
    for signal in opened:
        if symbol and str(signal.get("symbol", "")).upper() != str(symbol).upper():
            remaining.append(signal)
            continue
        if timeframe and int(signal.get("timeframe", 0) or 0) != int(timeframe):
            remaining.append(signal)
            continue
        future = [c for c in candles if int(c.get("at", 0)) > int(signal.get("opened_at", 0))]
        result = None
        result_basis = None
        exit_price = None
        exit_at = int(time.time())
        direction = str(signal.get("direction", "")).upper()
        lookahead = max(1, int(os.getenv("EXNESS_OUTCOME_LOOKAHEAD", "12")))
        for candle in future[:lookahead]:
            high = float(candle.get("high", 0.0)); low = float(candle.get("low", 0.0))
            sl = float(signal.get("sl", 0.0)); tp = float(signal.get("tp", 0.0))
            is_buy = "UP" in direction or "BUY" in direction
            hit_sl = low <= sl if is_buy else high >= sl
            hit_tp = high >= tp if is_buy else low <= tp
            if hit_sl and hit_tp:
                result = "loss"  # conservative when both occur in one candle
                result_basis = "SL+TP same candle (conservative SL)"
                exit_price = sl
                exit_at = int(candle.get("at", 0))
                break
            if hit_sl:
                result = "loss"
                result_basis = "SL hit"
                exit_price = sl
                exit_at = int(candle.get("at", 0))
                break
            if hit_tp:
                result = "win"
                result_basis = "TP hit"
                exit_price = tp
                exit_at = int(candle.get("at", 0))
                break
        if result is None and len(future) >= lookahead:
            # Do not leave a signal open forever. At expiry, compare the last
            # closed candle with entry and report the result transparently.
            expiry_candle = future[lookahead - 1]
            exit_price = float(expiry_candle.get("close", signal.get("entry", 0.0)))
            exit_at = int(expiry_candle.get("at", 0))
            entry_price = float(signal.get("entry", 0.0))
            is_buy = "UP" in direction or "BUY" in direction
            if abs(exit_price - entry_price) < max(abs(entry_price) * 1e-6, 1e-9):
                result = "push"
            else:
                result = "win" if (exit_price > entry_price if is_buy else exit_price < entry_price) else "loss"
            result_basis = f"{lookahead} candle expiry close"
        if result is None:
            remaining.append(signal)
            continue
        symbol = str(signal.get("symbol"))
        row = stats.setdefault(symbol, {"signals": 0, "wins": 0, "losses": 0, "pushes": 0, "consecutive_losses": 0})
        row["signals"] = int(row.get("signals", 0))
        row["wins"] = int(row.get("wins", 0))
        row["losses"] = int(row.get("losses", 0))
        row["pushes"] = int(row.get("pushes", 0))
        row["signals"] += 1
        if result == "win":
            row["wins"] += 1; row["consecutive_losses"] = 0
        elif result == "loss":
            row["losses"] += 1; row["consecutive_losses"] = int(row.get("consecutive_losses", 0)) + 1
        else:
            row["pushes"] += 1; row["consecutive_losses"] = 0
        settled_at = int(time.time())
        row["last_result"] = result
        row["last_settled_at"] = settled_at
        for history_row in reversed(history):
            if history_row.get("id") == signal.get("id"):
                history_row.update(
                    {
                        "status": "settled",
                        "result": result,
                        "result_basis": result_basis,
                        "exit_price": exit_price,
                        "settled_at": settled_at,
                    }
                )
                break
        outcome_label = {"win": "PROFIT", "loss": "LOSS", "push": "PUSH"}[result]
        result_icon = {"win": "✅", "loss": "❌", "push": "➖"}[result]
        _telegram_typing()
        telegram_send(
            f"{result_icon} *{outcome_label}*\n"
            f"{symbol} • {direction} • {_timeframe_label(signal.get('timeframe', 0))}\n"
            f"Entry: `{signal.get('entry')}` → Exit: `{exit_price}`\n"
            f"{result_basis}"
        )
        result_chart_path = _render_result_chart(signal, candles, result, exit_price, exit_at)
        if result_chart_path:
            telegram_send_photo(
                result_chart_path,
                f"{symbol} {outcome_label} | {result_basis} | Entry {signal.get('entry')} -> {exit_price}",
            )
            try:
                os.unlink(result_chart_path)
            except OSError:
                pass
        changed += 1
    if changed:
        _write_json(PREMIUM_STATS_FILE, stats)
        _write_json(OPEN_SIGNALS_FILE, remaining)
        _write_json(SIGNAL_HISTORY_FILE, history[-1000:])
    return changed


async def _settle_open_signals_with_bridge(bridge):
    """Settle each open signal with candles from its own symbol/timeframe."""
    opened = _read_json(OPEN_SIGNALS_FILE, [])
    groups = {}
    for signal in opened:
        key = (str(signal.get("symbol", "")).upper(), int(signal.get("timeframe", 60) or 60))
        if key[0]:
            groups.setdefault(key, None)
    changed = 0
    for (symbol, timeframe) in groups:
        candles = await bridge.candles(symbol, timeframe, count=500)
        if len(candles) >= 2:
            changed += _settle_signals_from_candles(candles, symbol=symbol, timeframe=timeframe)
    return changed


def _open_signal_status(signal, current_price):
    entry = float(signal.get("entry", 0.0))
    sl = float(signal.get("sl", entry))
    is_buy = "UP" in str(signal.get("direction", "")).upper() or "BUY" in str(signal.get("direction", "")).upper()
    move = (current_price - entry) if is_buy else (entry - current_price)
    risk = max(abs(entry - sl), 1e-12)
    r_multiple = move / risk
    if abs(move) < risk * 0.01:
        label, icon = "AT ENTRY", "⚪"
    elif move > 0:
        label, icon = "PROFIT", "🟢"
    else:
        label, icon = "LOSS", "🔴"
    return label, icon, r_multiple


async def _send_open_signal_statuses(bridge):
    """Send one compact Telegram update for every currently open signal."""
    opened = _read_json(OPEN_SIGNALS_FILE, [])
    if not opened:
        return 0
    lines = ["📡 *OPEN SIGNAL UPDATE* • every 5 minutes"]
    changed = 0
    for signal in opened:
        symbol = str(signal.get("symbol", "")).upper()
        if not symbol:
            continue
        quote = await bridge.quote(symbol, minutes=5, force_refresh=True)
        if not quote:
            lines.append(f"⚪ {symbol} — price unavailable")
            continue
        current_price = float(quote["bid"])
        label, icon, r_multiple = _open_signal_status(signal, current_price)
        digits = _price_digits(symbol)
        signal["last_status"] = label
        signal["current_price"] = current_price
        signal["unrealized_r"] = round(r_multiple, 3)
        signal["status_updated_at"] = int(time.time())
        changed += 1
        lines.append(
            f"{icon} *{symbol} {('BUY' if 'UP' in str(signal.get('direction', '')).upper() or 'BUY' in str(signal.get('direction', '')).upper() else 'SELL')}* — "
            f"*{label}* `{r_multiple:+.2f}R`\n"
            f"Entry `{float(signal.get('entry', 0)):.{digits}f}` | Now `{current_price:.{digits}f}` | "
            f"SL `{float(signal.get('sl', 0)):.{digits}f}` | TP `{float(signal.get('tp', 0)):.{digits}f}`"
        )
    if changed:
        _write_json(OPEN_SIGNALS_FILE, opened)
        telegram_send("\n".join(lines))
    return changed


def _premium_market_quality(candles):
    if len(candles) < 60:
        return False, ["not enough candles"]
    ranges = [max(0.0, float(c["high"]) - float(c["low"])) for c in candles[-30:]]
    atr = _calc_atr(candles, 14) or 0.0
    median_range = sorted(ranges)[len(ranges) // 2]
    last_range = ranges[-1]
    reasons = []
    if atr <= 0 or median_range <= 0:
        return False, ["invalid volatility"]
    if last_range > atr * float(os.getenv("EXNESS_MAX_CANDLE_ATR", "3.0")):
        return False, ["abnormal candle spike"]
    if last_range < atr * float(os.getenv("EXNESS_MIN_CANDLE_ATR", "0.12")):
        return False, ["dead candle"]
    bodies = [abs(float(c["close"]) - float(c["open"])) for c in candles[-5:]]
    if sum(bodies) / max(1, len(bodies)) < atr * 0.08:
        return False, ["low participation"]
    reasons.append(f"ATR {atr:.6g}")
    reasons.append(f"range {last_range:.6g}")
    return True, reasons


def backtest_strategy(candles, symbol, timeframe=15, lookahead=12):
    """Walk through closed candles and return transparent signal/outcome metrics."""
    outcomes = []
    for idx in range(60, max(60, len(candles) - lookahead)):
        window = candles[:idx + 1]
        quality_ok, _ = _premium_market_quality(window)
        if not quality_ok:
            continue
        current = window[-1]
        result = ultra_hybrid_strategy(window, current, ux_volatility_analysis(window), asset=symbol, timeframe=timeframe)
        if not result:
            continue
        direction = result if isinstance(result, str) else result.get("direction")
        entry, sl, tp = levels_from_candles(symbol, window, direction)
        outcome = "unresolved"
        is_buy = "UP" in str(direction).upper() or "BUY" in str(direction).upper()
        for candle in candles[idx + 1:idx + 1 + lookahead]:
            hit_sl = float(candle["low"]) <= sl if is_buy else float(candle["high"]) >= sl
            hit_tp = float(candle["high"]) >= tp if is_buy else float(candle["low"]) <= tp
            if hit_sl and hit_tp: outcome = "loss"; break
            if hit_sl: outcome = "loss"; break
            if hit_tp: outcome = "win"; break
        if outcome != "unresolved":
            outcomes.append(outcome)
    wins = outcomes.count("win"); losses = outcomes.count("loss")
    return {"symbol": symbol, "timeframe": timeframe, "signals": len(outcomes), "wins": wins, "losses": losses, "win_rate": round(100.0 * wins / max(1, wins + losses), 2), "profit_factor_proxy": round(wins / max(1, losses), 3)}


def walk_forward_report(candles, symbol, timeframe=15):
    split = max(60, int(len(candles) * 0.70))
    train = backtest_strategy(candles[:split], symbol, timeframe)
    test = backtest_strategy(candles[max(0, split - 60):], symbol, timeframe)
    return {"train": train, "out_of_sample": test, "validated": test["signals"] >= 10 and test["win_rate"] >= float(os.getenv("EXNESS_MIN_OOS_WINRATE", "48"))}


_LAST_SIGNAL_AT = {}


def _rsi_from_closes(closes, period=14):
    return _calc_rsi(list(closes), period=period)


def _ema_series(prices, period):
    if not prices:
        return []
    out = [prices[0]]
    k = 2 / (period + 1)
    for p in prices[1:]:
        out.append(out[-1] + k * (p - out[-1]))
    return out


def _macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal:
        return None
    ema_fast = _ema_series(prices, fast)
    ema_slow = _ema_series(prices, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    sig = _ema_series(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line, sig)]
    n = 2
    return {
        "macd": macd_line[-1], "signal": sig[-1], "hist": hist[-1],
        "hist_prev": hist[-n - 1] if len(hist) > n else hist[-1],
        "hist_rising": len(hist) >= 2 and hist[-1] > hist[-2],
        "above_zero": macd_line[-1] >= 0,
    }


def _bb_width_percentile(closes, period=20, lookback=100):
    if len(closes) < period + lookback:
        return None
    widths = []
    for i in range(len(closes) - lookback, len(closes)):
        if i < period:
            continue
        win = closes[i - period:i]
        mid = sum(win) / period
        var = sum((x - mid) ** 2 for x in win) / period
        sd = var ** 0.5
        widths.append(4.0 * sd)
    if not widths:
        return None
    last = widths[-1]
    pct = sum(1 for w in widths if w <= last) / len(widths) * 100.0
    return {"bandwidth_pct": last, "percentile": pct, "squeezed": pct < 15.0}


def _family_of(base):
    if base in _CRYPTO_FAMILY:
        return "crypto"
    if base in _INDEX_FAMILY:
        return "index"
    if base in _METAL_ENERGY_FAMILY:
        return "metal"
    jpy = base.endswith("JPY")
    if base in {"USDZAR", "USDTRY", "USDMXN", "USDSGD", "USDPLN", "USDNOK", "USDSEK", "USDCNH", "USDINR"}:
        return "exoticfx"
    return "majorfx"


def _contract_size(base):
    metals = {"XAUUSD": 100, "XAGUSD": 5000, "XPTUSD": 50, "XPDUSD": 50}
    energy = {"USOIL": 1000, "UKOIL": 1000, "NGAS": 10000}
    indices = {"US30": 1, "US500": 5, "NAS100": 1, "GER40": 1, "FR40": 1, "EU50": 1, "UK100": 1, "JPN225": 1, "HK50": 1, "AU200": 1, "ES35": 1}
    if base in metals: return metals[base]
    if base in energy: return energy[base]
    if base in indices: return indices[base]
    if base in _CRYPTO_FAMILY: return 1
    return 100000


def _lot_suggestion(base, entry, sl):
    try:
        risk_pct = float(os.getenv("EXNESS_RISK_PERCENT", "1.0"))
        balance = float(os.getenv("EXNESS_ACCOUNT_BALANCE", "1000"))
    except ValueError:
        return None
    dist = abs(float(entry) - float(sl))
    if dist <= 0:
        return None
    risk_money = balance * risk_pct / 100.0
    lots = risk_money / (dist * _contract_size(base))
    lots = max(0.01, min(10.0, round(lots, 2)))
    return {"lots": lots, "risk_pct": risk_pct, "balance": balance}


def _diversification_check(base, direction):
    if os.getenv("EXNESS_DIVERSIFICATION", "1").strip().lower() in {"0", "false", "off", "no"}:
        return True, "diversification off"
    opened = _read_json(OPEN_SIGNALS_FILE, [])
    active = [s for s in opened if s.get("direction")]
    max_open = max(1, int(os.getenv("EXNESS_MAX_OPEN_SIGNALS", "3")))
    if len(active) >= max_open:
        return False, f"{len(active)} open signals >= {max_open}"
    family = _family_of(base)
    fam = [s for s in active if _family_of(str(s.get("symbol", ""))) == family]
    if len(fam) >= 2:
        return False, f"family {family} has {len(fam)} concurrent signals"
    major_up = [s for s in active if ("UP" in str(s.get("direction", "")).upper()) and _family_of(str(s.get("symbol", ""))) == "majorfx"]
    major_dn = [s for s in active if ("DOWN" in str(s.get("direction", "")).upper()) and _family_of(str(s.get("symbol", ""))) == "majorfx"]
    is_buy = "UP" in str(direction).upper() or "BUY" in str(direction).upper()
    majors = major_up if is_buy else major_dn
    if len(majors) >= 2:
        return False, "correlated major direction crowded"
    return True, "diversified"


async def _advanced_confirmation(bridge, symbol, timeframe, direction):
    """Require multi-timeframe alignment: trend, momentum, impulse and compression."""
    higher_tf = max(60, int(timeframe) * 4)
    htf = await bridge.candles(symbol, higher_tf, count=120)
    if len(htf) < 60:
        return False, ["HTF data unavailable"], {}
    closes = [float(x["close"]) for x in htf]
    ema21 = _calc_ema(closes, 21)
    ema55 = _calc_ema(closes, 55)
    rsi = _rsi_from_closes(closes, 14)
    adx = _calc_adx(htf, 14) or 0.0
    macd = _macd(closes)
    bb = _bb_width_percentile(closes)
    is_buy = "UP" in str(direction).upper() or "BUY" in str(direction).upper()
    reasons = [f"HTF {'BUY' if is_buy else 'SELL'} alignment", f"HTF ADX {adx:.1f}"]
    meta = {"rsi": rsi, "adx": adx, "macd_hist": None, "squeezed": bool(bb and bb["squeezed"])}

    # 4H macro alignment (soft gate: only when enough data exists)
    sfetch = await bridge.candles(symbol, 240, count=90)
    if len(sfetch) >= 40:
        scloses = [float(x["close"]) for x in sfetch]
        e21 = _calc_ema(scloses, 21); e55 = _calc_ema(scloses, 55)
        four_trend = e21 > e55
        s_adx = _calc_adx(sfetch, 14) or 0.0
        meta["h4_trend"] = "UP" if four_trend else "DOWN"
        meta["h4_adx"] = round(s_adx, 1)
        if four_trend != is_buy and s_adx >= 22:
            reasons.append("4H trend mismatch")
            return False, reasons + [f"4H ADX {s_adx:.1f} against signal"], meta
        reasons.append(f"4H {'BUY' if four_trend else 'SELL'} {s_adx:.1f}")

    if not (ema21 > ema55 if is_buy else ema21 < ema55):
        reasons.append("HTF trend mismatch")
        return False, reasons, meta
    if rsi >= 72 if is_buy else rsi <= 28:
        reasons.append(f"HTF RSI exhaustion {rsi:.1f}")
        return False, reasons, meta
    if adx < 16:
        reasons.append(f"HTF ADX too weak {adx:.1f}")
        return False, reasons, meta

    # MACD momentum confirmation (soft)
    if macd:
        meta["macd_hist"] = round(macd["hist"], 5)
        momentum_bull = macd["hist_rising"] and (macd["above_zero"] or macd["hist"] > 0)
        momentum_bear = (not macd["hist_rising"]) and ((not macd["above_zero"]) or macd["hist"] < 0)
        m_ok = momentum_bull if is_buy else momentum_bear
        if not m_ok and macd["hist_rising"] == (not is_buy):
            reasons.append(f"HTF MACD divergence {macd['hist']:.5f}")
        else:
            reasons.append(f"HTF MACD momentum {macd['hist']:.4f}")

    # Bollinger compression: weak trend, decompression expected (soft note)
    if bb and bb["squeezed"]:
        reasons.append("BB compression breakout setup")
    elif bb:
        reasons.append(f"BB width pct {bb['percentile']:.0f}%")

    # Volume impulse on signal timeframe (only if volume reported)
    return True, reasons + [f"HTF RSI {rsi:.1f}"], meta


def _session_allowed(symbol):
    if os.getenv("EXNESS_SESSION_FILTER", "1").strip().lower() in {"0", "false", "off", "no"}:
        return True, "session filter off"
    now = datetime.now(timezone.utc)
    hour = now.hour
    base = str(symbol).upper().replace("/", "").replace("-", "")
    if base in _CRYPTO_FAMILY:
        return True, "crypto 24/7"
    if now.weekday() >= 5:
        return False, "market weekend"
    if base in _METAL_ENERGY_FAMILY or base in _INDEX_FAMILY:
        allowed = 7 <= hour <= 20
    else:
        allowed = 6 <= hour <= 20
    return allowed, f"UTC {hour:02d}:00 session"


async def run_signal_cycle(bridge: FreeFeedBridge, symbol: str, timeframe: int = 60):
    now = time.time()
    cooldown = max(300, int(os.getenv("EXNESS_SIGNAL_COOLDOWN_SECONDS", "900")))
    if now - _LAST_SIGNAL_AT.get(symbol, 0.0) < cooldown:
        return None
    allowed, session_reason = _session_allowed(symbol)
    if not allowed:
        logger.info("Skipped %s: %s", symbol, session_reason)
        return None
    quality_ok, quality_reason, quality_row = _quality_status(symbol)
    if not quality_ok:
        logger.info("Skipped %s: adaptive quality gate: %s", symbol, quality_reason)
        return None
    candles = await bridge.candles(symbol, timeframe, count=220)
    if len(candles) < 60:
        return None
    settled = 0
    market_ok, market_reasons = _premium_market_quality(candles)
    if not market_ok:
        logger.info("Skipped %s: market quality: %s", symbol, "; ".join(market_reasons))
        return None
    current = candles[-1]
    volatility = ux_volatility_analysis(candles, lookback=20)
    strategy_result = ultra_hybrid_strategy(candles, current, volatility, asset=symbol, timeframe=timeframe)
    if not strategy_result:
        return None
    direction = strategy_result if isinstance(strategy_result, str) else (strategy_result.get("direction") or strategy_result.get("signal") or strategy_result.get("trend"))
    if not direction:
        return None
    up_side = "UP" in str(direction).upper() or "BUY" in str(direction).upper()
    tf_closes = [float(c["close"]) for c in candles]
    ema_mult = 2.0 / 22.0
    ema21_val = tf_closes[0]
    ema21_hist = [ema21_val]
    for price in tf_closes[1:]:
        ema21_val = (price - ema21_val) * ema_mult + ema21_val
        ema21_hist.append(ema21_val)
    if len(ema21_hist) >= 4:
        ema_slope = ema21_hist[-1] - ema21_hist[-4]
        if up_side and ema_slope < 0:
            logger.info("Skipped %s: EMA21 falling, against BUY direction", symbol)
            return None
        if (not up_side) and ema_slope > 0:
            logger.info("Skipped %s: EMA21 rising, against SELL direction", symbol)
            return None
    confirmed, confirmation_notes, confirmation_meta = await _advanced_confirmation(bridge, symbol, timeframe, direction)
    if not confirmed:
        logger.info("Skipped %s: %s", symbol, "; ".join(confirmation_notes))
        return None
    quote = await bridge.quote(symbol, minutes=timeframe)
    if quote is None:
        return None
    spread_points = (quote["ask"] - quote["bid"]) / max(quote["point"], 1e-12)
    max_spread = float(os.getenv("EXNESS_MAX_SPREAD_POINTS", "40"))
    if spread_points > max_spread:
        logger.info("Skipped %s: spread %.1f > %.1f points", symbol, spread_points, max_spread)
        return None
    meta = dict(last_signal_meta)
    confidence = float(meta.get("confidence", 0.0) or 0.0)
    fam = _family_of(symbol)
    min_confidence = float(os.getenv(
        "EXNESS_CRYPTO_MIN_CONFIDENCE" if fam == "crypto" else "EXNESS_MIN_SIGNAL_CONFIDENCE",
        "85" if fam == "crypto" else "90",
    ))
    if confidence < min_confidence:
        logger.info("Skipped %s: confidence %.1f < %.1f", symbol, confidence, min_confidence)
        return None
    entry, sl, tp = levels_from_candles(symbol, candles, direction, bid=quote["bid"], ask=quote["ask"])
    if entry is None or sl is None or tp is None:
        return None
    # professional anchor gate: entry must sit near support (pullback) or
    # at/above resistance (breakout). No-man's-land entries = chasing = reject.
    try:
        anchor_lows, anchor_highs = _find_swings(candles, window=3, lookback=70)
        atr14 = _calc_atr(candles, 14) or 0.0
        if atr14 > 0:
            if up_side:
                near_support = any(0 < (entry - l) <= atr14 * 1.5 for l in anchor_lows if l <= entry)
                breakout = any(0 <= (h - entry) <= atr14 * 0.8 for h in anchor_highs if h >= entry)
                below_anchor = any((entry - l) <= atr14 * 0.6 for l in anchor_lows if l <= entry)
                if not (near_support or breakout or below_anchor):
                    logger.info("Skipped %s: BUY entry in no-man's-land (chasing), no anchor", symbol)
                    return None
            else:
                near_resistance = any(0 < (h - entry) <= atr14 * 1.5 for h in anchor_highs if h >= entry)
                breakdown = any(0 <= (entry - l) <= atr14 * 0.8 for l in anchor_lows if l <= entry)
                above_anchor = any((h - entry) <= atr14 * 0.6 for h in anchor_highs if h >= entry)
                if not (near_resistance or breakdown or above_anchor):
                    logger.info("Skipped %s: SELL entry in no-man's-land (chasing), no anchor", symbol)
                    return None
    except Exception as exc:
        logger.info("Anchor gate error %s: %s", symbol, exc)
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    rr = reward / max(risk, 1e-12)
    min_rr = float(os.getenv(
        "EXNESS_CRYPTO_MIN_RR" if fam == "crypto" else "EXNESS_MIN_RR",
        "1.30" if fam == "crypto" else "1.40",
    ))
    if rr < min_rr:
        logger.info("Skipped %s: RR %.2f < %.2f", symbol, rr, min_rr)
        return None
    # professional diversification guard
    break_needed = False
    break_reason = None
    if not break_needed:
        div_ok, div_reason = _diversification_check(symbol, direction)
        if not div_ok:
            break_needed = True
            break_reason = f"diversification: {div_reason}"
    if break_needed:
        logger.info("Skipped %s: %s", symbol, break_reason)
        return None
    winrate = 100.0 * int(quality_row.get("wins", 0)) / max(1, int(quality_row.get("wins", 0)) + int(quality_row.get("losses", 0)))
    adaptive = float(os.getenv("EXNESS_ADAPTIVE_LEVEL", "3"))
    acc_score = round(min(100.0, 0.55 * confidence + 0.22 * min(100.0, rr * 55.0) + 0.23 * min(100.0, 40.0 + winrate * 0.6)), 1)
    mode_tag = f"ADAPTIVE·MTF v{adaptive:.0f}"
    lots = _lot_suggestion(symbol, entry, sl)
    meta["htf_rsi"] = round(float(confirmation_meta.get("rsi", 0.0)), 1)
    meta["htf_adx"] = round(float(confirmation_meta.get("adx", 0.0)), 1)
    meta["spread_points"] = round(spread_points, 1)
    meta["risk_reward"] = round(rr, 2)
    meta["quality_winrate"] = round(winrate, 1)
    meta["settled_count"] = int(quality_row.get("signals", 0))
    meta["settled_this_cycle"] = settled
    meta["market_quality"] = market_reasons
    meta["confirmation"] = confirmation_notes
    meta["accuracy"] = acc_score
    meta["engine"] = mode_tag
    meta["adaptive_level"] = adaptive
    if confirmation_meta.get("macd_hist") is not None:
        meta["macd_hist"] = confirmation_meta["macd_hist"]
    if confirmation_meta.get("h4_trend"):
        meta["h4_trend"] = confirmation_meta["h4_trend"]
        meta["h4_adx"] = confirmation_meta.get("h4_adx", 0.0)
    if confirmation_meta.get("squeezed"):
        meta["compression"] = "BB breakout setup"
    session_now = datetime.now(timezone.utc)
    session_tag = "COMEX" if _family_of(symbol) in ("metal",) else (_family_of(symbol).upper())
    direction_label = "BUY" if "UP" in str(direction).upper() or "BUY" in str(direction).upper() else "SELL"
    message = (
        f"📊 *{symbol} {direction_label}* • `{_timeframe_label(timeframe)}`\n"
        f"Entry: `{entry}`\n"
        f"SL: `{sl}` | TP: `{tp}`\n"
        f"Conf: `{confidence:.0f}%` | RR: `{rr:.2f}`\n"
        f"MTF 4H confirmed • `{session_now.strftime('%H:%M')} UTC`"
    )
    _telegram_typing()
    notification_sent = telegram_send(message)
    chart_path = _render_signal_chart(symbol, candles, direction_label, entry, sl, tp, timeframe)
    chart_sent = telegram_send_photo(
        chart_path,
        f"{symbol} {direction_label} • Entry {entry} | SL {sl} | TP {tp}",
    )
    if chart_path:
        try:
            os.unlink(chart_path)
        except OSError:
            pass
    # Result tracking must not depend on Telegram being configured. Signals
    # are always persisted locally and later settled from closed candles.
    _record_open_signal(
        symbol,
        direction,
        entry,
        sl,
        tp,
        timeframe,
        confidence,
        notification_sent,
        chart_sent,
    )
    _LAST_SIGNAL_AT[symbol] = now
    return {"symbol": symbol, "direction": direction, "entry": entry, "sl": sl, "tp": tp, "strategy": meta, "confidence": confidence, "accuracy": acc_score, "quality_score": acc_score}


def _is_weekend(now_utc=None):
    dt = now_utc or datetime.now(timezone.utc)
    return dt.weekday() >= 5


def _telegram_announce(text):
    if os.getenv("EXNESS_TELEGRAM_STATUS", "1") != "1":
        return False
    try:
        return telegram_send(text)
    except Exception:
        logger.warning("Telegram announce failed", exc_info=True)
        return False


_LAST_TYPING = [0.0]


def _telegram_typing():
    """Show 'typing...' animation in Telegram before messages (rate-limited)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    if time.time() - _LAST_TYPING[0] < 4.0:
        return
    _LAST_TYPING[0] = time.time()
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendChatAction",
            data={"chat_id": chat_id, "action": "typing"},
            timeout=10,
        )
    except Exception:
        pass


async def main():
    print_status_message("┌────────────────────────────────────────────┐", "info")
    print_status_message("│   UX50 PRO v2.0 • ADAPTIVE MTF ENGINE         │", "info")
    print_status_message("│   multi-timeframe precision signal advisory   │", "info")
    print_status_message("│   signal-only • no order placement            │", "info")
    print_status_message("└────────────────────────────────────────────┘", "info")
    bridge = FreeFeedBridge()
    connected, message = await bridge.connect()
    if not connected:
        logger.error(message)
        return
    logger.info("Signal-only mode enabled; no orders will be placed")
    symbols = [x.strip() for x in os.getenv("EXNESS_SYMBOLS", "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,NZDUSD,USDCHF,EURGBP,EURJPY,GBPJPY,EURCHF,GBPCHF,EURAUD,EURNZD,EURCAD,GBPAUD,GBPNZD,GBPCAD,AUDJPY,AUDNZD,AUDCAD,AUDCHF,NZDJPY,NZDCAD,NZDCHF,CADJPY,CADCHF,CHFJPY,USDZAR,USDTRY,USDMXN,USDSGD,USDPLN,USDNOK,USDSEK,USDCNH,USDINR,XAUUSD,XAGUSD,XPTUSD,XPDUSD,USOIL,UKOIL,NGAS,US30,US500,NAS100,GER40,FR40,EU50,UK100,JPN225,HK50,AU200,ES35,BTCUSD,ETHUSD,XRPUSD,SOLUSD,DOGEUSD,LTCUSD,BCHUSD").split(",") if x.strip()]
    timeframe = int(os.getenv("EXNESS_TIMEFRAME_MINUTES", "60"))
    interval = max(30, int(os.getenv("EXNESS_SCAN_SECONDS", "60")))
    status_every = int(os.getenv("EXNESS_STATUS_EVERY_SECONDS", "1800"))
    pass_summary_every = max(1, int(os.getenv("EXNESS_TELEGRAM_PASS_EVERY", "6")))
    verbose = os.getenv("EXNESS_TELEGRAM_VERBOSE", "0") == "1"
    _loop_cursor = 0
    _last_status = 0.0
    _last_weekend_msg = 0.0
    tg_configured = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip())
    _telegram_announce(
        "UX50 SIGNAL BOT STARTED\n"
        f"Markets: {len(symbols)}\n"
        f"Timeframe: {timeframe}m | scan every {interval}s\n"
        "Feed: Yahoo FreeFeed connected\n"
        "Mode: signal-only advisory\n"
        f"Telegram: {'ON' if tg_configured else 'NOT CONFIGURED (logs only)'}"
    )
    try:
        while True:
            if _is_weekend():
                if time.time() - _last_weekend_msg >= 3600:
                    _last_weekend_msg = time.time()
                    print_status_message("WEEKEND - market closed, scanning paused", "warning")
                    _telegram_announce(
                        "WEEKEND DETECTED\n"
                        "Market closed (Sat/Sun UTC).\n"
                        "Scanning paused. Will resume automatically after weekend."
                    )
                await asyncio.sleep(3600)
                continue
            try:
                await _settle_open_signals_with_bridge(bridge)
            except Exception:
                logger.error("Open-signal settlement pass failed", exc_info=True)
            signals_found = 0
            scanned_this_pass = 0
            live_every = int(os.getenv("EXNESS_TELEGRAM_LIVE_EVERY", "10"))
            for symbol in symbols:
                try:
                    result = await run_signal_cycle(bridge, symbol, timeframe)
                except Exception:
                    logger.error("Cycle failed for %s", symbol, exc_info=True)
                    result = None
                if result:
                    signals_found += 1
                elif verbose:
                    try:
                        telegram_send(f"{symbol}: scan done, no signal found")
                    except Exception:
                        pass
                scanned_this_pass += 1
                if live_every > 0 and (scanned_this_pass % live_every == 0 or scanned_this_pass == len(symbols)):
                    _telegram_typing()
                    _telegram_announce(
                        f"LIVE SCAN {scanned_this_pass}/{len(symbols)} | {symbol} | "
                        f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC | signals so far: {signals_found}"
                    )
                await asyncio.sleep(1)
            _loop_cursor += 1
            if _loop_cursor % pass_summary_every == 0:
                next_label = f"{interval // 60} min" if interval >= 60 else f"{interval} sec"
                open_count = len(_read_json(OPEN_SIGNALS_FILE, []))
                _telegram_announce(
                    f"SCAN PASS #{_loop_cursor}\n"
                    f"Markets scanned: {len(symbols)}/{len(symbols)}\n"
                    f"New signals: {signals_found}\n"
                    f"Open signals: {open_count}\n"
                    f"Next pass in: ~{next_label}"
                )
            if time.time() - _last_status >= status_every:
                _last_status = time.time()
                try:
                    await _send_open_signal_statuses(bridge)
                except Exception:
                    logger.error("Open-signal status pass failed", exc_info=True)
                stats = _read_json(PREMIUM_STATS_FILE, {})
                total_sigs = sum(int(r.get("signals", 0)) for r in stats.values())
                total_wins = sum(int(r.get("wins", 0)) for r in stats.values())
                total_loss = sum(int(r.get("losses", 0)) for r in stats.values())
                wr = (100.0 * total_wins / max(1, total_wins + total_loss)) if (total_wins + total_loss) else 0.0
                print_status_message(
                    f"STATUS | markets={len(symbols)} scanned={_loop_cursor} signals={total_sigs} "
                    f"wins={total_wins} losses={total_loss} winrate={wr:.1f}% active={(len(_LAST_SIGNAL_AT))}",
                    "info",
                )
            await asyncio.sleep(interval)
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
