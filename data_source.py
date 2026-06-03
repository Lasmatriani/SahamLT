"""
data_source.py  –  IDX Stock Data Fetcher
==========================================
Berdasarkan konfirmasi endpoint aktual:

  GetStockSummary → BEKERJA ✅ (harga hari ini, 1 baris OHLCV)
  GetChartStockbyCode → kadang 503 Varnish ⚠️ (coba dulu, fallback)

Fallback chain untuk data HISTORIS (diperlukan /chart, RSI, MACD, BB):
  1. IDX GetChartStockbyCode  (coba 3x dengan delay)
  2. yfinance  (<CODE>.JK)
  3. Stooq     (<CODE>.ID)

Untuk /cek (harga terkini):
  → IDX GetStockSummary langsung (cepat, stabil)
"""

import logging
import time
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple

import requests
import pandas as pd

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# HEADERS IDX  (penting: tanpa ini sering 403/503)
# ─────────────────────────────────────────────────────────────
IDX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.idx.co.id/id/data-pasar/ringkasan-perdagangan/ringkasan-saham/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9",
    "Origin": "https://www.idx.co.id",
    "X-Requested-With": "XMLHttpRequest",
}

IDX_SUMMARY_URL = (
    "https://www.idx.co.id/primary/TradingSummary/GetStockSummary"
    "?start=0&length=1&code={code}&lang=id"
)

IDX_CHART_URL = (
    "https://www.idx.co.id/primary/StockData/GetChartStockbyCode"
    "?indexCode={code}&period={period}"
)

REQUEST_TIMEOUT = 15
MAX_RETRY       = 3


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(IDX_HEADERS)
    return s


def _sleep(base: float = 1.0):
    time.sleep(base + random.uniform(0.3, 1.0))


def _parse_df(rows: list) -> Optional[pd.DataFrame]:
    """
    Ubah list of dict → DataFrame OHLCV bersih.
    Handles field-name variants dari berbagai endpoint IDX.
    """
    if not rows:
        return None

    df = pd.DataFrame(rows)

    # Normalize kolom
    rename = {
        "OpenPrice": "Open", "open_price": "Open", "open": "Open",
        "High": "High", "high": "High",
        "Low": "Low", "low": "Low",
        "Close": "Close", "close": "Close",
        "Volume": "Volume", "volume": "Volume",
        "Date": "Date", "date": "Date",
        "IDXDate": "Date", "tradingDate": "Date",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    required = ["Open", "High", "Low", "Close", "Volume"]
    for col in required:
        if col not in df.columns:
            df[col] = 0.0

    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date"])
        df = df.set_index("Date")
    elif not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce")

    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Close"])
    return df[required] if not df.empty else None


# ─────────────────────────────────────────────────────────────
# SOURCE 1A — IDX GetStockSummary  (harga hari ini)
# ─────────────────────────────────────────────────────────────
def _fetch_idx_summary(code: str) -> Optional[dict]:
    """
    Ambil harga terkini dari IDX.
    Return dict: code, name, open, high, low, close, prev,
                 change, change_pct, volume, date
    """
    url = IDX_SUMMARY_URL.format(code=code.upper())
    sess = _session()

    for attempt in range(MAX_RETRY):
        try:
            r = sess.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            payload = r.json()

            rows = payload.get("data", payload.get("Data", []))
            if not rows:
                return None

            row   = rows[0]
            close = float(row.get("Close") or 0)
            prev  = float(row.get("Previous") or close)
            open_ = float(row.get("OpenPrice") or prev)
            high  = float(row.get("High") or close)
            low   = float(row.get("Low") or close)
            vol   = int(row.get("Volume") or 0)
            chg   = float(row.get("Change") or 0)
            chg_pct = (chg / prev * 100) if prev else 0

            raw_date = row.get("Date", "")
            date_str = raw_date[:10] if raw_date else datetime.now().strftime("%Y-%m-%d")

            return {
                "code":       row.get("StockCode", code),
                "name":       row.get("StockName", ""),
                "open":       open_,
                "high":       high,
                "low":        low,
                "close":      close,
                "previous":   prev,
                "change":     chg,
                "change_pct": chg_pct,
                "volume":     vol,
                "date":       date_str,
                "source":     "IDX",
            }

        except Exception as e:
            logger.warning(f"[IDX Summary] {code} attempt {attempt+1}: {e}")
            _sleep(1.5)

    return None


# ─────────────────────────────────────────────────────────────
# SOURCE 1B — IDX GetChartStockbyCode  (historis)
# ─────────────────────────────────────────────────────────────
def _fetch_idx_chart(code: str, days: int = 200) -> Optional[pd.DataFrame]:
    """
    Coba endpoint chart IDX. Kadang 503 Varnish → return None kalau gagal.
    """
    period = 90 if days <= 90 else 180 if days <= 180 else 365 if days <= 365 else 730
    url    = IDX_CHART_URL.format(code=code.upper(), period=period)
    sess   = _session()

    for attempt in range(MAX_RETRY):
        try:
            r = sess.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 503:
                logger.warning(f"[IDX Chart] 503 Varnish attempt {attempt+1}/{MAX_RETRY}")
                _sleep(2.5)
                continue
            r.raise_for_status()

            raw = r.json()
            rows = raw.get("data", raw.get("Data", raw if isinstance(raw, list) else []))
            df   = _parse_df(rows)

            if df is not None and len(df) >= 10:
                logger.info(f"[IDX Chart] ✅ {code}: {len(df)} baris")
                return df

        except Exception as e:
            logger.warning(f"[IDX Chart] {code} attempt {attempt+1}: {e}")
            _sleep(2.0)

    return None


# ─────────────────────────────────────────────────────────────
# SOURCE 2 — yfinance  (<CODE>.JK)
# ─────────────────────────────────────────────────────────────
def _fetch_yfinance(code: str, days: int = 200) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("[yfinance] tidak terinstall")
        return None

    ticker   = f"{code.upper()}.JK"
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days + 30)

    for attempt in range(MAX_RETRY):
        try:
            df = yf.download(
                ticker,
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                progress=False,
                auto_adjust=True,
                threads=False,
            )
            if df is None or df.empty:
                break

            # handle MultiIndex dari yfinance terbaru
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df = df[["Open", "High", "Low", "Close", "Volume"]]
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            df = df.dropna(subset=["Close"])

            if len(df) >= 10:
                logger.info(f"[yfinance] ✅ {ticker}: {len(df)} baris")
                return df

        except Exception as e:
            err = str(e).lower()
            if "429" in err or "too many" in err:
                wait = (2 ** attempt) * 3 + random.uniform(0, 2)
                logger.warning(f"[yfinance] 429 rate limit, tunggu {wait:.1f}s")
                time.sleep(wait)
            else:
                logger.warning(f"[yfinance] {ticker} attempt {attempt+1}: {e}")
                _sleep()

    logger.warning(f"[yfinance] ❌ gagal: {ticker}")
    return None


# ─────────────────────────────────────────────────────────────
# SOURCE 3 — Stooq  (<CODE>.ID)
# ─────────────────────────────────────────────────────────────
def _fetch_stooq(code: str, days: int = 200) -> Optional[pd.DataFrame]:
    try:
        import pandas_datareader.data as web
    except ImportError:
        logger.warning("[stooq] pandas_datareader tidak terinstall")
        return None

    ticker   = f"{code.upper()}.ID"
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days + 30)

    for attempt in range(MAX_RETRY):
        try:
            df = web.DataReader(ticker, "stooq", start_dt, end_dt)
            if df is None or df.empty:
                break

            df.columns = [c.title() for c in df.columns]
            df = df[["Open", "High", "Low", "Close", "Volume"]]
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            df = df.dropna(subset=["Close"])

            if len(df) >= 10:
                logger.info(f"[Stooq] ✅ {ticker}: {len(df)} baris")
                return df

        except Exception as e:
            logger.warning(f"[Stooq] {ticker} attempt {attempt+1}: {e}")
            _sleep(1.5)

    logger.warning(f"[Stooq] ❌ gagal: {ticker}")
    return None


# ─────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────
def get_current_price(code: str) -> Optional[dict]:
    """
    Ambil harga terkini untuk command /cek.
    Prioritas: IDX GetStockSummary → fallback get_stock_data (baris terakhir)

    Return dict:
      code, name, open, high, low, close, previous,
      change, change_pct, volume, date, source
    """
    code = code.upper().strip()

    # Coba IDX Summary dulu (paling cepat & real-time)
    result = _fetch_idx_summary(code)
    if result and result["close"] > 0:
        return result

    logger.info(f"[get_current_price] IDX Summary gagal untuk {code}, coba historis...")

    # Fallback: ambil baris terakhir dari data historis
    df = get_stock_data(code, days=5)
    if df is not None and not df.empty:
        last  = df.iloc[-1]
        prev  = df.iloc[-2]["Close"] if len(df) >= 2 else last["Close"]
        chg   = float(last["Close"] - prev)
        chg_pct = (chg / float(prev) * 100) if prev else 0
        return {
            "code":       code,
            "name":       "",
            "open":       float(last["Open"]),
            "high":       float(last["High"]),
            "low":        float(last["Low"]),
            "close":      float(last["Close"]),
            "previous":   float(prev),
            "change":     chg,
            "change_pct": chg_pct,
            "volume":     int(last["Volume"]),
            "date":       str(df.index[-1].date()),
            "source":     "yfinance/stooq",
        }

    return None


def get_stock_data(code: str, days: int = 200) -> Optional[pd.DataFrame]:
    """
    Ambil data historis OHLCV untuk chart & indikator teknikal.
    Fallback chain: IDX Chart → yfinance → Stooq

    Return: pd.DataFrame (DatetimeIndex, kolom: Open/High/Low/Close/Volume)
            atau None jika semua gagal.
    """
    code = code.upper().strip()
    logger.info(f"[get_stock_data] {code} days={days}")

    # 1. IDX Chart
    df = _fetch_idx_chart(code, days)
    if df is not None and len(df) >= 10:
        return df

    # 2. yfinance
    logger.info(f"[get_stock_data] IDX Chart gagal → yfinance")
    df = _fetch_yfinance(code, days)
    if df is not None and len(df) >= 10:
        return df

    # 3. Stooq
    logger.info(f"[get_stock_data] yfinance gagal → Stooq")
    df = _fetch_stooq(code, days)
    if df is not None and len(df) >= 10:
        return df

    logger.error(f"[get_stock_data] ❌ semua source gagal: {code}")
    return None


def test_sources(code: str = "BBRI") -> dict:
    """
    Diagnostik semua source. Dipakai oleh /testsource command.
    """
    results = {}

    # IDX Summary
    try:
        s = _fetch_idx_summary(code)
        if s and s["close"] > 0:
            results["idx_summary"] = f"✅ Close={s['close']:,.0f} ({s['date']})"
        else:
            results["idx_summary"] = "⚠️ Data kosong"
    except Exception as e:
        results["idx_summary"] = f"❌ {e}"

    # IDX Chart
    try:
        df = _fetch_idx_chart(code, 60)
        results["idx_chart"] = f"✅ {len(df)} baris" if df is not None else "❌ None/503"
    except Exception as e:
        results["idx_chart"] = f"❌ {e}"

    # yfinance
    try:
        df = _fetch_yfinance(code, 30)
        results["yfinance"] = f"✅ {len(df)} baris" if df is not None else "❌ None"
    except Exception as e:
        results["yfinance"] = f"❌ {e}"

    # Stooq
    try:
        df = _fetch_stooq(code, 30)
        results["stooq"] = f"✅ {len(df)} baris" if df is not None else "❌ None"
    except Exception as e:
        results["stooq"] = f"❌ {e}"

    return results
