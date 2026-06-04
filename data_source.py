"""
data_source.py  –  IDX Official API Only
=========================================
Endpoint:
  1. GetStockSummary  → harga terkini (stabil ✅)
  2. GetChartStockbyCode → historis OHLCV (kadang 503, retry 3x)

Kalau GetChartStockbyCode gagal terus → return None,
bot akan kasih pesan error ke user.
"""

import logging
import time
import random
from datetime import datetime
from typing import Optional

import requests
import pandas as pd

logger = logging.getLogger(__name__)

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

MAX_RETRY = 3


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(IDX_HEADERS)
    return s


def _sleep(base: float = 1.5):
    time.sleep(base + random.uniform(0.3, 1.2))


# ── IDX GetStockSummary (harga terkini) ──────────────────────────────────
def get_current_price(code: str) -> Optional[dict]:
    """
    Ambil harga terkini dari IDX GetStockSummary.
    Return dict: code, name, open, high, low, close, previous,
                 change, change_pct, volume, date
    """
    code = code.upper().strip()
    url  = IDX_SUMMARY_URL.format(code=code)
    sess = _session()

    for attempt in range(MAX_RETRY):
        try:
            r = sess.get(url, timeout=15)
            r.raise_for_status()
            payload = r.json()
            rows    = payload.get("data", payload.get("Data", []))
            if not rows:
                return None

            row     = rows[0]
            close   = float(row.get("Close")     or 0)
            prev    = float(row.get("Previous")  or close)
            open_   = float(row.get("OpenPrice") or prev)
            high    = float(row.get("High")      or close)
            low     = float(row.get("Low")       or close)
            vol     = int(row.get("Volume")      or 0)
            chg     = float(row.get("Change")    or 0)
            chg_pct = (chg / prev * 100) if prev else 0
            date_str= (row.get("Date") or "")[:10]

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
            }
        except Exception as e:
            logger.warning(f"[IDX Summary] {code} attempt {attempt+1}: {e}")
            _sleep()

    return None


# ── IDX GetChartStockbyCode (historis OHLCV) ─────────────────────────────
def get_stock_data(code: str, days: int = 200) -> Optional[pd.DataFrame]:
    """
    Ambil data historis OHLCV dari IDX GetChartStockbyCode.
    Retry 3x dengan jitter sleep untuk bypass Varnish 503.

    Return: pd.DataFrame (DatetimeIndex, kolom Open/High/Low/Close/Volume)
            atau None jika gagal.
    """
    code   = code.upper().strip()
    period = 90 if days <= 90 else 180 if days <= 180 else 365 if days <= 365 else 730
    url    = IDX_CHART_URL.format(code=code, period=period)
    sess   = _session()

    for attempt in range(MAX_RETRY):
        try:
            r = sess.get(url, timeout=15)
            if r.status_code == 503:
                logger.warning(f"[IDX Chart] 503 Varnish attempt {attempt+1}/{MAX_RETRY}")
                _sleep(3.0)
                continue
            r.raise_for_status()

            raw  = r.json()
            rows = raw.get("data", raw.get("Data", raw if isinstance(raw, list) else []))

            if not rows:
                logger.warning(f"[IDX Chart] {code}: response kosong")
                _sleep()
                continue

            df = pd.DataFrame(rows)

            # Normalize nama kolom
            rename = {
                "OpenPrice": "Open", "open_price": "Open", "open": "Open",
                "High": "High", "high": "High",
                "Low":  "Low",  "low":  "Low",
                "Close":"Close","close":"Close",
                "Volume":"Volume","volume":"Volume",
                "Date": "Date", "date": "Date",
            }
            df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

            for col in ["Open", "High", "Low", "Close", "Volume"]:
                if col not in df.columns:
                    df[col] = 0.0

            if "Date" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
                df = df.dropna(subset=["Date"]).set_index("Date")
            else:
                df.index = pd.to_datetime(df.index, errors="coerce")

            df = df.sort_index()
            df = df[~df.index.duplicated(keep="last")]
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["Close"])
            df = df[["Open", "High", "Low", "Close", "Volume"]]

            if len(df) < 10:
                logger.warning(f"[IDX Chart] {code}: data kurang ({len(df)} baris)")
                _sleep()
                continue

            logger.info(f"[IDX Chart] ✅ {code}: {len(df)} baris")
            return df

        except Exception as e:
            logger.warning(f"[IDX Chart] {code} attempt {attempt+1}: {e}")
            _sleep(2.0)

    logger.error(f"[IDX Chart] ❌ {code}: semua retry gagal")
    return None


# ── Diagnostik /testsource ───────────────────────────────────────────────
def test_sources(code: str = "BBRI") -> dict:
    results = {}

    try:
        s = get_current_price(code)
        if s and s["close"] > 0:
            results["idx_summary"] = f"✅ Close=Rp {s['close']:,.0f} ({s['date']})"
        else:
            results["idx_summary"] = "⚠️ Data kosong"
    except Exception as e:
        results["idx_summary"] = f"❌ {e}"

    try:
        df = get_stock_data(code, 60)
        results["idx_chart"] = f"✅ {len(df)} baris" if df is not None else "❌ Gagal/503"
    except Exception as e:
        results["idx_chart"] = f"❌ {e}"

    return results
