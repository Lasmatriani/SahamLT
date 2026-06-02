import os
import json
import asyncio
import logging
from datetime import datetime, time
import pytz
import yfinance as yf
import pandas as pd
import numpy as np
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Config ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.environ["BOT_TOKEN"]
ALLOWED_IDS = set(map(int, os.environ.get("ALLOWED_USER_IDS", "").split(","))) if os.environ.get("ALLOWED_USER_IDS") else set()
WIB         = pytz.timezone("Asia/Jakarta")

# Default CL/TP %
DEFAULT_CL_PCT    = float(os.environ.get("DEFAULT_CL_PCT",  "-7"))
DEFAULT_TP_PCT    = float(os.environ.get("DEFAULT_TP_PCT",  "15"))
FINNHUB_API_KEY   = os.environ.get("FINNHUB_API_KEY", "")

# ── Persistent Storage (Railway Volume) ──────────────────────────────────
# Railway: tambahkan Volume di Settings → Volumes, mount path /data
# Fallback ke ./data/ untuk local dev
_VOLUME_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/data")
_LOCAL_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

def _get_data_dir() -> str:
    if os.path.isdir(_VOLUME_DIR):
        return _VOLUME_DIR
    os.makedirs(_LOCAL_DIR, exist_ok=True)
    return _LOCAL_DIR

def _data_path(fn: str) -> str:
    return os.path.join(_get_data_dir(), fn)

def load_watchlist() -> dict:
    """Load watchlist; auto-recover dari backup jika file utama corrupt."""
    for path in [_data_path("watchlist.json"), _data_path("watchlist.json.bak")]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"Watchlist loaded: {len(data)} items dari {path}")
                return data
            except Exception as e:
                logger.warning(f"Load gagal dari {path}: {e}")
    logger.info("Watchlist kosong, mulai fresh.")
    return {}

def save_watchlist(wl: dict):
    """Atomic write + backup otomatis."""
    import shutil
    primary = _data_path("watchlist.json")
    tmp     = _data_path("watchlist.json.tmp")
    backup  = _data_path("watchlist.json.bak")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(wl, f, indent=2, ensure_ascii=False)
        os.replace(tmp, primary)
        shutil.copy2(primary, backup)
        logger.info(f"Watchlist saved: {len(wl)} items")
    except Exception as e:
        logger.error(f"Save gagal: {e}")
        raise

def get_storage_info() -> dict:
    """Untuk command /status."""
    data_dir   = _get_data_dir()
    primary    = _data_path("watchlist.json")
    backup     = _data_path("watchlist.json.bak")
    is_volume  = os.path.isdir(_VOLUME_DIR)
    mtime_str  = "–"
    if os.path.exists(primary):
        import datetime as dt
        mtime_str = dt.datetime.fromtimestamp(
            os.path.getmtime(primary), WIB
        ).strftime("%d %b %Y %H:%M WIB")
    return {
        "mode":        "Railway Volume 💾" if is_volume else "Local Storage 📁",
        "path":        data_dir,
        "has_primary": os.path.exists(primary),
        "has_backup":  os.path.exists(backup),
        "last_saved":  mtime_str,
        "size":        os.path.getsize(primary) if os.path.exists(primary) else 0,
    }

# ── Yahoo Finance helpers ────────────────────────────────────────────────
def ticker_id(kode: str) -> str:
    """ANTM → ANTM.JK"""
    kode = kode.upper().strip()
    return kode if kode.endswith(".JK") else kode + ".JK"

def get_price_data(kode: str) -> dict | None:
    """
    Ambil data saham dari multiple sumber:
    1. Finnhub (primary — proper API, tidak diblokir cloud)
    2. Stooq (fallback)
    3. yfinance (last resort)
    """
    kode = kode.upper().strip()

    for source, func in [
        ("Finnhub",  lambda: _fetch_from_finnhub(kode)),
        ("Stooq",    lambda: _fetch_from_stooq(kode)),
        ("yfinance", lambda: _fetch_from_yfinance(kode)),
    ]:
        try:
            logger.info(f"Trying {source} for {kode}...")
            d = func()
            if d:
                logger.info(f"{source} SUCCESS for {kode}")
                return d
            logger.warning(f"{source} returned no data for {kode}")
        except Exception as e:
            logger.warning(f"{source} failed for {kode}: {e}")

    logger.error(f"All sources failed for {kode}")
    return None


def _fetch_from_finnhub(kode: str) -> dict | None:
    """
    Fetch dari Finnhub API — proper API key, reliable dari cloud.
    IDX Indonesia format: HATM.JK
    """
    import requests, time

    if not FINNHUB_API_KEY:
        logger.warning("FINNHUB_API_KEY tidak diset")
        return None

    headers = {"X-Finnhub-Token": FINNHUB_API_KEY}
    base    = "https://finnhub.io/api/v1"
    symbol  = f"{kode}.JK"   # format IDX di Finnhub

    # ── Harga terkini (quote)
    r = requests.get(f"{base}/quote", params={"symbol": symbol}, headers=headers, timeout=10)
    r.raise_for_status()
    q = r.json()
    logger.info(f"Finnhub quote {symbol}: {q}")

    current = float(q.get("c", 0) or 0)   # current price
    prev    = float(q.get("pc", 0) or 0)  # previous close

    if current == 0:
        # Coba format tanpa .JK
        r2 = requests.get(f"{base}/quote", params={"symbol": kode}, headers=headers, timeout=10)
        r2.raise_for_status()
        q2 = r2.json()
        current = float(q2.get("c", 0) or 0)
        prev    = float(q2.get("pc", 0) or 0)
        symbol  = kode
        if current == 0:
            return None

    chg_pct = ((current - prev) / prev * 100) if prev > 0 else 0.0

    # ── Data historis candle (180 hari)
    now       = int(time.time())
    from_ts   = now - (180 * 24 * 3600)

    r3 = requests.get(
        f"{base}/stock/candle",
        params={"symbol": symbol, "resolution": "D", "from": from_ts, "to": now},
        headers=headers, timeout=15
    )
    r3.raise_for_status()
    candles = r3.json()
    logger.info(f"Finnhub candles status: {candles.get('s')} count={len(candles.get('c',[]))}")

    if candles.get("s") != "ok" or not candles.get("c"):
        return None

    closes  = pd.Series(candles["c"], dtype=float)
    opens   = pd.Series(candles["o"], dtype=float)
    highs   = pd.Series(candles["h"], dtype=float)
    lows    = pd.Series(candles["l"], dtype=float)
    volumes = pd.Series(candles["v"], dtype=float)
    dates   = pd.to_datetime(candles["t"], unit="s")

    hist_df = pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows,
        "Close": closes, "Volume": volumes
    }, index=dates).dropna()

    if len(hist_df) < 20:
        logger.warning(f"Finnhub: not enough data ({len(hist_df)} rows) for {kode}")
        return None

    # ── Fundamental (P/E, P/BV)
    try:
        rf = requests.get(
            f"{base}/stock/metric",
            params={"symbol": symbol, "metric": "all"},
            headers=headers, timeout=10
        )
        rf.raise_for_status()
        metrics = rf.json().get("metric", {})
        per = metrics.get("peBasicExclExtraTTM") or metrics.get("peTTM")
        pbv = metrics.get("pbQuarterly") or metrics.get("pb")
        name = kode
        # Coba ambil nama perusahaan
        rp = requests.get(f"{base}/stock/profile2", params={"symbol": symbol}, headers=headers, timeout=10)
        rp.raise_for_status()
        profile = rp.json()
        name = profile.get("name", kode)
    except Exception as e:
        logger.warning(f"Finnhub fundamental error: {e}")
        per = pbv = None
        name = kode

    return _calculate_indicators(kode, name, current, prev, chg_pct, hist_df, pbv, per)


def _fetch_from_stooq(kode: str) -> dict | None:
    """Fetch dari Stooq — fallback, format ticker IDX: HATM.ID"""
    import requests, io

    ticker = kode.upper() + ".ID"
    url    = f"https://stooq.com/q/d/l/?s={ticker}&i=d"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()

    text = r.text.strip()
    if not text or "No data" in text or len(text) < 50:
        return None

    # Baca CSV tanpa parse_dates dulu
    hist_df = pd.read_csv(io.StringIO(text))
    logger.info(f"Stooq columns: {list(hist_df.columns)}")

    # Normalize nama kolom — Stooq kadang pakai 'Date', kadang lowercase
    hist_df.columns = [c.strip().title() for c in hist_df.columns]

    if "Date" not in hist_df.columns:
        logger.warning(f"Stooq: no Date column, got {list(hist_df.columns)}")
        return None

    hist_df["Date"] = pd.to_datetime(hist_df["Date"], errors="coerce")
    hist_df = hist_df.set_index("Date").sort_index()
    hist_df = hist_df[["Open","High","Low","Close","Volume"]].dropna()

    if len(hist_df) < 20:
        return None

    current = float(hist_df["Close"].iloc[-1])
    prev    = float(hist_df["Close"].iloc[-2])
    chg_pct = ((current - prev) / prev) * 100

    logger.info(f"Stooq: {kode} price={current} rows={len(hist_df)}")
    return _calculate_indicators(kode, kode, current, prev, chg_pct, hist_df, None, None)


def _fetch_from_idx(kode: str) -> dict | None:
    """Fetch dari IDX API tidak resmi — reliable dari server cloud."""
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://www.idx.co.id/",
    }

    try:
        # Fetch harga terkini dari IDX
        url_summary = (
            f"https://www.idx.co.id/primary/TradingSummary/GetStockSummary"
            f"?start=0&length=1&code={kode}&lang=id"
        )
        logger.info(f"IDX API: fetching summary for {kode}")
        r = requests.get(url_summary, headers=headers, timeout=15)
        logger.info(f"IDX API summary status: {r.status_code}")
        r.raise_for_status()
        data = r.json()
        logger.info(f"IDX API summary data keys: {list(data.keys()) if data else 'empty'}")

        if not data.get("data"):
            logger.warning(f"IDX API: no data for {kode}")
            return None

        stock = data["data"][0]
        current = float(stock.get("IndexLastPrice", 0) or stock.get("LastPrice", 0) or 0)
        prev    = float(stock.get("PreviousPrice", current) or current)
        logger.info(f"IDX API: {kode} price={current} prev={prev}")

        if current == 0:
            logger.warning(f"IDX API: price=0 for {kode}")
            return None

        chg_pct = ((current - prev) / prev * 100) if prev > 0 else 0.0

        # Fetch data historis dari IDX
        url_hist = (
            f"https://www.idx.co.id/primary/StockData/GetChartStockbyCode"
            f"?indexCode={kode}&tradingDate=&period=180&language=id"
        )
        logger.info(f"IDX API: fetching history for {kode}")
        r2 = requests.get(url_hist, headers=headers, timeout=15)
        logger.info(f"IDX API history status: {r2.status_code}")
        r2.raise_for_status()
        hist_data = r2.json()
        logger.info(f"IDX API history keys: {list(hist_data.keys()) if hist_data else 'empty'}")

        if not hist_data.get("ChartData"):
            logger.warning(f"IDX API: no ChartData for {kode}")
            return None

        chart = hist_data["ChartData"]
        logger.info(f"IDX API: got {len(chart)} candles for {kode}")

        closes  = pd.Series([float(c.get("close", 0) or 0) for c in chart], dtype=float)
        opens   = pd.Series([float(c.get("open",  0) or 0) for c in chart], dtype=float)
        highs   = pd.Series([float(c.get("high",  0) or 0) for c in chart], dtype=float)
        lows    = pd.Series([float(c.get("low",   0) or 0) for c in chart], dtype=float)
        volumes = pd.Series([float(c.get("volume",0) or 0) for c in chart], dtype=float)
        dates   = pd.to_datetime([c.get("date","") for c in chart], errors="coerce")

        hist_df = pd.DataFrame({
            "Open": opens, "High": highs, "Low": lows,
            "Close": closes, "Volume": volumes
        }, index=dates).dropna()

        if len(hist_df) < 20:
            logger.warning(f"IDX API: not enough data ({len(hist_df)} rows) for {kode}")
            return None

        name = stock.get("StockName", kode)
        return _calculate_indicators(kode, name, current, prev, chg_pct, hist_df, None, None)

    except Exception as e:
        logger.error(f"IDX API error for {kode}: {type(e).__name__}: {e}")
        return None


def _fetch_from_yfinance(kode: str) -> dict | None:
    """Fetch dari yfinance dengan session khusus."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    })
    retry = Retry(total=2, backoff_factor=2, status_forcelist=[429, 500, 502, 503])
    session.mount("https://", HTTPAdapter(max_retries=retry))

    tk   = yf.Ticker(ticker_id(kode), session=session)
    hist = tk.history(period="6mo", interval="1d")
    if hist.empty:
        hist = tk.history(period="3mo", interval="1d")
    if hist.empty:
        return None

    current = float(hist["Close"].iloc[-1])
    prev    = float(hist["Close"].iloc[-2])
    chg_pct = ((current - prev) / prev) * 100

    try:
        info = tk.info
        pbv  = info.get("priceToBook")
        per  = info.get("trailingPE")
        name = info.get("longName") or info.get("shortName") or kode
    except Exception:
        pbv = per = None
        name = kode

    return _calculate_indicators(kode, name, current, prev, chg_pct, hist, pbv, per)


def _calculate_indicators(kode, name, current, prev, chg_pct, hist, pbv, per) -> dict:
    """Hitung semua indikator teknikal dari DataFrame OHLCV."""
    close  = hist["Close"]
    volume = hist["Volume"]
    op     = hist["Open"]
    hi     = hist["High"]
    lo     = hist["Low"]

    # RSI
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss
    rsi   = float(100 - (100 / (1 + rs.iloc[-1])))

    # MACD
    ema12         = close.ewm(span=12).mean()
    ema26         = close.ewm(span=26).mean()
    macd_line     = ema12 - ema26
    signal_line   = macd_line.ewm(span=9).mean()
    macd_hist     = float(macd_line.iloc[-1] - signal_line.iloc[-1])
    macd_hist_prev= float(macd_line.iloc[-2] - signal_line.iloc[-2])
    macd_val      = float(macd_line.iloc[-1])
    signal_val    = float(signal_line.iloc[-1])
    macd_golden_cross = macd_hist > 0 and macd_hist_prev <= 0

    # MA
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma50 = float(close.rolling(min(50, len(close))).mean().iloc[-1])

    # Support / Resistance
    support    = float(close.rolling(20).min().iloc[-1])
    resistance = float(close.rolling(20).max().iloc[-1])

    # Volume
    avg_vol   = float(volume.rolling(10).mean().iloc[-1])
    vol_ratio = float(volume.iloc[-1]) / avg_vol if avg_vol > 0 else 1.0

    # Bollinger Bands
    bb_mid_s = close.rolling(20).mean()
    bb_std   = close.rolling(20).std()
    bb_upper = float((bb_mid_s + 2 * bb_std).iloc[-1])
    bb_lower = float((bb_mid_s - 2 * bb_std).iloc[-1])
    bb_mid_v = float(bb_mid_s.iloc[-1])
    bb_width = (bb_upper - bb_lower) / bb_mid_v if bb_mid_v > 0 else 0
    bb_pct   = (current - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5
    bb_squeeze = bb_width < 0.10

    # Candlestick
    cl2 = close
    def body(i):    return abs(float(cl2.iloc[i]) - float(op.iloc[i]))
    def candle(i):  return float(hi.iloc[i]) - float(lo.iloc[i])
    def is_bull(i): return float(cl2.iloc[i]) > float(op.iloc[i])
    def is_bear(i): return float(cl2.iloc[i]) < float(op.iloc[i])

    patterns = []
    if candle(-1) > 0 and body(-1) / candle(-1) < 0.10:
        patterns.append(("DOJI", "neutral", "Ketidakpastian — tunggu konfirmasi arah ⚖️"))
    if candle(-1) > 0:
        ls = float(op.iloc[-1] if is_bull(-1) else cl2.iloc[-1]) - float(lo.iloc[-1])
        us = float(hi.iloc[-1]) - float(cl2.iloc[-1] if is_bull(-1) else op.iloc[-1])
        b  = body(-1)
        if ls > 2*b and us < b and b > 0:
            patterns.append(("HAMMER", "bullish", "Hammer — sinyal reversal bullish 🔨"))
        if us > 2*b and ls < b and b > 0:
            patterns.append(("SHOOTING STAR", "bearish", "Shooting Star — potensi reversal turun ⭐"))
    if len(cl2) >= 2:
        if is_bear(-2) and is_bull(-1) and body(-1) > body(-2):
            if float(cl2.iloc[-1]) > float(op.iloc[-2]) and float(op.iloc[-1]) < float(cl2.iloc[-2]):
                patterns.append(("BULLISH ENGULFING", "bullish", "Bullish Engulfing — sinyal beli kuat 🟢"))
        if is_bull(-2) and is_bear(-1) and body(-1) > body(-2):
            if float(cl2.iloc[-1]) < float(op.iloc[-2]) and float(op.iloc[-1]) > float(cl2.iloc[-2]):
                patterns.append(("BEARISH ENGULFING", "bearish", "Bearish Engulfing — sinyal jual kuat 🔴"))
    if len(cl2) >= 3:
        if is_bear(-3) and body(-3) > candle(-3)*0.6 and body(-2) < candle(-2)*0.3 and is_bull(-1) and body(-1) > candle(-1)*0.6:
            patterns.append(("MORNING STAR", "bullish", "Morning Star — reversal bullish kuat ⭐🌅"))
        if is_bull(-3) and body(-3) > candle(-3)*0.6 and body(-2) < candle(-2)*0.3 and is_bear(-1) and body(-1) > candle(-1)*0.6:
            patterns.append(("EVENING STAR", "bearish", "Evening Star — reversal bearish 🌆"))
        if all(is_bull(-i) for i in [1,2,3]) and float(cl2.iloc[-1]) > float(cl2.iloc[-2]) > float(cl2.iloc[-3]):
            patterns.append(("THREE WHITE SOLDIERS", "bullish", "3 White Soldiers — tren naik kuat 💪"))
        if all(is_bear(-i) for i in [1,2,3]) and float(cl2.iloc[-1]) < float(cl2.iloc[-2]) < float(cl2.iloc[-3]):
            patterns.append(("THREE BLACK CROWS", "bearish", "3 Black Crows — tren turun kuat 🐦"))

    candle_bull = sum(1 for p in patterns if p[1] == "bullish")
    candle_bear = sum(1 for p in patterns if p[1] == "bearish")
    candle_bias = "bullish" if candle_bull > candle_bear else ("bearish" if candle_bear > candle_bull else "neutral")

    return {
        "kode":              kode,
        "name":              name,
        "current":           current,
        "prev":              prev,
        "chg_pct":           chg_pct,
        "rsi":               rsi,
        "macd":              macd_val,
        "signal":            signal_val,
        "macd_hist":         macd_hist,
        "macd_hist_prev":    macd_hist_prev,
        "macd_golden_cross": macd_golden_cross,
        "ma20":              ma20,
        "ma50":              ma50,
        "support":           support,
        "resistance":        resistance,
        "vol_ratio":         vol_ratio,
        "bb_upper":          bb_upper,
        "bb_lower":          bb_lower,
        "bb_mid":            bb_mid_v,
        "bb_pct":            bb_pct,
        "bb_width":          bb_width,
        "bb_squeeze":        bb_squeeze,
        "patterns":          patterns,
        "candle_bias":       candle_bias,
        "pbv":               pbv,
        "per":               per,
        "hist":              hist.tail(60),
    }

def analyze(d: dict, entry: float, cl_pct: float, tp_pct: float) -> dict:
    """
    Hitung sinyal BUY/SELL dengan logika ketat:

    STRONG BUY  → semua 4 kondisi inti terpenuhi + fundamental OK
    BUY         → minimal 4 dari 6 kondisi terpenuhi (termasuk min 2 teknikal inti)
    NEUTRAL     → sinyal campuran, belum cukup konfirmasi
    SELL        → teknikal lemah
    STRONG SELL → teknikal sangat lemah atau fundamental buruk

    OVERRIDE ke SELL jika perusahaan rugi (per negatif & pbv tinggi)
    OVERRIDE ke bawah jika harga sudah naik >50% dalam sebulan (terlambat masuk)
    """
    price     = d["current"]
    rsi       = d["rsi"]
    mh        = d["macd_hist"]
    gc        = d["macd_golden_cross"]   # MACD baru balik positif
    support   = d["support"]
    resist    = d["resistance"]
    ma20      = d["ma20"]
    vol_ratio = d["vol_ratio"]
    pbv       = d["pbv"]
    per       = d["per"]
    chg_pct   = d["chg_pct"]
    bb_upper  = d["bb_upper"]
    bb_lower  = d["bb_lower"]
    bb_mid    = d["bb_mid"]
    bb_pct    = d["bb_pct"]
    bb_squeeze= d["bb_squeeze"]
    patterns  = d["patterns"]
    candle_bias = d["candle_bias"]

    # ── CL & TP level (teknikal prioritas, % sebagai backup)
    cl_tech      = support * 0.99
    tp_tech      = resist
    cl_pct_level = entry * (1 + cl_pct / 100)
    tp_pct_level = entry * (1 + tp_pct / 100)
    cl_final     = max(cl_tech, cl_pct_level)
    tp_final     = min(tp_tech, tp_pct_level)

    # ── Evaluasi 6 kondisi sinyal ────────────────────────────────────────
    signals      = []   # deskripsi sinyal untuk ditampilkan
    conditions   = []   # True/False per kondisi
    tech_met     = 0    # counter kondisi teknikal inti

    # 1. RSI — wajib < 50 untuk BUY, bonus jika oversold
    if rsi < 30:
        signals.append(f"RSI oversold {rsi:.1f} — potensi reversal kuat 🟢")
        conditions.append(True)
        tech_met += 1
    elif rsi < 50:
        signals.append(f"RSI {rsi:.1f} — momentum belum overbought 🟢")
        conditions.append(True)
        tech_met += 1
    elif rsi > 70:
        signals.append(f"RSI overbought {rsi:.1f} — hati-hati koreksi 🔴")
        conditions.append(False)
    else:
        signals.append(f"RSI {rsi:.1f} — zona netral ⚖️")
        conditions.append(False)

    # 2. MACD — positif atau golden cross
    if gc:
        signals.append("MACD golden cross — baru balik bullish 🚀")
        conditions.append(True)
        tech_met += 1
    elif mh > 0:
        signals.append(f"MACD histogram positif ({mh:+.2f}) 🟢")
        conditions.append(True)
        tech_met += 1
    else:
        signals.append(f"MACD histogram negatif ({mh:+.2f}) 🔴")
        conditions.append(False)

    # 3. MA20 trend
    if price > ma20:
        signals.append(f"Harga di atas MA20 ({fmt(ma20)}) 🟢")
        conditions.append(True)
        tech_met += 1
    else:
        gap_pct = ((ma20 - price) / ma20) * 100
        signals.append(f"Harga di bawah MA20 — gap {gap_pct:.1f}% 🔴")
        conditions.append(False)

    # 4. Harga di atas support (tidak sedang breakdown)
    if price > support:
        signals.append(f"Di atas support Rp {fmt(support)} 🟢")
        conditions.append(True)
    else:
        signals.append(f"Di bawah support Rp {fmt(support)} — waspada 🔴")
        conditions.append(False)

    # 5. Volume spike konfirmasi
    if vol_ratio >= 1.5:
        signals.append(f"Volume spike {vol_ratio:.1f}x — ada minat beli 🔥")
        conditions.append(True)
    elif vol_ratio < 0.5:
        signals.append(f"Volume sangat sepi {vol_ratio:.1f}x — sinyal lemah 😴")
        conditions.append(False)
    else:
        signals.append(f"Volume normal {vol_ratio:.1f}x ⚖️")
        conditions.append(False)

    # 6. Bollinger Bands
    if bb_squeeze:
        signals.append(f"BB Squeeze — volatilitas rendah, potensi breakout ⚡")
        conditions.append(True)   # squeeze = peluang, dihitung kondisi
    elif bb_pct < 0.20:
        signals.append(f"Harga dekat Lower BB ({fmt(bb_lower)}) — potensi rebound 🟢")
        conditions.append(True)
    elif bb_pct > 0.90:
        signals.append(f"Harga dekat Upper BB ({fmt(bb_upper)}) — hati-hati overbought 🔴")
        conditions.append(False)
    else:
        pct_show = int(bb_pct * 100)
        signals.append(f"BB normal — posisi {pct_show}% dalam band ⚖️")
        conditions.append(False)

    # 7. Candlestick pattern
    if patterns:
        bull_patterns = [p for p in patterns if p[1] == "bullish"]
        bear_patterns = [p for p in patterns if p[1] == "bearish"]
        for p in patterns:
            signals.append(f"Candle: {p[2]}")
        if candle_bias == "bullish":
            conditions.append(True)
        elif candle_bias == "bearish":
            conditions.append(False)
        else:
            conditions.append(False)
    else:
        signals.append("Tidak ada pola candlestick signifikan ⚖️")
        conditions.append(False)

    # 6. Fundamental — PBV < 2x dan PER tidak ekstrem
    fund_ok = False
    if pbv is not None and per is not None:
        if pbv < 2.0 and 0 < per < 25:
            signals.append(f"Fundamental menarik: PBV {pbv:.2f}x, PER {per:.1f}x 🟢")
            conditions.append(True)
            fund_ok = True
        elif pbv > 4.0 or (per is not None and per > 40):
            signals.append(f"Valuasi mahal: PBV {pbv:.2f}x, PER {per:.1f}x 🔴")
            conditions.append(False)
        else:
            signals.append(f"Fundamental netral: PBV {pbv:.2f}x, PER {per:.1f}x ⚖️")
            conditions.append(False)
    elif pbv is not None:
        if pbv < 1.0:
            signals.append(f"PBV sangat murah {pbv:.2f}x 🟢")
            conditions.append(True)
            fund_ok = True
        else:
            signals.append(f"PBV {pbv:.2f}x ⚖️")
            conditions.append(False)
    else:
        signals.append("Data fundamental tidak tersedia ⚖️")
        conditions.append(False)

    # ── Hitung berapa kondisi terpenuhi ──────────────────────────────────
    met = sum(conditions)

    # ── Override: perusahaan rugi (PER negatif) ──────────────────────────
    rugi = (per is not None and per < 0)
    if rugi:
        signals.append("⛔ Perusahaan RUGI — override ke Sell")

    # ── Override: sudah naik terlalu kencang (>50% sebulan) ─────────────
    terlambat = price > resist * 1.10
    if terlambat:
        signals.append("⚠️ Harga sudah jauh di atas resistance — terlambat masuk")

    # ── Tentukan verdict (dari 8 kondisi total) ───────────────────────────
    # STRONG BUY: 6+ kondisi + min 2 teknikal inti + fundamental OK
    # BUY:        5+ kondisi + min 2 teknikal inti
    # NEUTRAL:    3–4 kondisi
    # SELL:       < 3 kondisi
    # STRONG SELL:< 2 kondisi atau override rugi

    if rugi or terlambat:
        if met <= 2 or rugi:
            verdict = "STRONG SELL ⚠️"
        else:
            verdict = "SELL 📉"
    elif met >= 6 and tech_met >= 2 and fund_ok:
        verdict = "STRONG BUY 🚀"
    elif met >= 5 and tech_met >= 2:
        verdict = "BUY 📈"
    elif met >= 3:
        verdict = "NEUTRAL ⚖️"
    elif met >= 2:
        verdict = "SELL 📉"
    else:
        verdict = "STRONG SELL ⚠️"

    # ── Alert CL / TP ─────────────────────────────────────────────────────
    alert = None
    if price <= cl_final:
        alert = "CUT_LOSS"
    elif price >= tp_final:
        alert = "TAKE_PROFIT"

    # Profit/loss dari entry
    pnl_pct = ((price - entry) / entry * 100) if entry > 0 else 0

    return {
        "score":        met,        # jumlah kondisi terpenuhi (0–6)
        "tech_met":     tech_met,
        "verdict":      verdict,
        "signals":      signals,
        "conditions":   conditions,
        "cl":           cl_final,
        "tp":           tp_final,
        "cl_tech":      cl_tech,
        "tp_tech":      tp_tech,
        "pnl_pct":      pnl_pct,
        "alert":        alert,
        "rugi":         rugi,
        "terlambat":    terlambat,
        "fund_ok":      fund_ok,
    }

# ── Format helpers ────────────────────────────────────────────────────────
def fmt(n, dec=0):
    if n is None: return "–"
    return f"{n:,.{dec}f}"

def pct_emoji(v):
    if v > 2:   return "🟢"
    if v > 0:   return "🔼"
    if v < -2:  return "🔴"
    return "🔽"

def build_card(d: dict, ana: dict, entry: float) -> str:
    chg       = d["chg_pct"]
    pnl       = ana["pnl_pct"]
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    score     = ana["score"]
    tech_met  = ana["tech_met"]

    alert_line = ""
    if ana["alert"] == "CUT_LOSS":
        alert_line = "\n\n🚨 *ALERT: HARGA MENYENTUH CUT LOSS!*\nSegera evaluasi posisi kamu."
    elif ana["alert"] == "TAKE_PROFIT":
        alert_line = "\n\n🎯 *ALERT: TARGET TAKE PROFIT TERCAPAI!*\nPertimbangkan untuk realisasi profit."

    # Pisahkan sinyal berdasarkan kategori
    rsi_sig   = next((s for s in ana["signals"] if "RSI" in s), "")
    macd_sig  = next((s for s in ana["signals"] if "MACD" in s), "")
    ma_sig    = next((s for s in ana["signals"] if "MA20" in s or "MA" in s and "MACD" not in s), "")
    sup_sig   = next((s for s in ana["signals"] if "support" in s.lower()), "")
    vol_sig   = next((s for s in ana["signals"] if "Volume" in s or "volume" in s), "")
    bb_sig    = next((s for s in ana["signals"] if "BB" in s), "")
    candle_sigs = [s for s in ana["signals"] if "Candle:" in s]
    fund_sig  = next((s for s in ana["signals"] if "Fundamental" in s or "PBV" in s or "Valuasi" in s or "murah" in s.lower()), "")
    override_sigs = [s for s in ana["signals"] if "RUGI" in s or "terlambat" in s.lower() or "⛔" in s or "⚠️" in s]

    pbv_txt = f"{d['pbv']:.2f}x" if d['pbv'] else "–"
    per_txt = f"{d['per']:.1f}x" if d['per'] else "–"

    # BB position bar visual
    bb_pos = int(d["bb_pct"] * 10)
    bb_pos = max(0, min(10, bb_pos))
    bb_bar = "░" * bb_pos + "▓" + "░" * (10 - bb_pos)
    bb_squeeze_tag = " ⚡SQUEEZE" if d["bb_squeeze"] else ""

    # Candlestick section
    if candle_sigs:
        candle_txt = "\n".join(f"  {s.replace('Candle: ','')}" for s in candle_sigs)
    else:
        candle_txt = "  Tidak ada pola signifikan"

    # Progress bar kondisi (dari 8)
    filled = "█" * score
    empty  = "░" * (8 - score)
    bar    = f"{filled}{empty} {score}/8"

    # Alasan verdict
    v = ana["verdict"]
    if "STRONG BUY" in v:
        reason = f"6+ kondisi ✅ · {tech_met} teknikal inti · fundamental ✅"
    elif "BUY" in v:
        reason = f"5+ kondisi ✅ · {tech_met} teknikal inti terpenuhi"
    elif "NEUTRAL" in v:
        reason = "Sinyal campuran — tunggu konfirmasi lebih lanjut"
    elif "STRONG SELL" in v:
        reason = "Terlalu sedikit kondisi terpenuhi" + (" · Perusahaan RUGI" if ana.get("rugi") else "")
    else:
        reason = "Kurang dari 3 kondisi terpenuhi"
    if ana.get("terlambat"):
        reason += " · Harga terlalu jauh dari resistance"

    override_txt = ("\n" + "\n".join(f"  {s}" for s in override_sigs)) if override_sigs else ""

    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *{d['kode']}* — {d['name'][:28]}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Harga: *Rp {fmt(d['current'])}* {pct_emoji(chg)} {chg:+.2f}%\n"
        f"📥 Entry: Rp {fmt(entry)} | {pnl_emoji} P/L: *{pnl:+.1f}%*\n\n"
        f"📊 *Teknikal:*\n"
        f"  {rsi_sig}\n"
        f"  {macd_sig}\n"
        f"  {ma_sig}\n"
        f"  {sup_sig}\n"
        f"  {vol_sig}\n\n"
        f"📉 *Bollinger Bands:*\n"
        f"  Upper: {fmt(d['bb_upper'])} | Mid: {fmt(d['bb_mid'])} | Lower: {fmt(d['bb_lower'])}\n"
        f"  Posisi: `[{bb_bar}]`{bb_squeeze_tag}\n"
        f"  {bb_sig}\n\n"
        f"🕯 *Candlestick Pattern:*\n"
        f"{candle_txt}\n\n"
        f"💹 *Fundamental:*\n"
        f"  PBV: {pbv_txt} | PER: {per_txt}\n"
        f"  {fund_sig}\n\n"
        f"🎯 *Level:*\n"
        f"  TP: Rp {fmt(ana['tp'])} _(teknikal: {fmt(ana['tp_tech'])})_\n"
        f"  CL: Rp {fmt(ana['cl'])} _(teknikal: {fmt(ana['cl_tech'])})_\n"
        f"{override_txt}\n"
        f"📶 `{bar}` kondisi terpenuhi\n"
        f"🏆 *Verdict: {ana['verdict']}*\n"
        f"_{reason}_"
        f"{alert_line}"
    )

# ── Chart Generator ───────────────────────────────────────────────────────
def generate_chart(d: dict, ana: dict, entry: float) -> bytes:
    """
    Generate chart 3-panel:
    1. Candlestick + BB + MA20 + MA50 + level CL/TP
    2. Volume bar
    3. RSI + MACD histogram
    Return PNG bytes untuk dikirim ke Telegram.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.gridspec import GridSpec
    import matplotlib.dates as mdates
    from io import BytesIO

    hist = d["hist"].copy()
    hist.index = pd.to_datetime(hist.index)
    if hasattr(hist.index, 'tz') and hist.index.tz is not None:
        hist.index = hist.index.tz_localize(None)

    close  = hist["Close"]
    open_  = hist["Open"]
    high   = hist["High"]
    low    = hist["Low"]
    volume = hist["Volume"]
    dates  = hist.index

    # ── Hitung indikator untuk chart ───────────────────────────────────
    ma20_s   = close.rolling(20).mean()
    ma50_s   = close.rolling(50).mean()
    bb_mid_s = close.rolling(20).mean()
    bb_std_s = close.rolling(20).std()
    bb_up_s  = bb_mid_s + 2 * bb_std_s
    bb_lo_s  = bb_mid_s - 2 * bb_std_s

    ema12    = close.ewm(span=12).mean()
    ema26    = close.ewm(span=26).mean()
    ml       = ema12 - ema26
    sl       = ml.ewm(span=9).mean()
    mh_s     = ml - sl

    delta    = close.diff()
    gain     = delta.clip(lower=0).rolling(14).mean()
    loss     = (-delta.clip(upper=0)).rolling(14).mean()
    rs       = gain / loss
    rsi_s    = 100 - (100 / (1 + rs))

    # ── Style ────────────────────────────────────────────────────────────
    BG      = "#0f1520"
    SURFACE = "#151e2e"
    GREEN   = "#00e5a0"
    RED     = "#ff4560"
    BLUE    = "#0099ff"
    YELLOW  = "#ffa500"
    MUTED   = "#5a7090"
    TEXT    = "#e2eaf5"

    fig = plt.figure(figsize=(12, 9), facecolor=BG)
    gs  = GridSpec(4, 1, figure=fig,
                   height_ratios=[4, 1.2, 1, 1],
                   hspace=0.06)

    ax1 = fig.add_subplot(gs[0])  # Candlestick
    ax2 = fig.add_subplot(gs[1], sharex=ax1)  # Volume
    ax3 = fig.add_subplot(gs[2], sharex=ax1)  # RSI
    ax4 = fig.add_subplot(gs[3], sharex=ax1)  # MACD

    for ax in [ax1, ax2, ax3, ax4]:
        ax.set_facecolor(SURFACE)
        ax.tick_params(colors=MUTED, labelsize=7)
        ax.yaxis.label.set_color(MUTED)
        for spine in ax.spines.values():
            spine.set_edgecolor("#1e2d42")

    # ── Panel 1: Candlestick ─────────────────────────────────────────────
    n = len(dates)
    xs = range(n)
    w  = 0.4

    for i, (o, h, l, c) in enumerate(zip(open_, high, low, close)):
        color = GREEN if c >= o else RED
        # Body
        ax1.bar(i, abs(c - o), bottom=min(c, o), color=color, width=w*1.8, alpha=0.9, zorder=3)
        # Wick
        ax1.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=2)

    # BB
    ax1.fill_between(xs, bb_lo_s, bb_up_s, alpha=0.08, color=BLUE, zorder=1)
    ax1.plot(xs, bb_up_s, color=BLUE, linewidth=0.7, alpha=0.5, linestyle="--", label="BB Upper")
    ax1.plot(xs, bb_lo_s, color=BLUE, linewidth=0.7, alpha=0.5, linestyle="--", label="BB Lower")
    ax1.plot(xs, bb_mid_s, color=BLUE, linewidth=0.5, alpha=0.3)

    # MA
    ax1.plot(xs, ma20_s, color=YELLOW, linewidth=1.0, label="MA20", zorder=4)
    ax1.plot(xs, ma50_s, color="#ff6b9d", linewidth=1.0, label="MA50", zorder=4)

    # CL / TP / Entry lines
    ax1.axhline(ana["cl"],    color=RED,   linewidth=1.2, linestyle="--", alpha=0.8, zorder=5)
    ax1.axhline(ana["tp"],    color=GREEN, linewidth=1.2, linestyle="--", alpha=0.8, zorder=5)
    ax1.axhline(entry,        color=YELLOW, linewidth=0.9, linestyle=":", alpha=0.7, zorder=5)
    ax1.axhline(d["current"], color=TEXT,  linewidth=0.7, linestyle=":", alpha=0.4)

    # Labels CL/TP
    ax1.text(n - 0.5, ana["cl"],  f" CL {ana['cl']:,.0f}", color=RED,   fontsize=7, va="center")
    ax1.text(n - 0.5, ana["tp"],  f" TP {ana['tp']:,.0f}", color=GREEN, fontsize=7, va="center")
    ax1.text(n - 0.5, entry,      f" Entry {entry:,.0f}", color=YELLOW, fontsize=7, va="center")

    # Pattern markers
    for pat in d["patterns"]:
        ptype = pat[1]
        pname = pat[0]
        color = GREEN if ptype == "bullish" else (RED if ptype == "bearish" else YELLOW)
        marker = "^" if ptype == "bullish" else ("v" if ptype == "bearish" else "D")
        ypos   = float(low.iloc[-1]) * 0.995 if ptype != "bearish" else float(high.iloc[-1]) * 1.005
        ax1.scatter(n - 1, ypos, color=color, marker=marker, s=80, zorder=6)
        ax1.annotate(pname, (n - 1, ypos), textcoords="offset points",
                     xytext=(0, -12 if ptype == "bearish" else 8),
                     fontsize=6, color=color, ha="center")

    verdict_color = GREEN if "BUY" in d.get("verdict", "") else (RED if "SELL" in d.get("verdict", "") else YELLOW)
    ax1.set_title(
        f"{d['kode']} — {d['name'][:35]}   |   "
        f"Rp {d['current']:,.0f}  {d['chg_pct']:+.2f}%   |   "
        f"RSI {d['rsi']:.1f}   |   {ana['verdict']}",
        color=TEXT, fontsize=9, pad=8, loc="left"
    )
    ax1.legend(fontsize=6, facecolor=BG, edgecolor=MUTED, labelcolor=MUTED, loc="upper left")
    ax1.set_ylabel("Harga (IDR)", color=MUTED, fontsize=7)

    # ── Panel 2: Volume ──────────────────────────────────────────────────
    avg_vol = volume.rolling(10).mean()
    vol_colors = [GREEN if c >= o else RED for c, o in zip(close, open_)]
    ax2.bar(xs, volume / 1e6, color=vol_colors, alpha=0.7, width=0.8)
    ax2.plot(xs, avg_vol / 1e6, color=YELLOW, linewidth=0.8, label="Vol MA10")
    ax2.set_ylabel("Vol (M)", color=MUTED, fontsize=7)
    ax2.legend(fontsize=6, facecolor=BG, edgecolor=MUTED, labelcolor=MUTED, loc="upper left")

    # ── Panel 3: RSI ─────────────────────────────────────────────────────
    ax3.plot(xs, rsi_s, color=BLUE, linewidth=1.0)
    ax3.axhline(70, color=RED,   linewidth=0.6, linestyle="--", alpha=0.6)
    ax3.axhline(30, color=GREEN, linewidth=0.6, linestyle="--", alpha=0.6)
    ax3.axhline(50, color=MUTED, linewidth=0.4, linestyle=":", alpha=0.4)
    ax3.fill_between(xs, rsi_s, 70, where=(rsi_s >= 70), alpha=0.15, color=RED)
    ax3.fill_between(xs, rsi_s, 30, where=(rsi_s <= 30), alpha=0.15, color=GREEN)
    ax3.set_ylim(0, 100)
    ax3.set_ylabel("RSI", color=MUTED, fontsize=7)
    ax3.text(n - 1, float(rsi_s.iloc[-1]), f" {float(rsi_s.iloc[-1]):.1f}",
             color=BLUE, fontsize=7, va="center")

    # ── Panel 4: MACD Histogram ──────────────────────────────────────────
    mh_colors = [GREEN if v >= 0 else RED for v in mh_s]
    ax4.bar(xs, mh_s, color=mh_colors, alpha=0.8, width=0.8)
    ax4.plot(xs, ml, color=BLUE,   linewidth=0.8, label="MACD")
    ax4.plot(xs, sl, color=YELLOW, linewidth=0.8, label="Signal")
    ax4.axhline(0, color=MUTED, linewidth=0.5, alpha=0.5)
    ax4.set_ylabel("MACD", color=MUTED, fontsize=7)
    ax4.legend(fontsize=6, facecolor=BG, edgecolor=MUTED, labelcolor=MUTED, loc="upper left")

    # ── X axis — tanggal ─────────────────────────────────────────────────
    tick_step = max(1, n // 8)
    ax4.set_xticks(range(0, n, tick_step))
    ax4.set_xticklabels(
        [dates[i].strftime("%d/%m") for i in range(0, n, tick_step)],
        color=MUTED, fontsize=7
    )
    plt.setp(ax1.get_xticklabels(), visible=False)
    plt.setp(ax2.get_xticklabels(), visible=False)
    plt.setp(ax3.get_xticklabels(), visible=False)

    # Footer
    now_str = datetime.now(WIB).strftime("%d %b %Y %H:%M WIB")
    fig.text(0.99, 0.01, f"IDX Saham Bot · {now_str} · Data: Yahoo Finance",
             ha="right", color=MUTED, fontsize=6)

    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()

# ── Auth helper ───────────────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    return update.effective_user.id in ALLOWED_IDS

# ── Command Handlers ──────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "👋 *Selamat datang di IDX Saham Bot!*\n\n"
        "Perintah tersedia:\n"
        "*/add KODE ENTRY* — tambah saham ke watchlist\n"
        "  _contoh: /add ANTM 2900_\n"
        "*/del KODE* — hapus saham\n"
        "*/list* — lihat semua watchlist\n"
        "*/cek KODE* — cek satu saham sekarang\n"
        "*/setcl KODE %* — ubah % cut loss (default -7%)\n"
        "  _contoh: /setcl ANTM -8_\n"
        "*/settp KODE %* — ubah % take profit (default +15%)\n"
        "  _contoh: /settp ANTM 20_\n"
        "*/scan* — scan semua watchlist sekarang\n"
        "*/status* — cek status bot & storage\n"
        "*/help* — bantuan",
        parse_mode="Markdown"
    )

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("❌ Format: /add KODE HARGA_ENTRY\nContoh: /add ANTM 2900")
        return

    kode  = args[0].upper().strip()
    try:
        entry = float(args[1].replace(",", ""))
    except ValueError:
        await update.message.reply_text("❌ Harga entry tidak valid.")
        return

    wl = load_watchlist()
    wl[kode] = {
        "entry":  entry,
        "cl_pct": DEFAULT_CL_PCT,
        "tp_pct": DEFAULT_TP_PCT,
        "added":  datetime.now(WIB).strftime("%Y-%m-%d %H:%M"),
    }
    save_watchlist(wl)

    await update.message.reply_text(
        f"✅ *{kode}* ditambahkan!\n"
        f"Entry: Rp {fmt(entry)}\n"
        f"CL default: {DEFAULT_CL_PCT}% | TP default: +{DEFAULT_TP_PCT}%\n\n"
        f"Gunakan /setcl dan /settp untuk ubah level.",
        parse_mode="Markdown"
    )

async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text("❌ Format: /del KODE")
        return

    kode = ctx.args[0].upper().strip()
    wl   = load_watchlist()
    if kode not in wl:
        await update.message.reply_text(f"❌ *{kode}* tidak ada di watchlist.", parse_mode="Markdown")
        return

    del wl[kode]
    save_watchlist(wl)
    await update.message.reply_text(f"🗑 *{kode}* dihapus dari watchlist.", parse_mode="Markdown")

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    wl = load_watchlist()
    if not wl:
        await update.message.reply_text("📋 Watchlist kosong. Tambah dengan /add KODE ENTRY")
        return

    lines = ["📋 *Watchlist kamu:*\n"]
    for kode, meta in wl.items():
        lines.append(
            f"• *{kode}* — Entry: Rp {fmt(meta['entry'])} | "
            f"CL: {meta['cl_pct']}% | TP: +{meta['tp_pct']}%"
        )
    lines.append(f"\nTotal: {len(wl)} saham")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_cek(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text("❌ Format: /cek KODE\nContoh: /cek ANTM")
        return

    kode = ctx.args[0].upper().strip()
    wl   = load_watchlist()
    meta = wl.get(kode)

    msg = await update.message.reply_text(f"⏳ Mengambil data {kode}...")

    d = get_price_data(kode)
    if not d:
        await msg.edit_text(f"❌ Data untuk *{kode}* tidak ditemukan. Cek kode saham.", parse_mode="Markdown")
        return

    entry  = meta["entry"]  if meta else d["current"]
    cl_pct = meta["cl_pct"] if meta else DEFAULT_CL_PCT
    tp_pct = meta["tp_pct"] if meta else DEFAULT_TP_PCT

    ana  = analyze(d, entry, cl_pct, tp_pct)
    # Simpan verdict ke d untuk dipakai di chart title
    d["verdict"] = ana["verdict"]
    card = build_card(d, ana, entry)

    note = "" if meta else "\n\n_💡 Saham belum di watchlist. /add untuk pantau otomatis._"
    await msg.edit_text(card + note, parse_mode="Markdown")

    # Kirim chart setelah teks
    await update.message.reply_text("📊 Membuat chart...")
    try:
        chart_bytes = await asyncio.get_event_loop().run_in_executor(
            None, generate_chart, d, ana, entry
        )
        from io import BytesIO
        await update.message.reply_photo(
            photo=BytesIO(chart_bytes),
            caption=f"📈 *{kode}* — 60 hari terakhir\nCandlestick + BB + MA + RSI + MACD",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Chart error {kode}: {e}")
        await update.message.reply_text("⚠️ Chart gagal dibuat. Data teks di atas tetap valid.")

async def cmd_chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Kirim chart saja tanpa teks analisis lengkap."""
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text("❌ Format: /chart KODE\nContoh: /chart ANTM")
        return

    kode = ctx.args[0].upper().strip()
    wl   = load_watchlist()
    meta = wl.get(kode)

    msg = await update.message.reply_text(f"📊 Membuat chart {kode}...")

    d = get_price_data(kode)
    if not d:
        await msg.edit_text(f"❌ Data *{kode}* tidak ditemukan.", parse_mode="Markdown")
        return

    entry  = meta["entry"]  if meta else d["current"]
    cl_pct = meta["cl_pct"] if meta else DEFAULT_CL_PCT
    tp_pct = meta["tp_pct"] if meta else DEFAULT_TP_PCT

    ana = analyze(d, entry, cl_pct, tp_pct)
    d["verdict"] = ana["verdict"]

    try:
        chart_bytes = await asyncio.get_event_loop().run_in_executor(
            None, generate_chart, d, ana, entry
        )
        from io import BytesIO
        await msg.delete()
        await update.message.reply_photo(
            photo=BytesIO(chart_bytes),
            caption=(
                f"📈 *{kode}* — {d['name'][:30]}\n"
                f"Rp {fmt(d['current'])}  {d['chg_pct']:+.2f}%  |  "
                f"RSI {d['rsi']:.1f}  |  {ana['verdict']}\n"
                f"TP: {fmt(ana['tp'])}  •  CL: {fmt(ana['cl'])}"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Chart error {kode}: {e}")
        await msg.edit_text("⚠️ Gagal membuat chart. Coba /cek untuk analisis teks.")

async def cmd_setcl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if len(ctx.args) < 2:
        await update.message.reply_text("❌ Format: /setcl KODE PERSEN\nContoh: /setcl ANTM -8")
        return

    kode = ctx.args[0].upper()
    try:
        val  = float(ctx.args[1])
    except ValueError:
        await update.message.reply_text("❌ % tidak valid.")
        return

    if val > 0: val = -val   # pastikan negatif

    wl = load_watchlist()
    if kode not in wl:
        await update.message.reply_text(f"❌ {kode} tidak ada di watchlist.")
        return

    wl[kode]["cl_pct"] = val
    save_watchlist(wl)
    await update.message.reply_text(f"✅ CL *{kode}* diset ke *{val}%*", parse_mode="Markdown")

async def cmd_settp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if len(ctx.args) < 2:
        await update.message.reply_text("❌ Format: /settp KODE PERSEN\nContoh: /settp ANTM 20")
        return

    kode = ctx.args[0].upper()
    try:
        val  = float(ctx.args[1])
    except ValueError:
        await update.message.reply_text("❌ % tidak valid.")
        return

    if val < 0: val = -val   # pastikan positif

    wl = load_watchlist()
    if kode not in wl:
        await update.message.reply_text(f"❌ {kode} tidak ada di watchlist.")
        return

    wl[kode]["tp_pct"] = val
    save_watchlist(wl)
    await update.message.reply_text(f"✅ TP *{kode}* diset ke *+{val}%*", parse_mode="Markdown")

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    wl = load_watchlist()
    if not wl:
        await update.message.reply_text("📋 Watchlist kosong.")
        return

    msg = await update.message.reply_text(f"⏳ Scanning {len(wl)} saham...")
    await do_scan(ctx.bot, update.effective_chat.id, wl, header="📡 *SCAN MANUAL*")
    await msg.delete()

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cek status storage & bot."""
    if not is_allowed(update): return
    info = get_storage_info()
    wl   = load_watchlist()
    now  = datetime.now(WIB).strftime("%d %b %Y %H:%M WIB")

    lines = [
        "⚙️ *STATUS BOT*\n",
        f"🕐 Waktu: {now}",
        f"💾 Storage: {info['mode']}",
        f"📂 Path: `{info['path']}`",
        f"📄 File utama: {'✅ Ada' if info['has_primary'] else '❌ Belum ada'}",
        f"🔁 Backup: {'✅ Ada' if info['has_backup'] else '❌ Belum ada'}",
        f"💿 Ukuran: {info['size']} bytes",
        f"🕓 Terakhir disimpan: {info['last_saved']}",
        f"\n📋 Watchlist: *{len(wl)} saham*",
        f"⏰ Auto-scan: 09:00 & 15:00 WIB",
        f"📉 Default CL: {DEFAULT_CL_PCT}% | TP: +{DEFAULT_TP_PCT}%",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await cmd_start(update, ctx)

# ── Scheduled scan ────────────────────────────────────────────────────────
async def do_scan(bot, chat_id: int, wl: dict, header: str = ""):
    now_str = datetime.now(WIB).strftime("%d %b %Y %H:%M WIB")
    alerts  = []
    results = []

    for kode, meta in wl.items():
        d = get_price_data(kode)
        if not d:
            continue
        ana = analyze(d, meta["entry"], meta["cl_pct"], meta["tp_pct"])
        results.append((d, ana, meta["entry"]))
        if ana["alert"]:
            alerts.append((kode, ana["alert"], d["current"], ana["cl"], ana["tp"]))

    if not results:
        await bot.send_message(chat_id, "⚠️ Gagal mengambil data. Coba lagi nanti.")
        return

    # Kirim header
    hdr = f"{header}\n🕐 {now_str}\n{'━'*22}\n"
    await bot.send_message(chat_id, hdr, parse_mode="Markdown")

    # Kirim kartu per saham
    for d, ana, entry in results:
        card = build_card(d, ana, entry)
        await bot.send_message(chat_id, card, parse_mode="Markdown")
        await asyncio.sleep(0.3)

    # Kirim ringkasan alert
    if alerts:
        alert_lines = ["🚨 *ALERT PENTING:*\n"]
        for kode, atype, price, cl, tp in alerts:
            if atype == "CUT_LOSS":
                alert_lines.append(f"⛔ *{kode}* → Harga Rp {fmt(price)} ≤ CL Rp {fmt(cl)}\nSegera pertimbangkan *CUT LOSS!*")
            else:
                alert_lines.append(f"🎯 *{kode}* → Harga Rp {fmt(price)} ≥ TP Rp {fmt(tp)}\nTarget tercapai! Pertimbangkan *TAKE PROFIT!*")
        await bot.send_message(chat_id, "\n\n".join(alert_lines), parse_mode="Markdown")

async def scheduled_scan(app: Application):
    """Dipanggil scheduler 2x sehari."""
    wl = load_watchlist()
    if not wl:
        return

    now  = datetime.now(WIB)
    sess = "🌅 MARKET OPEN" if now.hour < 12 else "🌇 MARKET CLOSE"

    # Kirim ke semua ALLOWED_IDS, atau lewati jika tidak dikonfigurasi
    if ALLOWED_IDS:
        for uid in ALLOWED_IDS:
            try:
                await do_scan(app.bot, uid, wl, header=f"📊 *{sess}*")
            except Exception as e:
                logger.error(f"Scheduled scan error for {uid}: {e}")
    else:
        logger.info("No ALLOWED_USER_IDS set — skipping scheduled scan")

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("del",    cmd_del))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("cek",    cmd_cek))
    app.add_handler(CommandHandler("chart",  cmd_chart))
    app.add_handler(CommandHandler("setcl",  cmd_setcl))
    app.add_handler(CommandHandler("settp",  cmd_settp))
    app.add_handler(CommandHandler("scan",   cmd_scan))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help",   cmd_help))

    # Scheduler — WIB 09:00 & 15:00
    scheduler = AsyncIOScheduler(timezone=WIB)
    scheduler.add_job(scheduled_scan, "cron", hour=9,  minute=0, args=[app])
    scheduler.add_job(scheduled_scan, "cron", hour=15, minute=0, args=[app])
    scheduler.start()

    logger.info("Bot started. Scheduler running for 09:00 & 15:00 WIB.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
